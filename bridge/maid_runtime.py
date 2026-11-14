"""Run control, live run state and the sandbox permission gate.

Purpose
    * RunControl   the one object through which the orchestrator talks to whoever is
                   driving it. The base class drives a terminal; bridge/maid_ui.py
                   subclasses it to drive the web dashboard. It holds the live run
                   state (agents, units, messages), pause/halt/steer, and the hooks
                   for clarifying questions and permission prompts.
    * SandboxGate  keeps every agent inside the target repository. Anything outside
                   needs an explicit, justified approval from the user.
    * File IPC     lets the MCP server (a grandchild process of the orchestrator)
                   ask the orchestrator for outside access.

Sandbox model
    Workers are started with the repository as their working directory and no extra
    directories, so the worker CLIs themselves refuse anything outside it in headless
    mode. The gate adds the approval path on top:
      1. Every prompt sent to a worker is pre-scanned for paths outside the repo.
      2. Workers can call the MCP tool `request_outside_access`.
    Either way the run pauses (`awaiting_permission`), a SANDBOX_PERMISSION_REQUEST is
    logged, and the user chooses Approve Once, Approve for Session, or Deny & Constrain
    to Repo. Approved directories are passed to the next worker call with `--add-dir`.

State mutations
    * In-memory run state only, plus request/response files under
      log/sandbox/<correlation-id>/ (git-ignored).

Side effects
    Terminal prompts (base RunControl). No network access.

@rollback
    Delete log/sandbox/ to discard pending requests. Grants live in memory and end
    with the process; SandboxGate.revoke_all() drops them immediately. To revert this
    module, `git checkout -- bridge/maid_runtime.py`. Standard library only, Python 3.9+.
"""
import copy
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from maid_common import LOG_DIR, get_correlation_id, log_event

AGENT_KEYS = ("agent0", "agy", "claude")
AGENT_TITLES = {
    "agent0": "AGENT 0 (LOCAL - BRAIN)",
    "agy": "AGENT 1 (AGY - WORKER/REVIEWER)",
    "claude": "AGENT 2 (CLAUDE - WORKER/REVIEWER)",
}
DECISIONS = ("approve_once", "approve_session", "deny")
STREAM_BUFFER_CHARS = 60000
MESSAGE_LIMIT = 200
SANDBOX_POLL_SECONDS = 0.5

# A path-looking token: ~/..., an absolute path, or one that climbs with ../
_PATH_TOKEN = re.compile(r"(?<![\w.:/~])(~/[^\s'\"`<>|;,)\]]+|/(?:[\w.@+-]+/)*[\w.@+-]+|(?:\.\./)+[\w./@+-]*)")


class RunHalted(Exception):
    """Raised inside the orchestrator when the user halts the run.

    @rollback: none; the orchestrator rolls back the unit in progress when it sees this.
    """


# --------------------------------------------------------------------------- #
# Sandbox: pure path checks
# --------------------------------------------------------------------------- #


def is_path_within_repo(path, repo):
    """Return True if `path` resolves to the repository root or something inside it.

    Relative paths are taken relative to the repo. `~`, `..` and symlinks are
    resolved first, so `repo/../etc/passwd` and a symlink pointing out of the repo
    are both outside.

    @rollback: none (read-only filesystem queries).
    """
    root = Path(repo).resolve()
    candidate = Path(os.path.expanduser(str(path)))
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    return resolved == root or root in resolved.parents


def find_outside_paths(text, repo):
    """Return the paths mentioned in `text` that resolve outside the repository.

    A heuristic pre-scan, used on prompts before they reach a worker. To keep URL
    paths such as /v1/models from raising false alarms, an absolute path only counts
    when its first component exists on this machine.

    @rollback: none (read-only filesystem queries).
    """
    found = []
    for match in _PATH_TOKEN.finditer(text or ""):
        token = match.group(1).rstrip(".:")
        if token.startswith("/"):
            parts = Path(token).parts
            if len(parts) < 2 or not Path("/", parts[1]).exists():
                continue
        if not is_path_within_repo(token, repo) and token not in found:
            found.append(token)
    return found


def permission_payload(target_path, reason, goal_impact, requester="worker"):
    """Build the request shown to the user by the permission gate.

    Returns: dict with id, target_path (absolute, resolved), reason, goal_impact,
        requester, requested_at and the three available actions.

    @rollback: none (pure apart from resolving the path).
    """
    return {
        "id": uuid.uuid4().hex[:10],
        "target_path": str(Path(os.path.expanduser(str(target_path))).resolve()),
        "reason": str(reason or "").strip() or "(no reason given)",
        "goal_impact": str(goal_impact or "").strip() or "(not stated)",
        "requester": requester,
        "requested_at": time.time(),
        "actions": [
            {"decision": "approve_once", "label": "Approve Once"},
            {"decision": "approve_session", "label": "Approve for Session"},
            {"decision": "deny", "label": "Deny & Constrain to Repo"},
        ],
    }


# --------------------------------------------------------------------------- #
# Sandbox: file IPC between the MCP server and the orchestrator
# --------------------------------------------------------------------------- #


def sandbox_ipc_dir(correlation_id=None):
    """Return the directory where outside-access requests for a run are exchanged.

    @rollback: none (pure).
    """
    return LOG_DIR / "sandbox" / (correlation_id or get_correlation_id())


def _write_json(path, value):
    """Write JSON atomically, so a reader never sees a half-written file.

    State mutations: creates or replaces `path`.
    @rollback: delete `path`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp-%s" % uuid.uuid4().hex[:6])
    temporary.write_text(json.dumps(value), encoding="utf-8")
    os.replace(str(temporary), str(path))


def submit_outside_request(payload, timeout):
    """Called in the MCP server: file a request and wait for the orchestrator's answer.

    Returns: {"decision": ..., "note": ...}. If no orchestrator answers within
        `timeout` seconds the request is denied, so an unattended worker can never
        gain access by waiting.

    State mutations: writes req-<id>.json under sandbox_ipc_dir() and removes it again
        if it times out.
    @rollback: delete the request file; an unanswered request grants nothing.
    """
    directory = sandbox_ipc_dir()
    request_path = directory / ("req-%s.json" % payload["id"])
    response_path = directory / ("res-%s.json" % payload["id"])
    _write_json(request_path, payload)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if response_path.is_file():
            try:
                return json.loads(response_path.read_text(encoding="utf-8"))
            except ValueError:
                pass
        time.sleep(SANDBOX_POLL_SECONDS)
    try:
        request_path.unlink()
    except OSError:
        pass
    return {"decision": "deny", "note": "no orchestrator answered within %ds" % timeout}


def pending_outside_requests():
    """Called in the orchestrator: return the filed requests that have no answer yet.

    @rollback: none (read-only).
    """
    directory = sandbox_ipc_dir()
    if not directory.is_dir():
        return []
    pending = []
    for request_path in sorted(directory.glob("req-*.json")):
        if (directory / request_path.name.replace("req-", "res-", 1)).exists():
            continue
        try:
            pending.append(json.loads(request_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return pending


def answer_outside_request(request_id, decision, note=""):
    """Called in the orchestrator: write the answer the MCP server is waiting for.

    State mutations: writes res-<id>.json under sandbox_ipc_dir().
    @rollback: none useful; the MCP server reads the answer within a second.
    """
    _write_json(sandbox_ipc_dir() / ("res-%s.json" % request_id), {"decision": decision, "note": note})


# --------------------------------------------------------------------------- #
# Run control and live state
# --------------------------------------------------------------------------- #


def _fresh_state():
    """Return the initial live-state dict.

    @rollback: none (pure).
    """
    return {
        "run": {"id": "", "status": "idle", "task": "", "repo": "", "spec_path": "", "started_at": 0, "error": ""},
        "agents": {
            key: {"title": AGENT_TITLES[key], "state": "idle", "model": "", "target": "", "since": time.time()}
            for key in AGENT_KEYS
        },
        "units": [],
        "questions": None,
        "permission": None,
        "sandbox": "LOCKED",
        "messages": [],
        "paused": False,
    }


class RunControl:
    """The orchestrator's link to its driver, with terminal behaviour by default.

    Thread-safety: state is guarded by a re-entrant lock; listeners are called
    outside any long operation and must not block.

    State mutations: in-memory only.
    @rollback: `reset()` returns to the initial state.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._listeners = []
        self._paused = threading.Event()
        self._halted = threading.Event()
        self._steers = {key: [] for key in AGENT_KEYS}
        self._process = None
        self.state = _fresh_state()
        self.streams = {key: "" for key in AGENT_KEYS}

    # -- events ----------------------------------------------------------- #

    def subscribe(self, listener):
        """Register `listener(kind, payload)` for state, chunk and message events.

        @rollback: remove it from `self._listeners`.
        """
        self._listeners.append(listener)

    def emit(self, kind, payload):
        """Send one event to every listener; a failing listener is ignored.

        @rollback: none needed.
        """
        for listener in list(self._listeners):
            try:
                listener(kind, payload)
            except Exception:  # noqa: BLE001 - a broken listener must never stop a run
                pass

    def snapshot(self):
        """Return a deep copy of the live state, safe to serialise.

        @rollback: none (read-only).
        """
        with self._lock:
            return copy.deepcopy(self.state)

    def _changed(self):
        self.emit("state", self.snapshot())

    # -- state updates ---------------------------------------------------- #

    def reset(self, task="", repo=""):
        """Start a new run: clear state, flags, steering and stream buffers.

        State mutations: replaces the whole live state.
        @rollback: not reversible; the previous run's state is in the logs.
        """
        with self._lock:
            models = {key: agent["model"] for key, agent in self.state["agents"].items()}
            self.state = _fresh_state()
            for key, model in models.items():
                self.state["agents"][key]["model"] = model
            self.state["run"].update(
                id=get_correlation_id(), status="starting", task=task, repo=str(repo), started_at=time.time()
            )
            self.streams = {key: "" for key in AGENT_KEYS}
            self._steers = {key: [] for key in AGENT_KEYS}
        self._paused.clear()
        self._halted.clear()
        self._changed()

    def set_run(self, **fields):
        """Update fields of the run block (status, spec_path, error, ...).

        @rollback: call again with the previous values.
        """
        with self._lock:
            self.state["run"].update(fields)
        self._changed()

    def set_agent(self, agent, state, target="", model=None):
        """Set an agent's live state: idle, thinking, coding, reviewing, awaiting_permission.

        @rollback: call again with the previous values.
        """
        with self._lock:
            block = self.state["agents"][agent]
            block.update(state=state, target=target, since=time.time())
            if model is not None:
                block["model"] = model
        self._changed()

    def set_models(self, models):
        """Record the active model of each agent, from maid_common.get_models().

        @rollback: call again with the previous mapping.
        """
        with self._lock:
            for key, name in (("agent0", models.get("local")), ("agy", models.get("agy")), ("claude", models.get("claude"))):
                self.state["agents"][key]["model"] = name or "default"
        self._changed()

    def set_units(self, units):
        """Publish the plan's units as Backlog cards.

        @rollback: `set_units([])`.
        """
        with self._lock:
            self.state["units"] = [
                {
                    "id": unit["id"], "file": unit["file"], "action": unit["action"], "symbol": unit.get("symbol", ""),
                    "description": unit.get("description", ""), "agent": unit.get("suggested_agent", ""),
                    "column": "backlog", "status": "pending", "attempt": 0, "checks": [], "rollback": "", "diff": "",
                }
                for unit in units
            ]
        self._changed()

    def update_unit(self, unit_id, **fields):
        """Update one unit card (column, status, agent, attempt, checks, rollback, diff).

        @rollback: call again with the previous values.
        """
        with self._lock:
            for unit in self.state["units"]:
                if unit["id"] == unit_id:
                    unit.update(fields)
        self._changed()

    def add_message(self, sender, recipient, purpose, prompt, response=None):
        """Record a prompt (and later its response) for the inspector's MESSAGES tab.

        Returns: the message id, to pass to complete_message().
        @rollback: none needed; the list is capped at MESSAGE_LIMIT.
        """
        message = {
            "id": uuid.uuid4().hex[:8], "at": time.time(), "from": sender, "to": recipient,
            "purpose": purpose, "prompt": prompt, "response": response,
        }
        with self._lock:
            self.state["messages"] = (self.state["messages"] + [message])[-MESSAGE_LIMIT:]
        self._changed()
        return message["id"]

    def complete_message(self, message_id, response):
        """Attach the raw response to a message recorded with add_message().

        @rollback: none needed.
        """
        with self._lock:
            for message in self.state["messages"]:
                if message["id"] == message_id:
                    message["response"] = response
        self._changed()

    def chunk(self, agent, stream, text):
        """Publish a piece of live output: LM Studio tokens or worker stdout/stderr.

        Args:
            agent: "agent0", "agy" or "claude".
            stream: "reasoning", "content", "stdout", "stderr" or "info".

        State mutations: appends to the agent's bounded stream buffer.
        @rollback: none needed.
        """
        if not text:
            return
        with self._lock:
            self.streams[agent] = (self.streams[agent] + text)[-STREAM_BUFFER_CHARS:]
        self.emit("chunk", {"agent": agent, "stream": stream, "text": text})

    # -- interaction (terminal defaults; the UI overrides these) ---------- #

    def ask_questions(self, questions):
        """Ask the user Agent 0's clarifying questions and return the answers.

        Terminal behaviour: print each question and read a line. A closed stdin
        yields empty answers.

        @rollback: none needed.
        """
        answers = []
        for number, question in enumerate(questions, 1):
            print("\n[Agent 0] Question %d/%d: %s" % (number, len(questions), question))
            try:
                answers.append(input("> ").strip())
            except EOFError:
                answers.append("")
        return answers

    def ask_permission(self, payload):
        """Ask the user to decide a sandbox request; return one of DECISIONS.

        Terminal behaviour: show the request and read 1, 2 or 3. Anything else,
        including a closed stdin, denies.

        @rollback: none needed; deciding again later replaces the grant.
        """
        print("\n🔒 SANDBOX PERMISSION REQUEST (from %s)" % payload["requester"])
        print("   target_path: %s" % payload["target_path"])
        print("   reason:      %s" % payload["reason"])
        print("   goal_impact: %s" % payload["goal_impact"])
        print("   [1] Approve Once   [2] Approve for Session   [3] Deny & Constrain to Repo")
        try:
            choice = input("> ").strip()
        except EOFError:
            choice = ""
        return {"1": "approve_once", "2": "approve_session"}.get(choice, "deny")

    def set_permission(self, payload):
        """Show or clear the pending permission request in the live state.

        @rollback: `set_permission(None)`.
        """
        with self._lock:
            self.state["permission"] = payload
            self.state["sandbox"] = "PENDING" if payload else "LOCKED"
        self._changed()

    # -- flow control ----------------------------------------------------- #

    def pause(self):
        """Pause the run at its next checkpoint (between model and worker calls).

        @rollback: `resume()`.
        """
        self._paused.set()
        with self._lock:
            self.state["paused"] = True
        self._changed()

    def resume(self):
        """Resume a paused run.

        @rollback: `pause()`.
        """
        self._paused.clear()
        with self._lock:
            self.state["paused"] = False
        self._changed()

    def halt(self):
        """Stop the run: kill the worker in progress and abort at the next checkpoint.

        State mutations: terminates the registered subprocess, if any.
        @rollback: not reversible; the orchestrator rolls back the unit in progress.
        """
        self._halted.set()
        self._paused.clear()
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
        with self._lock:
            self.state["paused"] = False
        self._changed()

    @property
    def halted(self):
        return self._halted.is_set()

    def checkpoint(self):
        """Block while paused; raise RunHalted if the run was halted.

        Called by the orchestrator before every model call and worker call.
        @rollback: none needed.
        """
        while self._paused.is_set() and not self._halted.is_set():
            time.sleep(0.2)
        if self._halted.is_set():
            raise RunHalted("run halted by the user")

    def register_process(self, process):
        """Remember the running worker subprocess so halt() can stop it (None clears).

        @rollback: `register_process(None)`.
        """
        self._process = process

    def steer(self, agent, text):
        """Queue steering text for an agent; it is injected into its next prompt.

        @rollback: `take_steer(agent)` drains the queue.
        """
        text = str(text or "").strip()
        if text and agent in self._steers:
            with self._lock:
                self._steers[agent].append(text)
            log_event_safe("steer_queued", "steering queued for %s" % agent, agent=agent, text=text)

    def take_steer(self, agent):
        """Return and clear the steering text queued for an agent.

        @rollback: re-queue the returned strings with steer().
        """
        with self._lock:
            taken, self._steers[agent] = self._steers[agent], []
        return taken


def log_event_safe(event, message, **data):
    """Log through the orchestrator's logger if it exists; otherwise do nothing.

    Lets this module log without owning a logger that would claim the console.
    @rollback: none needed.
    """
    import logging

    logger = logging.getLogger("maid.runtime")
    if logging.getLogger("maid").handlers:
        log_event(logger, event, message, **data)


# --------------------------------------------------------------------------- #
# Sandbox gate
# --------------------------------------------------------------------------- #


class SandboxGate:
    """Decides, with the user, whether an agent may touch a path outside the repo.

    State mutations: in-memory grant sets only.
    @rollback: `revoke_all()` drops every grant; denying is the default everywhere.
    """

    def __init__(self, repo, control):
        self.repo = Path(repo).resolve()
        self.control = control
        self._session = set()
        self._once = set()
        self._denied = set()

    @staticmethod
    def _grant_dir(path):
        """A grant covers a directory: the path itself, or its parent for a file."""
        path = Path(path)
        return path if path.is_dir() else path.parent

    def is_allowed(self, path):
        """Return True if `path` is inside the repo or inside a granted directory.

        @rollback: none (read-only).
        """
        if is_path_within_repo(path, self.repo):
            return True
        resolved = Path(os.path.expanduser(str(path))).resolve()
        return any(grant == resolved or grant in resolved.parents for grant in self._session | self._once)

    def granted_dirs(self):
        """Return the directories to hand to the next worker call via --add-dir.

        @rollback: none (read-only).
        """
        return sorted(str(path) for path in self._session | self._once)

    def consume_once(self, used=None):
        """Drop Approve Once grants after the worker call that used them.

        Args:
            used: the directories that were passed to that call. A grant approved
                while the call was already running was not usable by it, so it is
                kept for the next call. None drops every Approve Once grant.

        @rollback: not reversible; request the access again.
        """
        if used is None:
            self._once.clear()
        else:
            self._once -= {Path(directory) for directory in used}

    def revoke_all(self):
        """Drop every grant and remembered denial.

        @rollback: not reversible; request the access again.
        """
        self._session.clear()
        self._once.clear()
        self._denied.clear()

    def request(self, target_path, reason, goal_impact, requester="worker", waiting_agent=None):
        """Ask the user for access to one outside path and record the decision.

        Returns: "not_needed" for a path inside the repo, otherwise one of DECISIONS.
            A path already covered by a grant, or already denied in this run, is
            answered from memory without asking again.

        State mutations: may add a grant or a denial; shows and clears the pending
            request in the live state (`awaiting_permission`).
        Side effects: logs SANDBOX_PERMISSION_REQUEST and SANDBOX_PERMISSION_DECISION.
        @rollback: `revoke_all()`.
        """
        if is_path_within_repo(target_path, self.repo):
            return "not_needed"
        payload = permission_payload(target_path, reason, goal_impact, requester)
        resolved = Path(payload["target_path"])
        if self.is_allowed(resolved):
            return "approve_session"
        if resolved in self._denied:
            return "deny"

        log_event_safe(
            "SANDBOX_PERMISSION_REQUEST",
            "%s requests access outside the repo: %s" % (requester, payload["target_path"]),
            **{key: payload[key] for key in ("id", "target_path", "reason", "goal_impact", "requester")},
        )
        previous = None
        if waiting_agent:
            previous = dict(self.control.snapshot()["agents"][waiting_agent])
            self.control.set_agent(waiting_agent, "awaiting_permission", payload["target_path"])
        self.control.set_permission(payload)
        try:
            decision = self.control.ask_permission(payload)
        finally:
            self.control.set_permission(None)
            if previous:
                self.control.set_agent(waiting_agent, previous["state"], previous["target"])
        if decision not in DECISIONS:
            decision = "deny"
        if decision == "approve_session":
            self._session.add(self._grant_dir(resolved))
        elif decision == "approve_once":
            self._once.add(self._grant_dir(resolved))
        else:
            self._denied.add(resolved)
        log_event_safe(
            "SANDBOX_PERMISSION_DECISION",
            "%s -> %s" % (payload["target_path"], decision),
            id=payload["id"],
            target_path=payload["target_path"],
            decision=decision,
            granted_dirs=self.granted_dirs(),
        )
        return decision

    def prescan(self, text, requester, waiting_agent=None):
        """Pre-scan a prompt for outside paths and ask about each one.

        Returns: (approved, denied) lists of path strings as written in the text.

        State mutations / side effects: see request().
        @rollback: `revoke_all()`.
        """
        approved, denied = [], []
        for token in find_outside_paths(text, self.repo):
            decision = self.request(
                token,
                "The instructions for this unit reference this path, which is outside the repository.",
                "The worker may need to read or write it to complete the unit as instructed.",
                requester=requester,
                waiting_agent=waiting_agent,
            )
            (denied if decision == "deny" else approved).append(token)
        return approved, denied

    def serve_pending(self, waiting_agent=None):
        """Answer the outside-access requests that workers filed through the MCP tool.

        Called by the orchestrator while a worker subprocess is running.
        Returns: the number of requests answered.

        State mutations / side effects: see request(); writes one response file each.
        @rollback: `revoke_all()`.
        """
        answered = 0
        for payload in pending_outside_requests():
            decision = self.request(
                payload.get("target_path", ""),
                payload.get("reason", ""),
                payload.get("goal_impact", ""),
                requester=payload.get("requester", "worker"),
                waiting_agent=waiting_agent,
            )
            note = {
                "not_needed": "That path is inside the repository; no approval is needed.",
                "deny": "Denied. Stay inside the repository and complete the unit without this path.",
            }.get(decision, "Approved. The directory is available to worker calls started from now on.")
            answer_outside_request(payload["id"], decision, note)
            answered += 1
        return answered

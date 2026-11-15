"""MAID dashboard: a local web UI over the orchestrator (Kanban board + agent inspector).

Purpose
    `python3 bridge/maid_ui.py` serves a single-page dashboard on 127.0.0.1 that
    starts orchestrator runs and shows them live:
      Zone A  top bar: project, git branch and diff counter, model dropdowns for the
              three agents, new-task input, clarification question cards
      Zone B  Kanban board: Backlog -> In Progress -> Review -> Done, one card per
              atomic unit, synced with the live run and with doc/tasks/*.md
      Zone C  agent status blocks: state, model, target, elapsed time
      Zone D  inspector drawer: STREAM, DIFF (with check details), MESSAGES, TRACES,
              METRICS and SPEC, plus pause, halt, steer and the sandbox permission banner
      Cards   a menu on every card: edit, reassign, skip, rollback, retry, hide, copy
      Footer  a live metrics strip and the quota and telemetry status line

Design
    One process, standard library only. The orchestrator runs in a worker thread and
    talks to the browser through UIControl (a RunControl whose question and
    permission prompts wait for an HTTP request instead of the terminal). Live
    updates go out over Server-Sent Events on /api/stream.

Security
    * Binds to 127.0.0.1 only; this UI can start coding agents, so it must never be
      reachable from the network.
    * Rejects requests whose Host header is not localhost (DNS-rebinding guard).
    * Every state-changing request must carry the per-process token embedded in the
      page (X-MAID-Token), so another website cannot drive the dashboard.
    * All model and agent output is rendered as text, never as HTML.

State mutations
    * Starts orchestrator runs, with everything that implies (see orchestrator.py).
    * Switches models, pauses/halts runs, rolls back units, answers sandbox requests.
    * Edits, pins or skips units that have not started; hides cards of earlier runs.
    * Remembers opened projects in log/ui_projects.json and hidden cards in
      log/ui_hidden.json (both git-ignored).
    * Runs a ResourceSampler thread (one `ps` and one `vm_stat` every two seconds).

Performance
    The dashboard stays out of the model's way: streamed tokens are coalesced into
    one browser event per 100 ms, the page re-renders a zone only when its data
    changed, git and spec lookups are cached for a few seconds, and nothing in the
    page runs more often than once per second.

@rollback
    Stop the server with Ctrl-C; it holds no state beyond the files the orchestrator
    writes. Delete log/ui_projects.json to forget projects. To revert this file,
    `git checkout -- bridge/maid_ui.py`. Standard library only, Python 3.9+.
"""
import argparse
import json
import logging
import queue
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import orchestrator
from maid_common import (
    LOG_DIR,
    REPO_ROOT,
    TELEMETRY,
    ResourceSampler,
    discover_models,
    format_status_line,
    get_correlation_id,
    get_models,
    git,
    git_head,
    git_summary,
    log_event,
    metrics_snapshot,
    set_model,
)
from maid_runtime import DECISIONS, RunControl

DEFAULT_PORT = 8765
TOKEN = secrets.token_urlsafe(24)
PROJECTS_FILE = LOG_DIR / "ui_projects.json"
HIDDEN_FILE = LOG_DIR / "ui_hidden.json"
CHUNK_FLUSH_SECONDS = 0.1
CACHE_SECONDS = 3
TRACE_LIMIT = 400
TRACE_FIELD_LIMIT = 4000
DISCOVERY_TTL = 60
MODEL_AGENTS = {"local": "agent0", "agy": "agy", "claude": "claude"}

logger = logging.getLogger("maid.ui")


# --------------------------------------------------------------------------- #
# Event bus (Server-Sent Events fan-out)
# --------------------------------------------------------------------------- #


class EventBus:
    """Fans events out to every connected browser.

    Each subscriber has a bounded queue; a browser that stops reading loses its
    oldest events rather than blocking a run.

    State mutations: in-memory only.
    @rollback: none needed.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._queues = []

    def subscribe(self):
        """Return a new subscriber queue.

        @rollback: `unsubscribe(queue)`.
        """
        subscriber = queue.Queue(maxsize=2000)
        with self._lock:
            self._queues.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber):
        """Remove a subscriber queue.

        @rollback: none needed.
        """
        with self._lock:
            if subscriber in self._queues:
                self._queues.remove(subscriber)

    def publish(self, kind, payload):
        """Queue one event for every subscriber, dropping the oldest if one is full.

        @rollback: none needed.
        """
        with self._lock:
            subscribers = list(self._queues)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait((kind, payload))
            except queue.Full:
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait((kind, payload))
                except (queue.Empty, queue.Full):
                    pass


class TraceHandler(logging.Handler):
    """Publishes every MAID log record of this process to the browser as a trace.

    @rollback: `logging.getLogger("maid").removeHandler(handler)`.
    """

    def __init__(self, bus):
        super().__init__(level=logging.DEBUG)
        self.bus = bus

    def emit(self, record):
        self.bus.publish("trace", summarise_record(
            {
                "timestamp": time.strftime("%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "correlation_id": get_correlation_id(),
                "event": getattr(record, "event", "log"),
                "logger": record.name,
                "message": record.getMessage(),
                "data": getattr(record, "data", {}),
            }
        ))


def summarise_record(record):
    """Return a log record with each data field capped, safe to send to the browser.

    @rollback: none (pure).
    """
    data = {}
    for key, value in (record.get("data") or {}).items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        data[key] = text if len(text) <= TRACE_FIELD_LIMIT else text[:TRACE_FIELD_LIMIT] + " … (%d more chars)" % (len(text) - TRACE_FIELD_LIMIT)
    return dict(record, data=data)


# --------------------------------------------------------------------------- #
# Run control for the browser
# --------------------------------------------------------------------------- #


class UIControl(RunControl):
    """A RunControl whose prompts are answered from the browser.

    ask_questions() and ask_permission() publish the prompt in the live state and
    block the orchestrator thread until the matching HTTP request arrives or the
    run is halted.

    State mutations: in-memory only.
    @rollback: halting the run releases any pending prompt (answers empty / deny).
    """

    def __init__(self, bus):
        super().__init__()
        self._answers = None
        self._answers_ready = threading.Event()
        self._decision = None
        self._decision_ready = threading.Event()
        self._bus = bus
        self._pending_chunks = []
        self._chunk_lock = threading.Lock()
        self.subscribe(self._forward)
        threading.Thread(target=self._flush_loop, daemon=True, name="maid-chunks").start()

    def _forward(self, kind, payload):
        """Send run events to the browser; stream chunks are buffered for coalescing.

        @rollback: none needed.
        """
        if kind == "chunk":
            with self._chunk_lock:
                last = self._pending_chunks[-1] if self._pending_chunks else None
                if last and last[0] == payload["agent"] and last[1] == payload["stream"]:
                    last[2] += payload["text"]
                else:
                    self._pending_chunks.append([payload["agent"], payload["stream"], payload["text"]])
        else:
            self._bus.publish(kind, light_state(payload) if kind == "state" else payload)

    def flush_chunks(self):
        """Publish the buffered chunks as one "chunks" event; return how many were sent.

        Invariant: the browser gets at most one stream event per CHUNK_FLUSH_SECONDS,
        however fast the model generates.
        @rollback: none needed.
        """
        with self._chunk_lock:
            chunks, self._pending_chunks = self._pending_chunks, []
        if chunks:
            self._bus.publish("chunks", chunks)
        return len(chunks)

    def _flush_loop(self):
        while True:
            time.sleep(CHUNK_FLUSH_SECONDS)
            self.flush_chunks()

    def _wait(self, ready):
        """Block until `ready` is set or the run is halted; return True if it was set."""
        while not ready.wait(0.2):
            if self.halted:
                return False
        return True

    def ask_questions(self, questions):
        """Show Agent 0's questions as cards and wait for the user's answers.

        @rollback: halt the run to release the wait with empty answers.
        """
        self._answers_ready.clear()
        with self._lock:
            self.state["questions"] = list(questions)
        self.set_run(status="awaiting_answers")
        answered = self._wait(self._answers_ready)
        with self._lock:
            self.state["questions"] = None
        self.set_run(status="clarifying")
        return list(self._answers or []) if answered else []

    def submit_answers(self, answers):
        """Deliver the answers posted by the browser.

        @rollback: none needed.
        """
        self._answers = [str(answer) for answer in answers]
        self._answers_ready.set()

    def ask_permission(self, payload):
        """Wait for the user's decision on the sandbox request shown in the banner.

        The request itself is already in the live state (SandboxGate publishes it).
        @rollback: halt the run to release the wait with "deny".
        """
        self._decision_ready.clear()
        self._decision = None
        if not self._wait(self._decision_ready):
            return "deny"
        return self._decision if self._decision in DECISIONS else "deny"

    def submit_permission(self, request_id, decision):
        """Deliver the decision posted by the browser; return False if it does not match.

        @rollback: none needed; `SandboxGate.revoke_all()` drops a grant.
        """
        pending = self.snapshot()["permission"]
        if not pending or pending["id"] != request_id or decision not in DECISIONS:
            return False
        self._decision = decision
        self._decision_ready.set()
        return True


def light_state(state):
    """Strip the bulky parts (prompt texts, diffs) from a state snapshot for SSE.

    The browser fetches those on demand from /api/messages and /api/diff.
    @rollback: none (pure).
    """
    light = dict(state)
    light["messages_count"] = len(state.get("messages") or [])
    light.pop("messages", None)
    light["units"] = [dict(unit, has_diff=bool(unit.get("diff")), diff="") for unit in state.get("units") or []]
    return light


# --------------------------------------------------------------------------- #
# Application state
# --------------------------------------------------------------------------- #


class App:
    """Everything the HTTP handlers share: the project, the run thread, caches.

    State mutations: see the module docstring.
    @rollback: see the module docstring.
    """

    def __init__(self, repo, sample=True):
        self.bus = EventBus()
        self.control = orchestrator.set_control(UIControl(self.bus))
        self.control.set_models(get_models())
        self.repo = Path(repo).resolve()
        self._lock = threading.Lock()
        self._thread = None
        self._discovery = (0, None)
        self._cache = {}
        self.sampler = ResourceSampler() if sample else None
        if self.sampler:
            self.sampler.start()
        logging.getLogger("maid").addHandler(TraceHandler(self.bus))
        self.remember_project(self.repo)

    # -- projects ---------------------------------------------------------- #

    def known_projects(self):
        """Return the remembered project paths, current one first.

        @rollback: none (read-only).
        """
        try:
            known = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            known = []
        known = [path for path in known if isinstance(path, str) and Path(path).is_dir()]
        return [str(self.repo)] + [path for path in known if path != str(self.repo)]

    def remember_project(self, path):
        """Add a project to log/ui_projects.json.

        State mutations: rewrites that file.
        @rollback: delete the file.
        """
        known = [str(path)] + [item for item in self.known_projects() if item != str(path)]
        try:
            PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            PROJECTS_FILE.write_text(json.dumps(known[:20]), encoding="utf-8")
        except OSError:
            pass

    def set_project(self, path):
        """Switch the dashboard to another git repository.

        Raises: ValueError if a run is active or `path` is not a git repo with a commit.
        State mutations: changes the target repo of future runs.
        @rollback: call again with the previous path.
        """
        if self.busy:
            raise ValueError("a run is in progress; halt it before switching project")
        candidate = Path(str(path)).expanduser().resolve()
        if not candidate.is_dir() or not git_head(candidate):
            raise ValueError("%s is not a git repository with at least one commit" % candidate)
        self.repo = candidate
        self._cache.clear()
        self.remember_project(candidate)

    # -- runs -------------------------------------------------------------- #

    @property
    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    def start_task(self, task, options):
        """Start an orchestrator run in a background thread.

        Raises: ValueError if a run is already active or the task is empty.
        State mutations: everything an orchestrator run does.
        @rollback: halt the run; passed units have their own rollback.
        """
        task = str(task or "").strip()
        if not task:
            raise ValueError("the task is empty")
        argv = [task, "--repo", str(self.repo)]
        for flag in ("no_clarify", "no_review", "plan_only"):
            if options.get(flag):
                argv.append("--" + flag.replace("_", "-"))
        if str(options.get("test_cmd") or "").strip():
            argv += ["--test-cmd", str(options["test_cmd"]).strip()]
        with self._lock:
            if self.busy:
                raise ValueError("a run is already in progress")
            self._thread = threading.Thread(target=self._run, args=(argv,), daemon=True, name="maid-run")
            self._thread.start()

    def _run(self, argv):
        try:
            code = orchestrator.main(argv)
            log_event(logger, "ui_run_finished", "run finished with exit code %s" % code, exit_code=code)
        except Exception as error:  # noqa: BLE001 - a crashed run must be reported, not kill the server
            logger.exception("run crashed")
            self.control.set_run(status="failed", error="%s: %s" % (type(error).__name__, error))
        finally:
            self.bus.publish("refresh", {})

    # -- data for the page ------------------------------------------------- #

    def models(self, refresh=False):
        """Return current and available models, cached for DISCOVERY_TTL seconds.

        @rollback: none (read-only; see discover_models()).
        """
        stamp, cached = self._discovery
        if refresh or cached is None or time.time() - stamp > DISCOVERY_TTL:
            cached = discover_models()
            self._discovery = (time.time(), cached)
        current = get_models()
        return {
            agent: dict(info, current=current[agent] or ("default" if agent == "claude" else ""))
            for agent, info in cached.items()
        }

    def _cached(self, key, compute):
        """Return `compute()` cached for CACHE_SECONDS, so polling never repeats git or disk work.

        @rollback: `self._cache.clear()`.
        """
        stamp, value = self._cache.get(key, (0, None))
        if time.time() - stamp > CACHE_SECONDS:
            value = compute()
            self._cache[key] = (time.time(), value)
        return value

    def hidden(self):
        """Return the set of hidden card keys ("<spec name>#<unit id>").

        @rollback: none (read-only).
        """
        try:
            keys = json.loads(HIDDEN_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            keys = []
        return {key for key in keys if isinstance(key, str)}

    def set_hidden(self, key, hidden):
        """Hide or unhide one earlier-run card. The spec file itself is never touched.

        State mutations: rewrites log/ui_hidden.json.
        @rollback: call again with the opposite value, or delete the file.
        """
        keys = self.hidden()
        (keys.add if hidden else keys.discard)(str(key))
        HIDDEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        HIDDEN_FILE.write_text(json.dumps(sorted(keys)), encoding="utf-8")
        self._cache.pop("history", None)

    def history(self, current_spec):
        """Return Kanban cards for the units in doc/tasks/*.md, newest spec first.

        The spec of the run in progress is skipped; its units are live cards. Hidden
        cards are returned with "hidden": true so the page can offer to show them.
        @rollback: none (read-only).
        """

        def load():
            cards = []
            tasks_dir = self.repo / "doc" / "tasks"
            specs = sorted(tasks_dir.glob("*_*.md"), reverse=True) if tasks_dir.is_dir() else []
            for spec in specs[:30]:
                cards += parse_spec(spec.read_text(encoding="utf-8", errors="replace"), spec.name)
            hidden = self.hidden()
            return [dict(card, hidden=card["key"] in hidden) for card in cards]

        current = Path(current_spec).name if current_spec else ""
        return [card for card in self._cached("history", load) if card["spec"] != current]

    def spec(self, name):
        """Return the text of one task spec.

        Raises: ValueError unless `name` is a plain *.md file name that exists in
            doc/tasks/ (no directories, so nothing outside that folder can be read).
        @rollback: none (read-only).
        """
        name = str(name or "")
        path = self.repo / "doc" / "tasks" / name
        if not re.fullmatch(r"[\w.-]+\.md", name) or not path.is_file():
            raise ValueError("no such spec: %s" % name)
        return {"name": name, "text": path.read_text(encoding="utf-8", errors="replace")}

    def metrics(self):
        """Return telemetry, the per-call history and recent resource samples.

        @rollback: none (read-only; no subprocess is started by this call).
        """
        return metrics_snapshot(self.sampler)

    def state(self):
        """Return the full page state.

        @rollback: none (read-only).
        """
        snapshot = self.control.snapshot()
        summary = self._cached("git", lambda: git_summary(self.repo))
        return {
            "project": dict(summary, path=str(self.repo), name=self.repo.name, known=self.known_projects()),
            "run": light_state(snapshot),
            "busy": self.busy,
            "status_line": format_status_line(TELEMETRY.snapshot(), get_models(), summary, snapshot["sandbox"]),
            "history": self.history(snapshot["run"].get("spec_path")),
            "streams": dict(self.control.streams),
        }

    def diff(self, unit_id):
        """Return the diff and rollback command for a unit (or the unit in progress).

        A finished unit returns the diff recorded at verification; a unit still being
        written returns the live `git diff` of its file.
        @rollback: none (read-only).
        """
        units = self.control.snapshot()["units"]
        unit = next((item for item in units if item["id"] == unit_id), None)
        if unit is None:
            active = [item for item in units if item["column"] in ("in_progress", "review")]
            unit = (active or units[-1:] or [None])[0]
        if unit is None:
            return {"unit": "", "file": "", "diff": "", "rollback": "", "live": False}
        if unit.get("diff"):
            return {"unit": unit["id"], "file": unit["file"], "diff": unit["diff"], "rollback": unit.get("rollback", ""), "live": False}
        live = git(self.repo, "diff", "HEAD", "--", unit["file"], check=False).stdout
        if not live and (self.repo / unit["file"]).is_file():
            live = git(self.repo, "diff", "--no-index", "--", "/dev/null", unit["file"], check=False).stdout
        return {"unit": unit["id"], "file": unit["file"], "diff": live, "rollback": unit.get("rollback", ""), "live": True}

    def traces(self, correlation_id):
        """Return the last TRACE_LIMIT records of one correlation ID from the JSONL log.

        Includes records written by other processes of the run (the MCP server).
        @rollback: none (read-only).
        """
        correlation_id = correlation_id or self.control.snapshot()["run"]["id"]
        log_path = LOG_DIR / "maid_execution.jsonl"
        records = []
        if correlation_id and log_path.is_file():
            needle = '"correlation_id": "%s"' % correlation_id
            with log_path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if needle not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    record["timestamp"] = str(record.get("timestamp", ""))[11:19]
                    records.append(summarise_record(record))
        return {"correlation_id": correlation_id, "records": records[-TRACE_LIMIT:]}


_UNIT_HEADING = re.compile(r"^### (U\d+): (\w+) `([^`]+)`")


def parse_spec(text, name):
    """Turn one task spec into Kanban cards.

    Units come from the `### U1: action `file`` headings; their outcome from the
    "Execution results" table. A passed unit lands in Done, anything else in Backlog.

    @rollback: none (pure).
    """
    title, cards = name, {}
    for line in text.splitlines():
        if line.startswith("# Task spec: "):
            title = line[len("# Task spec: "):].strip()
        heading = _UNIT_HEADING.match(line)
        if heading:
            stamp = re.match(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})", name)
            cards[heading.group(1)] = {
                "id": heading.group(1), "action": heading.group(2), "file": heading.group(3), "spec": name,
                "title": title, "agent": "", "status": "planned", "column": "backlog", "rollback": "", "attempt": 0,
                "history": True, "key": "%s#%s" % (name, heading.group(1)),
                "date": "%s-%s-%s %s:%s" % stamp.groups() if stamp else "",
            }
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 6 and cells[0] in cards and line.lstrip().startswith("|"):
            status = cells[2]
            cards[cells[0]].update(
                status=status,
                agent=cells[3],
                attempt=int(cells[4]) if cells[4].isdigit() else 0,
                rollback="|".join(cells[5:]).strip("`") if status == "passed" else "",
                column="done" if status == "passed" else "backlog",
            )
    return list(cards.values())


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


class Handler(BaseHTTPRequestHandler):
    """Serves the page, the REST endpoints and the SSE stream.

    @rollback: none; see the endpoint methods for what each one changes.
    """

    app = None
    server_version = "MAID-UI"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002 - signature fixed by the base class
        """Silence the default per-request stderr logging."""

    # -- helpers ----------------------------------------------------------- #

    def _host_ok(self):
        """DNS-rebinding guard: accept only localhost Host headers."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1")

    def _send(self, status, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, value, status=200):
        self._send(status, json.dumps(value, ensure_ascii=False, default=str))

    def _error(self, status, message):
        self._json({"error": message}, status)

    # -- GET --------------------------------------------------------------- #

    def do_GET(self):  # noqa: N802 - name fixed by the base class
        """Read-only endpoints.

        @rollback: none (read-only).
        """
        if not self._host_ok():
            return self._error(403, "forbidden host")
        url = urlparse(self.path)
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        app = self.app
        if url.path == "/":
            return self._send(200, PAGE.replace("__TOKEN__", TOKEN), "text/html")
        if url.path == "/api/state":
            return self._json(app.state())
        if url.path == "/api/models":
            return self._json(app.models(refresh=query.get("refresh") == "1"))
        if url.path == "/api/messages":
            return self._json(app.control.snapshot()["messages"])
        if url.path == "/api/diff":
            return self._json(app.diff(query.get("unit", "")))
        if url.path == "/api/traces":
            return self._json(app.traces(query.get("cid", "")))
        if url.path == "/api/metrics":
            return self._json(app.metrics())
        if url.path == "/api/spec":
            try:
                return self._json(app.spec(query.get("name", "")))
            except ValueError as error:
                return self._error(404, str(error))
        if url.path == "/api/stream":
            return self._stream()
        return self._error(404, "not found")

    def _stream(self):
        """Server-Sent Events: state, chunk, trace and refresh events until the client leaves."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        subscriber = self.app.bus.subscribe()
        try:
            while True:
                try:
                    kind, payload = subscriber.get(timeout=15)
                    message = "event: %s\ndata: %s\n\n" % (kind, json.dumps(payload, ensure_ascii=False, default=str))
                except queue.Empty:
                    message = ": keep-alive\n\n"
                self.wfile.write(message.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.bus.unsubscribe(subscriber)
            self.close_connection = True

    # -- POST -------------------------------------------------------------- #

    def do_POST(self):  # noqa: N802 - name fixed by the base class
        """State-changing endpoints; each needs the page's X-MAID-Token.

        @rollback: per endpoint, see the App and RunControl methods they call.
        """
        if not self._host_ok():
            return self._error(403, "forbidden host")
        if not secrets.compare_digest(self.headers.get("X-MAID-Token") or "", TOKEN):
            return self._error(403, "missing or wrong token")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            if not isinstance(body, dict):
                raise ValueError("the request body must be a JSON object")
        except ValueError as error:
            return self._error(400, "bad request: %s" % error)
        app, path = self.app, urlparse(self.path).path
        try:
            if path == "/api/task":
                app.start_task(body.get("task"), body)
            elif path == "/api/answer":
                app.control.submit_answers(body.get("answers") or [])
            elif path == "/api/permission":
                if not app.control.submit_permission(body.get("id"), body.get("decision")):
                    return self._error(409, "no matching permission request is pending")
            elif path == "/api/models":
                agent = body.get("agent")
                set_model(agent, body.get("model"))
                app.control.set_models(get_models())
                log_event(logger, "model_switched", "%s model -> %s" % (agent, body.get("model")), agent=agent, model=body.get("model"))
            elif path == "/api/control":
                action = body.get("action")
                if action not in ("pause", "resume", "halt"):
                    return self._error(400, "unknown action")
                getattr(app.control, action)()
                log_event(logger, "run_control", "user requested %s" % action, action=action)
            elif path == "/api/steer":
                if body.get("agent") not in MODEL_AGENTS.values():
                    return self._error(400, "unknown agent")
                app.control.steer(body.get("agent"), body.get("text"))
            elif path == "/api/rollback":
                if app.busy:
                    return self._error(409, "a run is in progress; halt it or wait before rolling back")
                return self._json({"ok": True, "result": orchestrator.rollback_passed_unit(body.get("unit"))})
            elif path == "/api/project":
                app.set_project(body.get("path"))
            elif path == "/api/unit":
                card = app.control.edit_unit(
                    body.get("id"), description=body.get("description"),
                    pinned_agent=body.get("pinned_agent"), skip=body.get("skip"),
                )
                return self._json({"ok": True, "unit": card})
            elif path == "/api/hide":
                if not re.fullmatch(r"[\w.-]+\.md#U\d+", str(body.get("key") or "")):
                    return self._error(400, "bad card key")
                app.set_hidden(body["key"], bool(body.get("hidden", True)))
            else:
                return self._error(404, "not found")
        except KeyError as error:
            return self._error(400, "unknown %s" % error)
        except ValueError as error:
            return self._error(409, str(error))
        return self._json({"ok": True})


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MAID</title>
<style>
:root{--bg:#0d1014;--panel:#151a21;--panel2:#1b222b;--line:#28313c;--text:#d7dde5;--muted:#8793a2;--accent:#6cb6ff;
--ok:#56d364;--warn:#e3b341;--bad:#f85149;--violet:#b392f0;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--text);font:13px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;
display:flex;flex-direction:column;height:100vh;height:100dvh;overflow:hidden}
button,input,select,textarea{font:inherit;color:inherit}
button{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:5px 10px;cursor:pointer}
button:hover{border-color:var(--accent)}
button:disabled{opacity:.45;cursor:default;border-color:var(--line)}
button.primary{background:#1f6feb;border-color:#1f6feb;color:#fff}
button.danger{border-color:#6e2b2b;color:var(--bad)}
button.ghost{background:none;border-color:transparent;padding:0 6px;color:var(--muted);line-height:1.2}
button.ghost:hover{color:var(--text);border-color:var(--line)}
input[type=text],select,textarea{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:5px 8px;min-width:0}
textarea{resize:vertical;width:100%;line-height:1.4}
input[type=text]:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent)}
label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.mono{font-family:var(--mono)}
.muted{color:var(--muted)}
[hidden]{display:none!important}
#top{flex:none;background:var(--panel);border-bottom:1px solid var(--line);padding:10px 14px;display:flex;flex-wrap:wrap;gap:10px 16px;align-items:end}
#top .field{display:flex;flex-direction:column;gap:3px;min-width:0}
#top .brand{font-weight:700;letter-spacing:.12em;font-size:15px;align-self:center;margin-right:4px}
#project{width:200px}
.chip{display:inline-flex;gap:6px;align-items:center;background:var(--bg);border:1px solid var(--line);border-radius:999px;padding:4px 10px;font-family:var(--mono);font-size:12px;white-space:nowrap}
.add{color:var(--ok)}.del{color:var(--bad)}
#task{flex:1 1 100%;display:flex;gap:8px;align-items:end}
#task .field{flex:1}
#task input[type=text]{width:100%}
.opt{display:flex;gap:5px;align-items:center;color:var(--muted);white-space:nowrap;padding-bottom:6px}
#notice{flex:none;display:flex;flex-direction:column;max-height:45vh;overflow:auto}
.banner{padding:10px 14px;border-bottom:1px solid var(--line);background:#2a2110;display:flex;flex-direction:column;gap:8px}
.banner.perm{background:#2b1717}
.banner.bar{background:var(--panel2);flex-direction:row;align-items:center;gap:14px;padding:6px 14px;flex-wrap:wrap}
.banner.bar.ok{background:#12261a}.banner.bar.bad{background:#2b1717}
.banner h3{margin:0;font-size:13px}
.banner dl{margin:0;display:grid;grid-template-columns:max-content 1fr;gap:3px 12px}
.banner dt{color:var(--muted);font-family:var(--mono);font-size:12px}
.banner dd{margin:0;overflow-wrap:anywhere}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.grow{flex:1}
.q{display:flex;flex-direction:column;gap:4px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--warn);display:inline-block}
#main{flex:1;min-height:0;display:flex}
#left{flex:1;min-width:0;display:flex;flex-direction:column}
#board{flex:1;min-height:0;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;padding:12px 14px;overflow:auto}
.col{background:var(--panel);border:1px solid var(--line);border-radius:8px;display:flex;flex-direction:column;min-height:0;min-width:0}
.col h2{margin:0;padding:9px 12px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--line);display:flex;justify-content:space-between}
.cards{padding:8px;display:flex;flex-direction:column;gap:8px;overflow:auto;min-height:0}
.card{background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:9px 10px;cursor:pointer;display:flex;flex-direction:column;gap:5px;min-width:0}
.card:hover,.card.sel{border-color:var(--accent)}
.card.hist{opacity:.8}
.card.dim{opacity:.45}
.card .head{display:flex;justify-content:space-between;gap:6px;align-items:center}
.card .id{font-family:var(--mono);font-weight:700}
.card .file{font-family:var(--mono);font-size:12px;overflow-wrap:anywhere}
.card .cmd{font-family:var(--mono);font-size:11px;color:var(--muted);overflow-wrap:anywhere}
.card .from{font-size:11px;color:var(--muted)}
.tag{font-size:11px;border:1px solid var(--line);border-radius:4px;padding:0 6px;white-space:nowrap}
.tag.agy{color:var(--violet);border-color:#4a3b78}.tag.claude{color:#f0a36c;border-color:#6b4526}
.st-ok{color:var(--ok)}.st-bad{color:var(--bad)}.st-run{color:var(--warn)}
.empty{color:var(--muted);padding:6px 4px;font-size:12px}
details.earlier>summary{cursor:pointer;color:var(--muted);font-size:11px;letter-spacing:.06em;text-transform:uppercase;padding:4px 2px}
details.earlier>div{display:flex;flex-direction:column;gap:8px;margin-top:6px}
#boardbar{flex:none;padding:0 14px 8px;display:flex;gap:14px;color:var(--muted);font-size:12px;align-items:center}
#drawer{flex:none;width:min(46vw,660px);border-left:1px solid var(--line);background:var(--panel);display:none;flex-direction:column;min-height:0}
body.open #drawer{display:flex}
#drawer header{padding:10px 12px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:8px}
#drawer header b{overflow-wrap:anywhere}
.tabs{display:flex;border-bottom:1px solid var(--line);flex:none}
.tabs button{flex:1;border:0;border-radius:0;background:none;padding:8px 2px;font-size:11px;letter-spacing:.05em;color:var(--muted);border-bottom:2px solid transparent;white-space:nowrap}
.tabs button.on{color:var(--text);border-bottom-color:var(--accent)}
#pane{flex:1;overflow:auto;min-height:0;padding:10px 12px}
#pane pre{margin:0;font:12px/1.5 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere}
.s-reasoning{color:var(--muted)}.s-stderr{color:#e9967a}.s-tool_call{color:var(--violet)}.s-info{color:var(--accent)}.s-noise{color:#5d6876}
.d-add{color:var(--ok)}.d-del{color:var(--bad)}.d-hunk{color:var(--accent)}.d-meta{color:var(--muted)}
.box{border:1px solid var(--line);border-radius:6px;margin-bottom:8px;background:var(--panel2)}
.box summary{padding:6px 9px;cursor:pointer;display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.box pre{padding:8px 9px;border-top:1px solid var(--line)}
.box h4{margin:0;padding:6px 9px 0;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.lv-ERROR{color:var(--bad)}.lv-WARNING{color:var(--warn)}.lv-DEBUG{color:var(--muted)}
.sect{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:10px 0 6px}
.kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px}
.kpi{border:1px solid var(--line);border-radius:6px;padding:7px 9px;background:var(--panel2)}
.kpi b{display:block;font:600 16px var(--mono)}
.kpi span{font-size:11px;color:var(--muted)}
.spark{width:100%;height:36px;color:var(--accent);display:block}
table.calls{width:100%;border-collapse:collapse;font:12px var(--mono)}
table.calls th,table.calls td{text-align:left;padding:3px 6px;border-bottom:1px solid var(--line);white-space:nowrap}
table.calls td.p{white-space:normal;overflow-wrap:anywhere}
#steer{border-top:1px solid var(--line);padding:10px 12px;display:flex;flex-direction:column;gap:8px;flex:none}
#steer input{flex:1}
#deck{flex:none;border-top:1px solid var(--line);background:var(--panel);padding:10px 14px;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
.agent{border:1px solid var(--line);border-radius:8px;padding:9px 11px;background:var(--panel2);cursor:pointer;display:flex;flex-direction:column;gap:4px;min-width:0}
.agent:hover,.agent.sel{border-color:var(--accent)}
.agent .title{font-size:11px;letter-spacing:.06em;font-weight:700}
.agent .line{display:flex;justify-content:space-between;gap:8px}
.agent .target{font-family:var(--mono);font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.state{font-family:var(--mono);font-weight:700}
.state.idle{color:var(--muted)}.state.thinking{color:var(--accent)}.state.coding{color:var(--ok)}.state.reviewing{color:var(--violet)}.state.awaiting_permission{color:var(--bad)}
#strip{flex:none;border-top:1px solid var(--line);background:var(--panel);padding:5px 14px;font:12px var(--mono);color:var(--muted);display:flex;gap:18px;flex-wrap:wrap;cursor:pointer}
#strip b{color:var(--text);font-weight:600}
#foot{flex:none;border-top:1px solid var(--line);background:#0a0c0f;padding:6px 14px;font:12px/1.6 var(--mono);color:var(--muted);overflow-wrap:anywhere}
#toast{position:fixed;left:50%;bottom:64px;transform:translateX(-50%);background:#2b1717;border:1px solid #6e2b2b;color:#ffd7d5;border-radius:6px;padding:8px 14px;display:none;max-width:80vw;z-index:30}
#toast.good{background:#12261a;border-color:#2c6a3c;color:#d3f5dc}
.menu{position:fixed;z-index:20;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:4px;display:flex;flex-direction:column;min-width:200px;box-shadow:0 8px 24px #0009}
.menu button{border:0;background:none;text-align:left;border-radius:5px;padding:6px 10px}
.menu button:hover{background:var(--panel2)}
.menu hr{border:0;border-top:1px solid var(--line);margin:4px 0;width:100%}
.modal{position:fixed;inset:0;background:#0009;z-index:25;display:flex;align-items:center;justify-content:center;padding:16px}
.dialog{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;width:min(640px,100%);display:flex;flex-direction:column;gap:10px}
.dialog h3{margin:0;font-size:14px;overflow-wrap:anywhere}
@media (max-width:900px){body{height:auto;overflow:auto}#main{flex-direction:column}#board{grid-template-columns:1fr 1fr}#deck{grid-template-columns:1fr}#drawer{width:auto;border-left:0;border-top:1px solid var(--line);max-height:70vh}}
</style>
</head>
<body>
<div id="top">
  <div class="brand">MAID</div>
  <div class="field"><label for="project">Project</label><input type="text" id="project" list="projects" spellcheck="false"><datalist id="projects"></datalist></div>
  <div class="field"><label>Branch</label><span class="chip"><span id="branch">?</span><span class="add" id="added">+0</span><span class="del" id="deleted">-0</span></span></div>
  <div class="field"><label for="m-local">Agent 0 Local</label><select id="m-local"></select></div>
  <div class="field"><label for="m-agy">Agent 1 agy</label><select id="m-agy"></select></div>
  <div class="field"><label for="m-claude">Agent 2 claude</label><select id="m-claude"></select></div>
  <form id="task">
    <div class="field"><label for="task-text">New task</label><input type="text" id="task-text" placeholder="Describe the task for Agent 0…  (press / to focus)" autocomplete="off"></div>
    <label class="opt"><input type="checkbox" id="no-clarify"> skip questions</label>
    <button class="primary" id="start">Start</button>
    <button type="button" class="danger" id="top-halt" hidden>Halt</button>
  </form>
</div>
<div id="notice"><div id="n-run"></div><div id="n-perm"></div><div id="n-questions"></div></div>
<div id="main">
  <div id="left">
    <div id="board"></div>
    <div id="boardbar"><label class="opt" style="padding:0;text-transform:none;letter-spacing:0;font-size:12px"><input type="checkbox" id="show-hidden"> show hidden cards</label><span id="hidden-count"></span></div>
    <div id="deck"></div>
  </div>
  <aside id="drawer">
    <header><b id="d-title">Inspector</b><span class="row"><span class="muted" id="d-run"></span><button id="d-close" title="Esc">Close</button></span></header>
    <div class="tabs">
      <button data-tab="stream" title="TERMINAL / STREAM">STREAM</button><button data-tab="diff" title="GIT DIFF">DIFF</button><button data-tab="messages">MESSAGES</button>
      <button data-tab="traces">TRACES</button><button data-tab="metrics">METRICS</button><button data-tab="spec">SPEC</button>
    </div>
    <div id="pane"></div>
    <div id="steer">
      <div class="row"><button id="pause">Pause</button><button id="halt" class="danger">Halt</button><span class="muted" id="ctl-note"></span></div>
      <form class="row" id="steer-form"><input type="text" id="steer-text" placeholder="steer this agent — injected as context on next turn/retry" autocomplete="off"><button>Steer</button></form>
    </div>
  </aside>
</div>
<div id="strip" title="Open metrics"></div>
<div id="foot">…</div>
<div id="toast"></div>
<script>
"use strict";
const TOKEN = "__TOKEN__";
const COLUMNS = [["backlog","Backlog"],["in_progress","In Progress"],["review","Review"],["done","Done"]];
const AGENTS = ["agent0","agy","claude"];
const MODEL_KEYS = {local:"m-local", agy:"m-agy", claude:"m-claude"};
const TABS = ["stream","diff","messages","traces","metrics","spec"];
const NOISE = /workspace has not been trusted|hasTrustDialogAccepted|unrecognized_model|Ignoring \d+ permissions/;
const $ = id => document.getElementById(id);

/* ---- small helpers ---- */
function store(key, value){
  try { if (value === undefined) return localStorage.getItem("maid."+key); localStorage.setItem("maid."+key, value); } catch (e) { return null; }
}
function h(tag, attrs, ...kids){
  const el = document.createElement(tag);
  for (const [k,v] of Object.entries(attrs||{})){
    if (k === "class") el.className = v; else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}
function toast(text, good){ const t=$("toast"); t.textContent=text; t.className=good?"good":""; t.style.display="block"; clearTimeout(toast.t); toast.t=setTimeout(()=>t.style.display="none",4000); }
async function api(path, body){
  const res = await fetch(path, body === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json","X-MAID-Token":TOKEN}, body:JSON.stringify(body)});
  const data = await res.json().catch(()=>({error:"bad response"}));
  if (!res.ok){ toast(data.error || res.statusText); throw new Error(data.error); }
  return data;
}
function copy(text){
  const done = () => toast("Copied: "+text, true);
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, ()=>toast("Could not copy"));
  else toast("Clipboard unavailable: "+text);
}
function elapsed(since){ const s=Math.max(0,Math.floor(Date.now()/1000-since)); return s<60 ? s+"s" : Math.floor(s/60)+"m "+(s%60)+"s"; }
const sig = {};
/* Render guard: a zone is rebuilt only when the data it shows has changed. */
function changed(zone, value){ const key = JSON.stringify(value); if (sig[zone] === key) return false; sig[zone] = key; return true; }

/* ---- state ---- */
let S = null;                                   // last /api/state
let sel = {agent:"agent0", unit:"", spec:""};
let tab = TABS.includes(store("tab")) ? store("tab") : "stream";
let showHidden = store("hidden") === "1";
let dismissedRun = "";
let metrics = null;
const streams = {agent0:[], agy:[], claude:[]};  // [kind, text] pairs per agent
const run = () => S.run;
const drawerOpen = () => document.body.classList.contains("open");
const specName = path => (path||"").split("/").pop();
function agentTitle(key){
  const a = run().agents[key];
  return key==="agent0" ? "AGENT 0 ("+(a.model||"local").split("/").pop().toUpperCase()+" - BRAIN)" : a.title;
}

/* ---- Zone A: top bar ---- */
function renderTop(){
  const p = S.project;
  if (changed("foot", S.status_line)) $("foot").textContent = S.status_line;
  if (!changed("top", [p, S.busy])) return;
  const box = $("project");
  if (document.activeElement !== box) box.value = p.name;
  box.title = p.path;
  $("projects").replaceChildren(...p.known.map(path => h("option",{value:path})));
  $("branch").textContent = "🌿 "+p.branch; $("added").textContent = "+"+p.added; $("deleted").textContent = "-"+p.deleted;
  $("start").disabled = S.busy; $("start").textContent = S.busy ? "Running…" : "Start";
  $("task-text").disabled = S.busy; $("top-halt").hidden = !S.busy;
}
async function loadModels(refresh){
  const models = await api("/api/models"+(refresh?"?refresh=1":""));
  for (const [agent,id] of Object.entries(MODEL_KEYS)){
    const info = models[agent], box = $(id);
    box.replaceChildren(...info.models.map(m => h("option",{value:m, selected:m===info.current}, m)));
    box.title = info.error || info.source;
  }
}

/* ---- notices: run bar, sandbox permission, clarifying questions ---- */
function renderRunBar(){
  const r = run(), units = r.units, done = units.filter(u=>u.column==="done").length;
  if (!changed("runbar", [r.run.id, r.run.status, r.run.error, S.busy, done, units.length, dismissedRun, r.paused])) return;
  let bar = null;
  if (r.run.id && S.busy){
    bar = h("div",{class:"banner bar"}, h("span",{class:"dot"}), h("b",{},"Run "+r.run.id), h("span",{}, r.paused ? "paused" : r.run.status.replace(/_/g," ")),
      h("span",{class:"mono", "data-since":r.run.started_at}, elapsed(r.run.started_at)),
      units.length ? h("span",{}, "units "+done+"/"+units.length) : null,
      h("span",{class:"muted grow"}, r.run.task));
  } else if (r.run.id && r.run.id !== dismissedRun && ["done","failed","halted"].includes(r.run.status)){
    const failed = units.filter(u=>/fail/.test(u.status)).length;
    bar = h("div",{class:"banner bar "+(r.run.status==="done"?"ok":"bad")}, h("b",{},"Run "+r.run.id+" "+r.run.status),
      units.length ? h("span",{}, done+" passed, "+failed+" failed of "+units.length) : null,
      r.run.error ? h("span",{class:"grow"}, r.run.error) : h("span",{class:"grow"}),
      h("button",{onclick:()=>{ tab="traces"; openDrawer(); }},"View traces"),
      h("button",{onclick:()=>{ dismissedRun=r.run.id; renderRunBar(); }},"Dismiss"));
  }
  $("n-run").replaceChildren(...(bar?[bar]:[]));
}
function renderPerm(){
  const p = run().permission;
  if (!changed("perm", p)) return;
  $("n-perm").replaceChildren(...(p ? [h("div",{class:"banner perm"},
    h("h3",{},"🔒 Sandbox permission request from "+p.requester),
    h("dl",{}, h("dt",{},"target_path"), h("dd",{class:"mono"},p.target_path), h("dt",{},"reason"), h("dd",{},p.reason), h("dt",{},"goal_impact"), h("dd",{},p.goal_impact)),
    h("div",{class:"row"}, ...p.actions.map(a => h("button",{class:a.decision==="deny"?"danger":"", onclick:()=>api("/api/permission",{id:p.id, decision:a.decision})}, a.label))))] : []));
}
function renderQuestions(){
  const q = run().questions;
  if (!changed("questions", q)) return;          // never rebuilt while you are typing
  if (!q){ $("n-questions").replaceChildren(); return; }
  const inputs = [];
  const send = () => api("/api/answer",{answers:inputs.map(i=>i.value)});
  const form = h("form",{class:"banner", onsubmit:e=>{e.preventDefault(); send();}},
    h("h3",{},"Agent 0 needs "+q.length+" clarification"+(q.length>1?"s":"")+" before planning"),
    ...q.map((text,i)=>{ const input=h("textarea",{rows:2, placeholder:"Your answer (leave blank to let Agent 0 decide)"}); inputs.push(input);
      input.addEventListener("keydown", e=>{ if (e.key==="Enter" && (e.metaKey||e.ctrlKey)){ e.preventDefault(); send(); } });
      return h("div",{class:"q"}, h("span",{},(i+1)+". "+text), input); }),
    h("div",{class:"row"}, h("button",{class:"primary"},"Send answers"), h("button",{type:"button", onclick:()=>api("/api/answer",{answers:[]})},"Let Agent 0 decide all"), h("span",{class:"muted"},"⌘/Ctrl + Enter sends")));
  $("n-questions").replaceChildren(form);
  if (inputs[0]) inputs[0].focus();
}

/* ---- Zone B: Kanban ---- */
function statusClass(st){ return /passed|verified/.test(st) ? "st-ok" : /fail|halt|rolled/.test(st) ? "st-bad" : /pending|planned|not run|skipped/.test(st) ? "muted" : "st-run"; }
function selectCard(u){
  sel = {agent: AGENTS.includes(u.agent) ? u.agent : sel.agent, unit: u.history ? "" : u.id, spec: u.history ? u.spec : specName(run().run.spec_path), card: u.history ? u.key : u.id};
  if (u.history) tab = "spec";
  openDrawer();
}
function cardMenu(u){
  const items = [];
  if (!u.history){
    if (u.column==="backlog" && (u.status==="pending" || u.status==="skipped")){
      items.push(["Edit description…", ()=>editUnit(u)]);
      items.push(["Assign to agy", ()=>api("/api/unit",{id:u.id, pinned_agent:"agy"})]);
      items.push(["Assign to claude", ()=>api("/api/unit",{id:u.id, pinned_agent:"claude"})]);
      items.push(["Let Agent 0 choose", ()=>api("/api/unit",{id:u.id, pinned_agent:""})]);
      items.push(u.status==="skipped" ? ["Restore unit", ()=>api("/api/unit",{id:u.id, skip:false})] : ["Skip (delete from this run)", ()=>api("/api/unit",{id:u.id, skip:true}), "danger"]);
      items.push(["-"]);
    }
    if (u.column==="done" && u.rollback) items.push(["Rollback unit", ()=>api("/api/rollback",{unit:u.id}).then(refresh), "danger"]);
    if (/fail|rolled|halt/.test(u.status)) items.push(["Retry as new task", ()=>{ $("task-text").value = u.description || (u.action+" "+u.file); $("task-text").focus(); }]);
    items.push(["Open diff and checks", ()=>{ selectCard(u); tab="diff"; renderDrawer(true); }]);
  } else {
    items.push(["Open spec", ()=>selectCard(u)]);
    items.push(u.hidden ? ["Unhide card", ()=>api("/api/hide",{key:u.key, hidden:false}).then(refresh)] : ["Hide card", ()=>api("/api/hide",{key:u.key, hidden:true}).then(refresh), "danger"]);
  }
  if (u.rollback) items.push(["Copy rollback command", ()=>copy(u.rollback)]);
  items.push(["Copy file path", ()=>copy(u.file)]);
  return items;
}
function card(u){
  const live = !u.history, checks = (u.checks||[]).map(c => (c.ok?"✓ ":"✗ ")+c.name).join("  ");
  const selected = sel.card === (live ? u.id : u.key) && drawerOpen();
  return h("div",{class:"card"+(live?"":" hist")+(u.hidden||u.status==="skipped"?" dim":"")+(selected?" sel":""), onclick:()=>selectCard(u)},
    h("div",{class:"head"}, h("span",{class:"id"},u.id),
      h("span",{class:"row"}, u.pinned_agent ? h("span",{class:"tag", title:"pinned by you"},"📌") : null, u.agent ? h("span",{class:"tag "+u.agent},u.agent) : null,
        h("button",{class:"ghost", title:"Card actions", onclick:e=>{ e.stopPropagation(); openMenu(cardMenu(u), e.currentTarget); }},"⋯"))),
    h("div",{class:"file"}, u.action+" "+u.file),
    h("div",{class:statusClass(u.status)}, u.status+(u.attempt>1?" (attempt "+u.attempt+")":"")),
    checks ? h("div",{class:"muted mono"},checks) : null,
    !live ? h("div",{class:"from"}, u.title+(u.date?" · "+u.date:"")) : null,
    u.rollback ? h("div",{class:"cmd"},u.rollback) : null);
}
function renderBoard(){
  const r = run(), earlierOpen = store("earlier") !== "0";
  if (!changed("board", [r.units, S.history, sel.card, drawerOpen(), S.busy, showHidden, earlierOpen])) return;
  const history = S.history.filter(u => showHidden || !u.hidden);
  const hiddenCount = S.history.filter(u => u.hidden).length;
  $("hidden-count").textContent = hiddenCount ? hiddenCount+" hidden" : "";
  $("board").replaceChildren(...COLUMNS.map(([key,title])=>{
    const live = r.units.filter(u=>u.column===key), old = history.filter(u=>u.column===key);
    const kids = live.map(card);
    if (old.length){
      const group = h("details",{class:"earlier", open:earlierOpen}, h("summary",{},"Earlier runs ("+old.length+")"), h("div",{}, old.map(card)));
      group.addEventListener("toggle", ()=>{ store("earlier", group.open ? "1" : "0"); });
      kids.push(group);
    }
    return h("section",{class:"col"}, h("h2",{},h("span",{},title),h("span",{},live.length+old.length)),
      h("div",{class:"cards"}, kids.length ? kids : h("div",{class:"empty"},"—")));
  }));
}

/* ---- card menu and edit dialog ---- */
let menuEl = null;
function closeMenu(){ if (menuEl){ menuEl.remove(); menuEl = null; } }
function openMenu(items, anchor){
  closeMenu();
  menuEl = h("div",{class:"menu"}, items.map(([label,fn,cls]) => label==="-" ? h("hr") : h("button",{class:cls||"", onclick:()=>{ closeMenu(); fn(); }}, label)));
  document.body.append(menuEl);
  const box = anchor.getBoundingClientRect();
  menuEl.style.top = Math.min(box.bottom+4, innerHeight-menuEl.offsetHeight-8)+"px";
  menuEl.style.left = Math.max(8, Math.min(box.left-160, innerWidth-220))+"px";
}
function closeModal(){ const m = document.querySelector(".modal"); if (m) m.remove(); return !!m; }
function editUnit(u){
  const text = h("textarea",{rows:9}, u.description||"");
  const dialog = h("div",{class:"modal", onclick:e=>{ if (e.target===dialog) dialog.remove(); }},
    h("form",{class:"dialog", onsubmit:e=>{ e.preventDefault(); api("/api/unit",{id:u.id, description:text.value}).then(()=>{ dialog.remove(); toast(u.id+" updated", true); }); }},
      h("h3",{},"Edit "+u.id+" · "+u.action+" "+u.file), h("span",{class:"muted"},"Agent 0 turns this description into the worker's instructions when the unit starts."),
      text, h("div",{class:"row"}, h("button",{class:"primary"},"Save"), h("button",{type:"button", onclick:()=>dialog.remove()},"Cancel"))));
  document.body.append(dialog); text.focus();
}

/* ---- Zone C: agent blocks ---- */
function renderDeck(){
  if (!changed("deck", [run().agents, sel.agent, drawerOpen()])) return;
  $("deck").replaceChildren(...AGENTS.map(key=>{
    const a = run().agents[key];
    return h("div",{class:"agent"+(sel.agent===key&&drawerOpen()?" sel":""), onclick:()=>{ sel={agent:key, unit:"", spec:specName(run().run.spec_path), card:""}; if (tab==="spec" && !sel.spec) tab="stream"; openDrawer(); }},
      h("div",{class:"title"}, agentTitle(key)),
      h("div",{class:"line"}, h("span",{class:"state "+a.state}, a.state), h("span",{class:"tag"}, a.model||"default")),
      h("div",{class:"line"}, h("span",{class:"target"}, a.target||"—"), h("span",{class:"muted mono", "data-since":a.since}, elapsed(a.since))));
  }));
}

/* ---- Zone D: inspector ---- */
function openDrawer(){ document.body.classList.add("open"); store("drawer","1"); renderDeck(); renderBoard(); renderDrawer(true); }
function closeDrawer(){ document.body.classList.remove("open"); store("drawer","0"); renderDeck(); renderBoard(); }
function renderDrawer(reload){
  if (!drawerOpen()) return;
  const r = run();
  if (tab==="spec" && !sel.spec) tab = "stream";
  if (changed("drawer", [sel, tab, r.run.id, r.run.status, r.paused, S.busy, r.agents[sel.agent].model])){
    $("d-title").textContent = (sel.unit ? sel.unit+" · " : "") + agentTitle(sel.agent);
    $("d-run").textContent = r.run.id ? "run "+r.run.id+" · "+r.run.status : "";
    $("pause").textContent = r.paused ? "Resume" : "Pause";
    $("pause").disabled = $("halt").disabled = !S.busy;
    $("ctl-note").textContent = r.paused ? "paused: takes effect between agent calls" : "";
    document.querySelectorAll(".tabs button").forEach(b=>{ b.classList.toggle("on", b.dataset.tab===tab); if (b.dataset.tab==="spec") b.hidden = !sel.spec; });
  }
  if (reload){ store("tab", tab); ({stream:paintStream, diff:loadDiff, messages:loadMessages, traces:loadTraces, metrics:loadMetrics, spec:loadSpec})[tab](); }
}

/* stream: appended incrementally, never rebuilt per token */
let streamPre = null, streamAgent = "", pendingChunks = [], frame = 0;
function paintStream(){
  streamAgent = sel.agent; pendingChunks = [];
  streamPre = h("pre",{}, ...streams[sel.agent].map(([kind,text]) => h("span",{class:"s-"+kind}, text)));
  if (!streams[sel.agent].length) streamPre.append(h("span",{class:"muted", id:"no-output"}, "No output from this agent yet."));
  $("pane").replaceChildren(streamPre); $("pane").scrollTop = $("pane").scrollHeight;
}
function flushChunks(){
  frame = 0;
  if (tab!=="stream" || !drawerOpen() || !streamPre){ pendingChunks = []; return; }
  const pane = $("pane"), stick = pane.scrollTop + pane.clientHeight >= pane.scrollHeight - 60;
  const placeholder = $("no-output"); if (placeholder && placeholder.remove) placeholder.remove();
  for (const [kind,text] of pendingChunks){
    const last = streamPre.lastChild;
    if (last && last.className === "s-"+kind) last.append(text); else streamPre.append(h("span",{class:"s-"+kind}, text));
  }
  pendingChunks = [];
  if (streamPre.childNodes.length > 1500) paintStream(); else if (stick) pane.scrollTop = pane.scrollHeight;
}
function addChunk(agent, kind, text){
  const list = streams[agent], last = list[list.length-1];
  if (last && last[0]===kind) last[1] += text; else list.push([kind,text]);
  if (list.length > 900) list.splice(0, list.length-700);
  if (tab==="stream" && drawerOpen() && streamAgent===agent){
    pendingChunks.push([kind,text]);
    if (!frame) frame = requestAnimationFrame(flushChunks);
  }
}
function pushChunk(agent, kind, text){
  if (!streams[agent] || !text) return;
  if (kind !== "stderr") return addChunk(agent, kind, text);
  for (const line of text.match(/[^\n]*\n?/g)) if (line) addChunk(agent, NOISE.test(line) ? "noise" : "stderr", line);   // known harmless CLI warnings are dimmed
}

async function loadDiff(){
  const d = await api("/api/diff?unit="+encodeURIComponent(sel.unit));
  if (tab!=="diff") return;
  const unit = run().units.find(u => u.id === d.unit);
  const lines = (d.diff||"").split("\n").map(line => h("span",{class: line.startsWith("@@")?"d-hunk": /^(\+\+\+|---|diff |index )/.test(line)?"d-meta": line[0]==="+"?"d-add": line[0]==="-"?"d-del":""}, line+"\n"));
  const checks = unit && unit.checks && unit.checks.length ? [h("div",{class:"sect"},"Checks"), ...unit.checks.map(c => h("details",{class:"box", open:!c.ok},
    h("summary",{}, h("b",{class:c.ok?"st-ok":"st-bad"}, (c.ok?"✓ ":"✗ ")+c.name), h("span",{class:"muted"}, c.detail.split("\n")[0].slice(0,110))), h("pre",{}, c.detail)))] : [];
  $("pane").replaceChildren(
    h("div",{class:"muted"}, d.file ? (d.unit+" · "+d.file+(d.live?" · live working-tree diff":" · diff recorded at verification")) : "No unit selected yet. Click a card on the board."),
    d.rollback ? h("div",{class:"row", style:"margin-top:8px"}, h("span",{class:"cmd mono grow"}, "rollback: "+d.rollback), h("button",{onclick:()=>copy(d.rollback)},"Copy")) : null,
    ...checks, h("div",{class:"sect"},"Diff"),
    h("pre",{}, d.diff ? lines : h("span",{class:"muted"},"No changes to this file yet.")));
}
async function loadMessages(){
  const all = await api("/api/messages");
  if (tab!=="messages") return;
  const mine = all.filter(m => m.to===sel.agent || m.from===sel.agent).reverse();
  $("pane").replaceChildren(...(mine.length ? mine.map(m => h("details",{class:"box"},
    h("summary",{}, h("b",{}, m.from+" → "+m.to), h("span",{class:"muted"}, m.purpose), h("span",{class:"muted mono"}, new Date(m.at*1000).toLocaleTimeString())),
    h("h4",{},"Prompt"), h("pre",{}, m.prompt), h("h4",{},"Raw response"), h("pre",{}, m.response==null ? "(waiting…)" : m.response))) : [h("div",{class:"muted"},"No messages for this agent yet.")]));
}
function traceRow(t){
  return h("details",{class:"box"}, h("summary",{}, h("span",{class:"muted mono"},t.timestamp), h("b",{class:"lv-"+t.level},t.event), h("span",{},t.message)),
    h("pre",{}, Object.entries(t.data||{}).map(([k,v])=>k+": "+v).join("\n") || "(no data)"));
}
async function loadTraces(){
  const res = await api("/api/traces");
  if (tab!=="traces") return;
  $("pane").replaceChildren(h("div",{class:"muted", style:"margin-bottom:8px"}, res.correlation_id ? "correlation_id "+res.correlation_id+" · "+res.records.length+" records from log/maid_execution.jsonl" : "No run yet."),
    ...res.records.slice().reverse().map(traceRow));
}
async function loadSpec(){
  if (!sel.spec) return;
  const res = await api("/api/spec?name="+encodeURIComponent(sel.spec));
  if (tab!=="spec") return;
  $("pane").replaceChildren(h("div",{class:"row", style:"margin-bottom:8px"}, h("span",{class:"muted mono grow"}, "doc/tasks/"+res.name), h("button",{onclick:()=>copy("doc/tasks/"+res.name)},"Copy path")), h("pre",{}, res.text));
}

/* ---- metrics: strip above the footer and the METRICS tab ---- */
function spark(values){
  const ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns,"svg");
  svg.setAttribute("viewBox","0 0 220 36"); svg.setAttribute("preserveAspectRatio","none"); svg.setAttribute("class","spark");
  if (values.length > 1){
    const max = Math.max(...values), min = Math.min(...values, 0), span = (max-min) || 1;
    const line = document.createElementNS(ns,"polyline");
    line.setAttribute("points", values.map((v,i)=>(i*220/(values.length-1)).toFixed(1)+","+(34-(v-min)/span*32).toFixed(1)).join(" "));
    line.setAttribute("fill","none"); line.setAttribute("stroke","currentColor"); line.setAttribute("stroke-width","1.5"); line.setAttribute("vector-effect","non-scaling-stroke");
    svg.append(line);
  }
  return svg;
}
const gb = mb => (mb/1024).toFixed(1)+" GB";
function metricFacts(){
  const t = metrics.telemetry, last = metrics.samples[metrics.samples.length-1] || {groups:{lmstudio:{cpu:0,rss_mb:0}}, memory:{}, load1:0};
  const since = run().run.started_at || 0, mine = metrics.calls.filter(c => c.at >= since);
  const spent = agent => mine.filter(c=>c.agent===agent).reduce((sum,c)=>sum+c.duration_s,0);
  return {t, last, mine, spent};
}
function renderStrip(){
  if (!metrics) return;
  const {t, last, spent} = metricFacts();
  const parts = [["tok/s", t.local.tok_s.toFixed(1)], ["first token", t.local.last_ttft_s==null ? "n/a" : t.local.last_ttft_s.toFixed(1)+"s"],
    ["LM Studio CPU", Math.round(last.groups.lmstudio.cpu)+"%"], ["available mem", last.memory.available_mb==null ? "n/a" : gb(last.memory.available_mb)],
    ["time: local", Math.round(spent("local"))+"s"], ["agy", Math.round(spent("agy"))+"s"], ["claude", Math.round(spent("claude"))+"s"], ["Claude cost", "$"+t.claude.cost_usd.toFixed(2)]];
  if (!changed("strip", parts)) return;
  $("strip").replaceChildren(...parts.map(([label,value]) => h("span",{}, label+" ", h("b",{},value))));
}
function paintMetrics(){
  const {t, last, mine, spent} = metricFacts(), local = metrics.calls.filter(c=>c.agent==="local");
  const kpi = (value,label) => h("div",{class:"kpi"}, h("b",{},value), h("span",{},label));
  $("pane").replaceChildren(
    h("div",{class:"kpis"},
      kpi(t.local.tok_s.toFixed(1), "tokens / second (last call)"), kpi(t.local.last_ttft_s==null?"n/a":t.local.last_ttft_s.toFixed(2)+"s", "time to first token"),
      kpi(String(t.local.prompt_tokens+t.local.completion_tokens), "local tokens this session"), kpi("$"+t.claude.cost_usd.toFixed(2), "Claude cost this session"),
      kpi(t.agy.calls_today+"/"+(t.agy.quota_est||"?"), "agy calls today"), kpi(Math.round(last.groups.lmstudio.cpu)+"%", "LM Studio CPU"),
      kpi(gb(last.groups.lmstudio.rss_mb), "LM Studio resident memory"), kpi(last.memory.available_mb==null?"n/a":gb(last.memory.available_mb), "available memory (free + inactive)"),
      kpi(last.memory.wired_mb==null?"n/a":gb(last.memory.wired_mb), "wired memory"), kpi(String(last.load1), "load average (1 min)")),
    h("div",{class:"sect"},"Tokens / second per local call ("+local.length+")"), spark(local.map(c=>c.tok_s||0)),
    h("div",{class:"sect"},"LM Studio CPU %, last "+metrics.samples.length+" samples (2 s apart)"), spark(metrics.samples.map(s=>s.groups.lmstudio.cpu)),
    h("div",{class:"sect"},"Time spent this run"), h("div",{class:"kpis"}, kpi(Math.round(spent("local"))+"s","Agent 0 (local)"), kpi(Math.round(spent("agy"))+"s","agy"), kpi(Math.round(spent("claude"))+"s","claude")),
    h("div",{class:"sect"},"Calls this run ("+mine.length+")"),
    h("table",{class:"calls"}, h("tr",{}, ...["agent","purpose","time","tok/s","first tok","cost"].map(x=>h("th",{},x))),
      ...mine.slice().reverse().slice(0,40).map(c => h("tr",{}, h("td",{class:c.ok?"":"st-bad"},c.agent), h("td",{class:"p"},c.purpose), h("td",{},c.duration_s.toFixed(1)+"s"),
        h("td",{}, c.tok_s!=null?c.tok_s.toFixed(1):""), h("td",{}, c.ttft_s!=null?c.ttft_s.toFixed(2)+"s":""), h("td",{}, c.cost_usd!=null?"$"+c.cost_usd.toFixed(3):"")))));
}
async function loadMetrics(){
  metrics = await api("/api/metrics");
  renderStrip();
  if (tab==="metrics" && drawerOpen()) paintMetrics();
}

/* ---- wiring ---- */
function renderAll(){ renderTop(); renderRunBar(); renderPerm(); renderQuestions(); renderBoard(); renderDeck(); renderDrawer(false); }
let firstLoad = true;
async function refresh(){
  S = await api("/api/state");
  if (firstLoad){
    firstLoad = false;
    for (const a of AGENTS) if (S.streams[a]) streams[a] = [["info", S.streams[a]]];
    const link = new URLSearchParams(location.search);
    if (TABS.includes(link.get("tab"))) tab = link.get("tab");
    if (AGENTS.includes(link.get("inspect"))) sel.agent = link.get("inspect");
    if (link.get("unit")){ sel.unit = sel.card = link.get("unit"); }
    sel.spec = link.get("spec") || specName(run().run.spec_path);
    if (link.get("inspect") || link.get("tab") || link.get("unit") || store("drawer")==="1") document.body.classList.add("open");
  }
  renderAll();
  if (drawerOpen() && changed("firstpane", 1)) renderDrawer(true);
}
$("task").addEventListener("submit", async e=>{ e.preventDefault();
  await api("/api/task",{task:$("task-text").value, no_clarify:$("no-clarify").checked});
  $("task-text").value=""; for (const a of AGENTS) streams[a]=[]; sel.unit=""; sel.card=""; dismissedRun=""; if (drawerOpen() && tab==="stream") paintStream(); refresh(); });
$("no-clarify").checked = store("skip") === "1";
$("no-clarify").addEventListener("change", e=>store("skip", e.target.checked ? "1" : "0"));
$("show-hidden").checked = showHidden;
$("show-hidden").addEventListener("change", e=>{ showHidden = e.target.checked; store("hidden", showHidden ? "1" : "0"); renderBoard(); });
$("project").addEventListener("focus", e=>{ e.target.value = S ? S.project.path : ""; e.target.select(); });
$("project").addEventListener("blur", e=>{ if (S) e.target.value = S.project.name; });
$("project").addEventListener("change", e=>{ const path = e.target.value; if (S && path && path !== S.project.path && path !== S.project.name) api("/api/project",{path}).then(refresh, refresh); });
for (const [agent,id] of Object.entries(MODEL_KEYS)) $(id).addEventListener("change", e=>api("/api/models",{agent, model:e.target.value}).then(refresh));
$("d-close").onclick = closeDrawer;
document.querySelectorAll(".tabs button").forEach(b=>b.onclick=()=>{ tab=b.dataset.tab; renderDrawer(true); });
$("pause").onclick = ()=>api("/api/control",{action: run().paused ? "resume" : "pause"});
$("halt").onclick = $("top-halt").onclick = ()=>api("/api/control",{action:"halt"});
$("strip").onclick = ()=>{ tab="metrics"; openDrawer(); };
$("steer-form").addEventListener("submit", e=>{ e.preventDefault(); const text=$("steer-text").value.trim(); if(!text) return;
  api("/api/steer",{agent:sel.agent, text}).then(()=>{ $("steer-text").value=""; pushChunk(sel.agent,"info","\n[steer queued] "+text+"\n"); }); });
document.addEventListener("click", e=>{ if (menuEl && !menuEl.contains(e.target)) closeMenu(); });
document.addEventListener("keydown", e=>{
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target.tagName||""));
  if (e.key === "Escape"){ if (menuEl) closeMenu(); else if (!closeModal() && !typing && drawerOpen()) closeDrawer(); }
  else if (e.key === "/" && !typing && !$("task-text").disabled){ e.preventDefault(); $("task-text").focus(); }
});

const events = new EventSource("/api/stream");
events.addEventListener("state", e=>{ if(!S) return; S.run = JSON.parse(e.data);
  renderRunBar(); renderPerm(); renderQuestions(); renderBoard(); renderDeck(); renderDrawer(false);
  if (changed("columns", S.run.units.map(u=>u.column+u.status))) refresh();      // branch, status line and history follow unit progress
  if (!drawerOpen()) return;
  const unit = S.run.units.find(u=>u.id===sel.unit) || S.run.units.find(u=>u.column==="in_progress"||u.column==="review");
  if (tab==="messages" && changed("messages", S.run.messages_count)) loadMessages();
  if (tab==="diff" && changed("diffkey", [sel.unit, unit && [unit.id, unit.status, unit.has_diff, unit.checks.length]])) loadDiff(); });
events.addEventListener("chunks", e=>{ for (const [agent,kind,text] of JSON.parse(e.data)) pushChunk(agent, kind, text); });
events.addEventListener("trace", e=>{ if (tab==="traces" && drawerOpen()){ const pane=$("pane"); if (pane.children.length) pane.insertBefore(traceRow(JSON.parse(e.data)), pane.children[1]||null); } });
events.addEventListener("refresh", ()=>{ refresh(); loadMetrics(); });
setInterval(()=>document.querySelectorAll("[data-since]").forEach(el=>el.textContent=elapsed(+el.dataset.since)), 1000);
setInterval(()=>{ if (S && (S.busy || (drawerOpen() && tab==="metrics"))) loadMetrics(); }, 2000);
setInterval(refresh, 20000);
refresh().then(()=>{ loadModels(false); loadMetrics(); });
</script>
</body>
</html>
"""


def parse_args(argv):
    """Parse the command line.

    @rollback: none (pure).
    """
    parser = argparse.ArgumentParser(description="MAID dashboard (local web UI for the orchestrator).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port on 127.0.0.1 (default %d)" % DEFAULT_PORT)
    parser.add_argument("--repo", default=str(REPO_ROOT), help="repository to open (default: this repo)")
    return parser.parse_args(argv)


def main(argv=None):
    """Serve the dashboard until interrupted.

    State mutations: binds 127.0.0.1:<port>; see the module docstring for the rest.
    @rollback: Ctrl-C stops the server; a run in progress is halted and rolled back.
    """
    args = parse_args(sys.argv[1:] if argv is None else argv)
    app = App(args.repo)
    Handler.app = app
    # Invariant: loopback only. This server can start coding agents.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    log_event(logger, "ui_start", "dashboard on http://127.0.0.1:%d" % args.port, repo=str(app.repo), port=args.port)
    print("MAID dashboard: http://127.0.0.1:%d  (Ctrl-C to stop)" % args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
        if app.busy:
            app.control.halt()
            app._thread.join(timeout=30)
    finally:
        server.server_close()
        if app.sampler:
            app.sampler.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

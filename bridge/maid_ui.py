"""MAID dashboard: a local web UI over the orchestrator (Kanban board + agent inspector).

Purpose
    `python3 bridge/maid_ui.py` serves a single-page dashboard on 127.0.0.1 that
    starts orchestrator runs and shows them live:
      Zone A  top bar: project, git branch and diff counter, model dropdowns for the
              three agents, new-task input, clarification question cards
      Zone B  Kanban board: Backlog -> In Progress -> Review -> Done, one card per
              atomic unit, synced with the live run and with doc/tasks/*.md
      Zone C  agent status blocks: state, model, target, elapsed time
      Zone D  inspector drawer: TERMINAL/STREAM, GIT DIFF, MESSAGES, TRACES, plus
              pause, halt, steer and the sandbox permission banner
      Footer  the quota and telemetry status line

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
    * Remembers opened projects in log/ui_projects.json (git-ignored).

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
    discover_models,
    get_correlation_id,
    get_models,
    git,
    git_head,
    git_summary,
    log_event,
    set_model,
    status_line,
)
from maid_runtime import DECISIONS, RunControl

DEFAULT_PORT = 8765
TOKEN = secrets.token_urlsafe(24)
PROJECTS_FILE = LOG_DIR / "ui_projects.json"
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
        self.subscribe(lambda kind, payload: bus.publish(kind, light_state(payload) if kind == "state" else payload))

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

    def __init__(self, repo):
        self.bus = EventBus()
        self.control = orchestrator.set_control(UIControl(self.bus))
        self.control.set_models(get_models())
        self.repo = Path(repo).resolve()
        self._lock = threading.Lock()
        self._thread = None
        self._discovery = (0, None)
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

    def history(self, current_spec):
        """Return Kanban cards for the units in doc/tasks/*.md, newest spec first.

        The spec of the run in progress is skipped; its units are live cards.
        @rollback: none (read-only).
        """
        cards = []
        tasks_dir = self.repo / "doc" / "tasks"
        specs = sorted(tasks_dir.glob("*_*.md"), reverse=True) if tasks_dir.is_dir() else []
        for spec in specs[:30]:
            if current_spec and spec.name == Path(current_spec).name:
                continue
            cards += parse_spec(spec.read_text(encoding="utf-8", errors="replace"), spec.name)
        return cards

    def state(self):
        """Return the full page state.

        @rollback: none (read-only).
        """
        snapshot = self.control.snapshot()
        summary = git_summary(self.repo)
        return {
            "project": dict(summary, path=str(self.repo), name=self.repo.name, known=self.known_projects()),
            "run": light_state(snapshot),
            "busy": self.busy,
            "status_line": status_line(self.repo, snapshot["sandbox"]),
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
            cards[heading.group(1)] = {
                "id": heading.group(1), "action": heading.group(2), "file": heading.group(3), "spec": name,
                "title": title, "agent": "", "status": "planned", "column": "backlog", "rollback": "", "attempt": 0,
                "history": True,
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
display:grid;grid-template-rows:auto auto 1fr auto auto;grid-template-columns:1fr auto;overflow:hidden}
button,input,select{font:inherit;color:inherit}
button{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:5px 10px;cursor:pointer}
button:hover{border-color:var(--accent)}
button:disabled{opacity:.45;cursor:default;border-color:var(--line)}
button.primary{background:#1f6feb;border-color:#1f6feb;color:#fff}
button.danger{border-color:#6e2b2b;color:var(--bad)}
input[type=text],select{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:5px 8px;min-width:0}
input[type=text]:focus,select:focus{outline:none;border-color:var(--accent)}
label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.mono{font-family:var(--mono)}
.muted{color:var(--muted)}
#top{grid-column:1/3;background:var(--panel);border-bottom:1px solid var(--line);padding:10px 14px;display:flex;flex-wrap:wrap;gap:10px 16px;align-items:end}
#top .field{display:flex;flex-direction:column;gap:3px;min-width:0}
#top .brand{font-weight:700;letter-spacing:.12em;font-size:15px;align-self:center;margin-right:4px}
#project{width:230px}
.chip{display:inline-flex;gap:6px;align-items:center;background:var(--bg);border:1px solid var(--line);border-radius:999px;padding:4px 10px;font-family:var(--mono);font-size:12px;white-space:nowrap}
.add{color:var(--ok)}.del{color:var(--bad)}
#task{flex:1 1 320px;display:flex;gap:8px;align-items:end}
#task .field{flex:1}
#task input[type=text]{width:100%}
.opt{display:flex;gap:5px;align-items:center;color:var(--muted);white-space:nowrap;padding-bottom:6px}
#notice{grid-column:1/3;display:flex;flex-direction:column}
.banner{padding:10px 14px;border-bottom:1px solid var(--line);background:#2a2110;display:flex;flex-direction:column;gap:8px}
.banner.perm{background:#2b1717}
.banner h3{margin:0;font-size:13px}
.banner dl{margin:0;display:grid;grid-template-columns:max-content 1fr;gap:3px 12px}
.banner dt{color:var(--muted);font-family:var(--mono);font-size:12px}
.banner dd{margin:0;overflow-wrap:anywhere}
.banner .row{display:flex;gap:8px;flex-wrap:wrap}
.q{display:flex;flex-direction:column;gap:4px}
.q input{width:100%}
#board{grid-column:1;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;padding:12px 14px;overflow:auto;min-height:0}
.col{background:var(--panel);border:1px solid var(--line);border-radius:8px;display:flex;flex-direction:column;min-height:0;min-width:0}
.col h2{margin:0;padding:9px 12px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--line);display:flex;justify-content:space-between}
.cards{padding:8px;display:flex;flex-direction:column;gap:8px;overflow:auto}
.card{background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:9px 10px;cursor:pointer;display:flex;flex-direction:column;gap:5px;min-width:0}
.card:hover,.card.sel{border-color:var(--accent)}
.card.hist{opacity:.72}
.card .head{display:flex;justify-content:space-between;gap:8px;align-items:baseline}
.card .id{font-family:var(--mono);font-weight:700}
.card .file{font-family:var(--mono);font-size:12px;overflow-wrap:anywhere}
.card .cmd{font-family:var(--mono);font-size:11px;color:var(--muted);overflow-wrap:anywhere}
.tag{font-size:11px;border:1px solid var(--line);border-radius:4px;padding:0 6px;white-space:nowrap}
.tag.agy{color:var(--violet);border-color:#4a3b78}.tag.claude{color:#f0a36c;border-color:#6b4526}
.st-ok{color:var(--ok)}.st-bad{color:var(--bad)}.st-run{color:var(--warn)}
.empty{color:var(--muted);padding:6px 4px;font-size:12px}
#drawer{grid-column:2;grid-row:3/5;width:min(46vw,640px);border-left:1px solid var(--line);background:var(--panel);display:none;flex-direction:column;min-height:0}
body.open #drawer{display:flex}
#drawer header{padding:10px 12px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:8px}
#drawer header b{overflow-wrap:anywhere}
.tabs{display:flex;border-bottom:1px solid var(--line)}
.tabs button{flex:1;border:0;border-radius:0;background:none;padding:8px 4px;font-size:11px;letter-spacing:.06em;color:var(--muted);border-bottom:2px solid transparent}
.tabs button.on{color:var(--text);border-bottom-color:var(--accent)}
#pane{flex:1;overflow:auto;min-height:0;padding:10px 12px}
#pane pre{margin:0;font:12px/1.5 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere}
.s-reasoning{color:var(--muted)}.s-stderr{color:#e9967a}.s-tool_call{color:var(--violet)}.s-info{color:var(--accent)}
.d-add{color:var(--ok)}.d-del{color:var(--bad)}.d-hunk{color:var(--accent)}.d-meta{color:var(--muted)}
.msg,.trace{border:1px solid var(--line);border-radius:6px;margin-bottom:8px;background:var(--panel2)}
.msg summary,.trace summary{padding:6px 9px;cursor:pointer;display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.msg pre,.trace pre{padding:8px 9px;border-top:1px solid var(--line)}
.msg h4{margin:0;padding:6px 9px 0;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.lv-ERROR{color:var(--bad)}.lv-WARNING{color:var(--warn)}.lv-DEBUG{color:var(--muted)}
#steer{border-top:1px solid var(--line);padding:10px 12px;display:flex;flex-direction:column;gap:8px}
#steer .row{display:flex;gap:8px}
#steer input{flex:1}
#deck{grid-column:1;border-top:1px solid var(--line);background:var(--panel);padding:10px 14px;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
.agent{border:1px solid var(--line);border-radius:8px;padding:9px 11px;background:var(--panel2);cursor:pointer;display:flex;flex-direction:column;gap:4px;min-width:0}
.agent:hover,.agent.sel{border-color:var(--accent)}
.agent .title{font-size:11px;letter-spacing:.06em;font-weight:700}
.agent .line{display:flex;justify-content:space-between;gap:8px}
.agent .target{font-family:var(--mono);font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.state{font-family:var(--mono);font-weight:700}
.state.idle{color:var(--muted)}.state.thinking{color:var(--accent)}.state.coding{color:var(--ok)}.state.reviewing{color:var(--violet)}.state.awaiting_permission{color:var(--bad)}
#foot{grid-column:1/3;border-top:1px solid var(--line);background:#0a0c0f;padding:6px 14px;font:12px var(--mono);color:var(--muted);white-space:nowrap;overflow-x:auto}
#toast{position:fixed;left:50%;bottom:44px;transform:translateX(-50%);background:#2b1717;border:1px solid #6e2b2b;color:#ffd7d5;border-radius:6px;padding:8px 14px;display:none;max-width:80vw}
@media (max-width:900px){body{overflow:auto;display:block}#board{grid-template-columns:1fr 1fr}#deck{grid-template-columns:1fr}#drawer{width:auto;border-left:0;border-top:1px solid var(--line);max-height:70vh}}
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
    <div class="field"><label for="task-text">New task</label><input type="text" id="task-text" placeholder="Describe the task for Agent 0…" autocomplete="off"></div>
    <label class="opt"><input type="checkbox" id="no-clarify"> skip questions</label>
    <button class="primary" id="start">Start</button>
  </form>
</div>
<div id="notice"></div>
<div id="board"></div>
<aside id="drawer">
  <header><b id="d-title">Inspector</b><span><span class="muted" id="d-run"></span> <button id="d-close">Close</button></span></header>
  <div class="tabs">
    <button data-tab="stream" class="on">TERMINAL / STREAM</button><button data-tab="diff">GIT DIFF</button>
    <button data-tab="messages">MESSAGES</button><button data-tab="traces">TRACES</button>
  </div>
  <div id="pane"></div>
  <div id="steer">
    <div class="row"><button id="pause">Pause</button><button id="halt" class="danger">Halt</button><span class="muted" id="ctl-note"></span></div>
    <form class="row" id="steer-form"><input type="text" id="steer-text" placeholder="steer this agent — injected as context on next turn/retry" autocomplete="off"><button>Steer</button></form>
  </div>
</aside>
<div id="deck"></div>
<div id="foot">…</div>
<div id="toast"></div>
<script>
"use strict";
const TOKEN = "__TOKEN__";
const COLUMNS = [["backlog","Backlog"],["in_progress","In Progress"],["review","Review"],["done","Done"]];
const AGENTS = ["agent0","agy","claude"];
const MODEL_KEYS = {local:"m-local", agy:"m-agy", claude:"m-claude"};
let S = null;                      // last /api/state
let sel = {agent:"agent0", unit:""};
let tab = "stream";
const streams = {agent0:[], agy:[], claude:[]};   // [kind, text] pairs per agent
let traces = [];
let messagesSeen = -1;

const $ = id => document.getElementById(id);
function h(tag, attrs, ...kids){
  const el = document.createElement(tag);
  for (const [k,v] of Object.entries(attrs||{})){
    if (k === "class") el.className = v; else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}
function toast(text){ const t=$("toast"); t.textContent=text; t.style.display="block"; clearTimeout(toast.t); toast.t=setTimeout(()=>t.style.display="none",5000); }
async function api(path, body){
  const res = await fetch(path, body === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json","X-MAID-Token":TOKEN}, body:JSON.stringify(body)});
  const data = await res.json().catch(()=>({error:"bad response"}));
  if (!res.ok){ toast(data.error || res.statusText); throw new Error(data.error); }
  return data;
}
const run = () => S.run;
function elapsed(since){ const s=Math.max(0,Math.floor(Date.now()/1000-since)); return s<60 ? s+"s" : Math.floor(s/60)+"m "+(s%60)+"s"; }

/* ---- Zone A ---- */
function renderTop(){
  const p = S.project;
  if (document.activeElement !== $("project")) $("project").value = p.path;
  $("projects").replaceChildren(...p.known.map(path => h("option",{value:path})));
  $("branch").textContent = "🌿 "+p.branch; $("added").textContent = "+"+p.added; $("deleted").textContent = "-"+p.deleted;
  $("start").disabled = S.busy; $("start").textContent = S.busy ? "Running…" : "Start";
  $("foot").textContent = S.status_line;
}
async function loadModels(refresh){
  const models = await api("/api/models"+(refresh?"?refresh=1":""));
  for (const [agent,id] of Object.entries(MODEL_KEYS)){
    const info = models[agent], box = $(id);
    box.replaceChildren(...info.models.map(m => h("option",{value:m, selected:m===info.current}, m)));
    box.title = info.error || info.source;
  }
}
function renderNotice(){
  const kids = [], r = run();
  if (r.permission){
    const p = r.permission;
    kids.push(h("div",{class:"banner perm"},
      h("h3",{},"🔒 Sandbox permission request from "+p.requester),
      h("dl",{}, h("dt",{},"target_path"), h("dd",{class:"mono"},p.target_path), h("dt",{},"reason"), h("dd",{},p.reason), h("dt",{},"goal_impact"), h("dd",{},p.goal_impact)),
      h("div",{class:"row"}, ...p.actions.map(a => h("button",{class:a.decision==="deny"?"danger":"", onclick:()=>api("/api/permission",{id:p.id, decision:a.decision})}, a.label)))));
  }
  if (r.questions){
    const inputs = [];
    const form = h("form",{class:"banner", onsubmit:e=>{e.preventDefault(); api("/api/answer",{answers:inputs.map(i=>i.value)});}},
      h("h3",{},"Agent 0 needs "+r.questions.length+" clarification"+(r.questions.length>1?"s":"")+" before planning"),
      ...r.questions.map((q,i)=>{ const input=h("input",{type:"text",autocomplete:"off",placeholder:"Your answer (leave blank to let Agent 0 decide)"}); inputs.push(input); return h("div",{class:"q"}, h("span",{},(i+1)+". "+q), input); }),
      h("div",{class:"row"}, h("button",{class:"primary"},"Send answers")));
    const old = document.querySelector("#notice form");
    if (old && old.dataset.key === r.questions.join("|")) kids.push(old); else { form.dataset.key = r.questions.join("|"); kids.push(form); }
  }
  if (r.run.error && (r.run.status === "failed" || r.run.status === "halted"))
    kids.push(h("div",{class:"banner"}, h("h3",{},"Run "+r.run.status+": "+r.run.error)));
  $("notice").replaceChildren(...kids);
}

/* ---- Zone B ---- */
function statusClass(st){ return /passed|verified/.test(st) ? "st-ok" : /fail|halt|rolled/.test(st) ? "st-bad" : /pending|planned|not run/.test(st) ? "muted" : "st-run"; }
function card(u){
  const live = !u.history;
  const checks = (u.checks||[]).map(c => (c.ok?"✓ ":"✗ ")+c.name).join("  ");
  return h("div",{class:"card"+(live?"":" hist")+(live&&sel.unit===u.id?" sel":""), onclick:()=>{ if(live){ sel={agent:u.agent||sel.agent, unit:u.id}; openDrawer(); } }},
    h("div",{class:"head"}, h("span",{class:"id"},u.id), u.agent ? h("span",{class:"tag "+u.agent},u.agent) : null),
    h("div",{class:"file"}, u.action+" "+u.file),
    h("div",{class:statusClass(u.status)}, u.status+(u.attempt>1?" (attempt "+u.attempt+")":"")),
    checks ? h("div",{class:"muted mono"},checks) : null,
    !live ? h("div",{class:"muted"},u.title) : null,
    u.rollback ? h("div",{class:"cmd"},u.rollback) : null,
    live && u.column==="done" && u.rollback ? h("button",{class:"danger", disabled:S.busy, title:S.busy?"wait for the run to finish":"restore this file to its pre-change state", onclick:e=>{e.stopPropagation(); api("/api/rollback",{unit:u.id}).then(refresh);}},"Rollback Unit") : null);
}
function renderBoard(){
  const units = run().units.concat(S.history);
  $("board").replaceChildren(...COLUMNS.map(([key,title])=>{
    const mine = units.filter(u=>u.column===key);
    return h("section",{class:"col"}, h("h2",{},h("span",{},title),h("span",{},mine.length)),
      h("div",{class:"cards"}, mine.length ? mine.map(card) : h("div",{class:"empty"},"—")));
  }));
}

/* ---- Zone C ---- */
function renderDeck(){
  $("deck").replaceChildren(...AGENTS.map(key=>{
    const a = run().agents[key];
    return h("div",{class:"agent"+(sel.agent===key&&document.body.classList.contains("open")?" sel":""), onclick:()=>{sel={agent:key, unit:""}; openDrawer();}},
      h("div",{class:"title"}, key==="agent0" ? "AGENT 0 ("+(a.model||"local").split("/").pop().toUpperCase()+" - BRAIN)" : a.title),
      h("div",{class:"line"}, h("span",{class:"state "+a.state}, a.state), h("span",{class:"tag"}, a.model||"default")),
      h("div",{class:"line"}, h("span",{class:"target"}, a.target||"—"), h("span",{class:"muted mono", "data-since":a.since}, elapsed(a.since))));
  }));
}

/* ---- Zone D ---- */
function openDrawer(){ document.body.classList.add("open"); renderDeck(); renderBoard(); renderDrawer(true); }
function renderDrawer(reload){
  if (!document.body.classList.contains("open")) return;
  const r = run();
  $("d-title").textContent = (sel.unit ? sel.unit+" · " : "") + r.agents[sel.agent].title;
  $("d-run").textContent = r.run.id ? "run "+r.run.id+" · "+r.run.status : "";
  $("pause").textContent = r.paused ? "Resume" : "Pause";
  $("pause").disabled = $("halt").disabled = !S.busy;
  $("ctl-note").textContent = r.paused ? "paused: takes effect between agent calls" : "";
  document.querySelectorAll(".tabs button").forEach(b=>b.classList.toggle("on", b.dataset.tab===tab));
  if (tab === "stream") paintStream();
  else if (reload) ({diff:loadDiff, messages:loadMessages, traces:loadTraces})[tab]();
}
function paintStream(){
  const pre = h("pre",{}, ...streams[sel.agent].map(([kind,text]) => h("span",{class:"s-"+kind}, text)));
  if (!streams[sel.agent].length) pre.append(h("span",{class:"muted"}, "No output from this agent yet."));
  $("pane").replaceChildren(pre); $("pane").scrollTop = $("pane").scrollHeight;
}
function pushChunk(agent, kind, text){
  const list = streams[agent], last = list[list.length-1];
  if (last && last[0]===kind) last[1] += text; else list.push([kind,text]);
  if (list.length > 600) list.splice(0, list.length-600);
  if (tab==="stream" && sel.agent===agent && document.body.classList.contains("open")){
    paintStream();
  }
}
async function loadDiff(){
  const d = await api("/api/diff?unit="+encodeURIComponent(sel.unit));
  if (tab!=="diff") return;
  const lines = (d.diff||"").split("\n").map(line => h("span",{class: line.startsWith("@@")?"d-hunk": /^(\+\+\+|---|diff |index )/.test(line)?"d-meta": line[0]==="+"?"d-add": line[0]==="-"?"d-del":""}, line+"\n"));
  $("pane").replaceChildren(
    h("div",{class:"muted", style:"margin-bottom:8px"}, d.file ? (d.unit+" · "+d.file+(d.live?" · live working-tree diff":" · diff recorded at verification")) : "No unit selected yet."),
    d.rollback ? h("div",{class:"cmd mono", style:"margin-bottom:8px"}, "rollback: "+d.rollback) : null,
    h("pre",{}, d.diff ? lines : h("span",{class:"muted"},"No changes to this file yet.")));
}
async function loadMessages(){
  const all = await api("/api/messages");
  if (tab!=="messages") return;
  messagesSeen = all.length;
  const mine = all.filter(m => m.to===sel.agent || m.from===sel.agent).reverse();
  $("pane").replaceChildren(...(mine.length ? mine.map(m => h("details",{class:"msg"},
    h("summary",{}, h("b",{}, m.from+" → "+m.to), h("span",{class:"muted"}, m.purpose), h("span",{class:"muted mono"}, new Date(m.at*1000).toLocaleTimeString())),
    h("h4",{},"Prompt"), h("pre",{}, m.prompt), h("h4",{},"Raw response"), h("pre",{}, m.response==null ? "(waiting…)" : m.response))) : [h("div",{class:"muted"},"No messages for this agent yet.")]));
}
function traceRow(t){
  return h("details",{class:"trace"}, h("summary",{}, h("span",{class:"muted mono"},t.timestamp), h("b",{class:"lv-"+t.level},t.event), h("span",{},t.message)),
    h("pre",{}, Object.entries(t.data||{}).map(([k,v])=>k+": "+v).join("\n") || "(no data)"));
}
async function loadTraces(){
  const res = await api("/api/traces");
  if (tab!=="traces") return;
  traces = res.records;
  $("pane").replaceChildren(h("div",{class:"muted", style:"margin-bottom:8px"}, res.correlation_id ? "correlation_id "+res.correlation_id+" · "+traces.length+" records from log/maid_execution.jsonl" : "No run yet."),
    ...traces.slice().reverse().map(traceRow));
}

/* ---- wiring ---- */
function renderAll(){ renderTop(); renderNotice(); renderBoard(); renderDeck(); renderDrawer(false); }
async function refresh(){
  const first = S === null;
  S = await api("/api/state");
  if (first) for (const a of AGENTS) if (S.streams[a]) streams[a] = [["info", S.streams[a]]];
  renderAll();
}
$("task").addEventListener("submit", async e=>{ e.preventDefault();
  await api("/api/task",{task:$("task-text").value, no_clarify:$("no-clarify").checked});
  $("task-text").value=""; for (const a of AGENTS) streams[a]=[]; traces=[]; sel.unit=""; refresh(); });
$("project").addEventListener("change", e=>api("/api/project",{path:e.target.value}).then(refresh, refresh));
for (const [agent,id] of Object.entries(MODEL_KEYS)) $(id).addEventListener("change", e=>api("/api/models",{agent, model:e.target.value}).then(refresh));
$("d-close").onclick = ()=>{ document.body.classList.remove("open"); renderDeck(); };
document.querySelectorAll(".tabs button").forEach(b=>b.onclick=()=>{ tab=b.dataset.tab; renderDrawer(true); });
$("pause").onclick = ()=>api("/api/control",{action: run().paused ? "resume" : "pause"});
$("halt").onclick = ()=>api("/api/control",{action:"halt"});
$("steer-form").addEventListener("submit", e=>{ e.preventDefault(); const text=$("steer-text").value.trim(); if(!text) return;
  api("/api/steer",{agent:sel.agent, text}).then(()=>{ $("steer-text").value=""; pushChunk(sel.agent,"info","\n[steer queued] "+text+"\n"); }); });

const events = new EventSource("/api/stream");
events.addEventListener("state", e=>{ if(!S) return; S.run = JSON.parse(e.data);
  renderNotice(); renderBoard(); renderDeck(); renderDrawer(false);
  if (document.body.classList.contains("open")){ if (tab==="messages" && S.run.messages_count!==messagesSeen) loadMessages(); if (tab==="diff") loadDiff(); } });
events.addEventListener("chunk", e=>{ const c=JSON.parse(e.data); pushChunk(c.agent, c.stream, c.text); });
events.addEventListener("trace", e=>{ if (tab==="traces" && document.body.classList.contains("open")){ const t=JSON.parse(e.data); const first=$("pane").children[1]; $("pane").insertBefore(traceRow(t), first||null); } });
events.addEventListener("refresh", refresh);
setInterval(()=>document.querySelectorAll("[data-since]").forEach(el=>el.textContent=elapsed(+el.dataset.since)), 1000);
setInterval(refresh, 5000);
refresh().then(()=>loadModels(false));
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
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""MAID structured logging.

Purpose
    One logging setup shared by every MAID script. Each record is written three ways:
      * log/maid_execution.jsonl  one JSON object per line, for machines
      * log/maid_human.log        the same record as readable text, for people
      * console                   a short one-line summary (optional)

Usage
    from LoggingTemplate import get_maid_logger, log_event, new_correlation_id
    logger = get_maid_logger("orchestrator", console="stderr")
    new_correlation_id()
    log_event(logger, "worker_call", "dispatching unit", agent="agy", argv=[...])

Environment
    MAID_LOG_LEVEL       minimum level for all handlers (default DEBUG)
    MAID_LOG_FORMAT      "JSON" makes the console handler emit JSON too
    MAID_LOG_DIR         directory for the two log files (default <repo>/log)
    MAID_CORRELATION_ID  correlation ID inherited from a parent MAID process

State mutations
    * Creates MAID_LOG_DIR if missing and appends to the two log files in it.
    * Attaches handlers to the "maid" logger of this process (once).
    * Holds the current correlation ID in a module global.

Side effects
    Disk writes only. No network access, no changes outside MAID_LOG_DIR.

@rollback
    Delete log/maid_execution.jsonl and log/maid_human.log to discard all records;
    both are git-ignored and are recreated on the next run. To revert this module,
    `git checkout -- logg-template/LoggingTemplate.py`.
"""
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = Path(os.getenv("MAID_LOG_DIR") or REPO_ROOT / "log")
JSONL_LOG_NAME = "maid_execution.jsonl"
HUMAN_LOG_NAME = "maid_human.log"
ROOT_LOGGER_NAME = "maid"

# Invariant: exactly one correlation ID is current per process at any time.
_correlation_id = os.getenv("MAID_CORRELATION_ID") or uuid.uuid4().hex[:12]


def get_correlation_id():
    """Return the correlation ID stamped on every record of this process.

    @rollback: none (pure read).
    """
    return _correlation_id


def set_correlation_id(correlation_id):
    """Replace the current correlation ID.

    State mutations: overwrites the module-global correlation ID.
    @rollback: call again with the previous value from get_correlation_id().
    """
    global _correlation_id
    _correlation_id = correlation_id
    return _correlation_id


def new_correlation_id():
    """Generate a fresh correlation ID and make it current.

    State mutations: overwrites the module-global correlation ID.
    @rollback: call set_correlation_id() with the previous value.
    """
    return set_correlation_id(uuid.uuid4().hex[:12])


def _timestamp(record):
    """Format a record's creation time as UTC ISO-8601 with a Z suffix.

    @rollback: none (pure).
    """
    created = datetime.fromtimestamp(record.created, timezone.utc)
    return created.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _CorrelationFilter(logging.Filter):
    """Stamps correlation_id, event and data onto every record.

    State mutations: sets three attributes on the LogRecord passing through.
    @rollback: remove the filter from its handler; records are not persisted by it.
    """

    def filter(self, record):
        record.correlation_id = _correlation_id
        record.event = getattr(record, "event", "log")
        record.data = getattr(record, "data", {})
        return True


class MAIDJSONFormatter(logging.Formatter):
    """Generates structured JSON logs for easy parsing and automated debugging.

    @rollback: none (pure formatting; swap the handler's formatter to change output).
    """

    def format(self, record):
        log_record = {
            "timestamp": _timestamp(record),
            "level": record.levelname,
            "correlation_id": getattr(record, "correlation_id", _correlation_id),
            "event": getattr(record, "event", "log"),
            "logger": record.name,
            "module": record.module,
            "function": record.funcName,
            "pid": record.process,
            "message": record.getMessage(),
            "data": getattr(record, "data", {}),
        }
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)
        # default=str keeps logging from ever raising on a non-serialisable field
        return json.dumps(log_record, ensure_ascii=False, default=str)


class MAIDHumanFormatter(logging.Formatter):
    """Renders the full record, including every data field, as indented text.

    @rollback: none (pure formatting).
    """

    def format(self, record):
        lines = [
            "%s | %-8s | %s | %s::%s | %s | %s"
            % (
                _timestamp(record),
                record.levelname,
                getattr(record, "correlation_id", _correlation_id),
                record.name,
                record.funcName,
                getattr(record, "event", "log"),
                record.getMessage(),
            )
        ]
        for key, value in (getattr(record, "data", {}) or {}).items():
            if not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False, default=str)
            value_lines = value.splitlines() or [""]
            lines.append("    %s: %s" % (key, value_lines[0]))
            lines.extend("        " + extra for extra in value_lines[1:])
        if record.exc_info:
            lines.extend("    " + line for line in self.formatException(record.exc_info).splitlines())
        return "\n".join(lines)


def _configure_root(console):
    """Attach the file and console handlers to the "maid" logger, once per process.

    State mutations: creates LOG_DIR; adds handlers to the "maid" logger.
    Side effects: opens both log files in append mode.
    @rollback: `logging.getLogger("maid").handlers.clear()` detaches everything;
        delete the two files under LOG_DIR to discard what was written.
    """
    root = logging.getLogger(ROOT_LOGGER_NAME)
    if root.handlers:
        return root
    root.setLevel(os.getenv("MAID_LOG_LEVEL", "DEBUG"))
    # Invariant: MAID records never reach the Python root logger (no duplicate output).
    root.propagate = False
    correlation_filter = _CorrelationFilter()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    for file_name, formatter in (
        (JSONL_LOG_NAME, MAIDJSONFormatter()),
        (HUMAN_LOG_NAME, MAIDHumanFormatter()),
    ):
        file_handler = logging.FileHandler(LOG_DIR / file_name, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.addFilter(correlation_filter)
        root.addHandler(file_handler)

    if console:
        # Console stays short: INFO and above, message only. Full detail is in the files.
        console_handler = logging.StreamHandler(sys.stderr if console == "stderr" else sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.addFilter(correlation_filter)
        # Determine formatting based on environment
        if os.getenv("MAID_LOG_FORMAT") == "JSON":
            console_handler.setFormatter(MAIDJSONFormatter())
        else:
            # Human-readable color-coded-like string format for standard development
            console_handler.setFormatter(
                logging.Formatter(
                    fmt="%(asctime)s | 🔍 %(levelname)-8s | 🧩 %(name)s::%(funcName)s -> %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
        root.addHandler(console_handler)
    return root


def get_maid_logger(name, console="stdout"):
    """Return the logger `maid.<name>`, configuring handlers on first use.

    Args:
        name: short component name, e.g. "orchestrator".
        console: "stdout", "stderr", or None for file-only logging. Processes that
            speak a protocol on stdout (the MCP server) must not pass "stdout".
            Only the first call in a process decides the console stream.

    State mutations / side effects: see _configure_root().
    @rollback: see _configure_root().
    """
    _configure_root(console)
    return logging.getLogger("%s.%s" % (ROOT_LOGGER_NAME, name))


def log_event(logger, event, message="", level=logging.INFO, **data):
    """Write one structured record.

    Args:
        logger: a logger from get_maid_logger().
        event: machine-readable event name, e.g. "worker_call".
        message: short human summary shown on the console.
        level: logging level.
        **data: arbitrary fields stored under "data" (prompts, argv, stdout, hashes).

    Side effects: appends one record to each log file (and the console if enabled).
    @rollback: none needed; records are append-only diagnostics.
    """
    # stacklevel=2 attributes the record to the caller, not to this helper
    logger.log(level, message or event, extra={"event": event, "data": data}, stacklevel=2)

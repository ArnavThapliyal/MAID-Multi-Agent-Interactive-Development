"""MAID three-agent orchestrator: the local model plans and routes, agy and claude do the work.

Purpose
    Agent 0 (the LM Studio model, qwen/qwen3.8-27b by default) is the brain. It never
    edits code. It clarifies the task, writes a spec, and hands each atomic unit to
    one of two worker CLIs:
      * ask_agy     runs `agy -p` headlessly
      * ask_claude  runs `claude -p --output-format json` headlessly

Phases
    1. Intake and clarification  Agent 0 reads the task against the repo map and asks
                                 the user targeted questions (skip with --no-clarify).
    2. Spec generation           Agent 0 splits the task into atomic units (one file or
                                 one function each); the spec is written to
                                 doc/tasks/<timestamp>_<slug>.md.
    3. Routing and execution     For each unit Agent 0 calls ask_agy or ask_claude. The
                                 orchestrator wraps the call with the repo map, the
                                 spec and the isolation rules.
    4. Verification and rollback Git state and file contents are checkpointed before
                                 the worker runs. Afterwards: scope check, syntax check,
                                 optional test command, and a diff review by the other
                                 agent. A failed unit is rolled back and retried.

Usage
    python3 bridge/orchestrator.py "task" [--no-clarify] [--plan-only] [--no-review]
        [--test-cmd "pytest -q"] [--max-retries 1] [--max-units 8] [--repo PATH]

Environment
    MAID_AGY_BIN / MAID_AGY_MODEL / MAID_AGY_TIMEOUT    agy binary, model, --print-timeout
    MAID_CLAUDE_BIN / MAID_CLAUDE_MODEL                 claude binary, optional model
    MAID_WORKER_TIMEOUT                                 hard limit per CLI call, seconds
    plus those in bridge/maid_common.py and logg-template/LoggingTemplate.py

State mutations
    * Writes doc/tasks/<timestamp>_<slug>.md in the target repo (Phase 2) and appends
      an "Execution results" section to it at the end of the run.
    * Worker CLIs create or modify files in the target repo (Phase 3).
    * Copies pre-change file contents to log/backups/<correlation-id>/ (Phase 4).
    * Appends to log/maid_execution.jsonl and log/maid_human.log.
    The orchestrator never commits, stashes, or pushes.

Side effects
    Prompts sent through ask_agy and ask_claude leave this machine. Each worker call
    uses the user's agy or Claude Code account.

@rollback
    A failed unit is rolled back automatically. To undo a unit that passed, run the
    rollback command printed in the run summary (and recorded in the spec and in the
    logs): `git checkout -- <file>` for a modified tracked file, `rm <file>` for a
    created file, or `cp <backup> <file>` when the file had uncommitted changes.
    To discard a spec, delete its file under doc/tasks/. To revert this script,
    `git checkout -- bridge/orchestrator.py`. Standard library only, Python 3.9+.
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from maid_common import (
    LMSTUDIO_MODEL,
    LOG_DIR,
    REPO_ROOT,
    get_correlation_id,
    get_maid_logger,
    git,
    git_head,
    lmstudio_chat,
    log_event,
    new_correlation_id,
)

AGY_BIN = os.getenv("MAID_AGY_BIN", "agy")
AGY_MODEL = os.getenv("MAID_AGY_MODEL", "gemini-3.8-flash-medium")
AGY_TIMEOUT = os.getenv("MAID_AGY_TIMEOUT", "10m")
CLAUDE_BIN = os.getenv("MAID_CLAUDE_BIN", "claude")
CLAUDE_MODEL = os.getenv("MAID_CLAUDE_MODEL", "")
WORKER_TIMEOUT = float(os.getenv("MAID_WORKER_TIMEOUT", "900"))

AGENTS = ("agy", "claude")
ABSENT = "<absent>"
REPO_MAP_LIMIT = 400
DIFF_LIMIT = 60000
MAX_QUESTIONS = 4
# Paths a unit may never target: git internals and this tool's own runtime output
FORBIDDEN_PREFIXES = (".git/", "log/")

ROUTING_GUIDE = (
    "Routing guide: use agy for fast scaffolding, boilerplate, simple new files and "
    "web-grounded checks. Use claude for complex logic, refactoring existing code and "
    "test writing or verification."
)

AGENT0_SYSTEM = (
    "You are Agent 0, the local orchestrator of MAID (Multi-Agent Interactive Development). "
    "You plan and route work; you never write the code yourself. Be precise and terse."
)

WORKER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "ask_agy",
            "description": (
                "Dispatch the unit to agy, a fast cloud agent. Best for scaffolding, "
                "boilerplate, simple new files and web-grounded checks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Complete instructions for this unit."},
                    "reason": {"type": "string", "description": "Why this agent suits the unit."},
                },
                "required": ["prompt", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_claude",
            "description": (
                "Dispatch the unit to claude, a stronger cloud agent. Best for complex "
                "logic, refactoring existing code, and writing or verifying tests."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Complete instructions for this unit."},
                    "reason": {"type": "string", "description": "Why this agent suits the unit."},
                },
                "required": ["prompt", "reason"],
            },
        },
    },
]

logger = get_maid_logger("orchestrator", console="stderr")


# --------------------------------------------------------------------------- #
# Small pure helpers
# --------------------------------------------------------------------------- #


def slugify(text, limit=50):
    """Turn free text into a lowercase, hyphenated, filename-safe slug.

    @rollback: none (pure).
    """
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].strip("-") or "task"


def extract_json(text):
    """Return the first JSON object found in `text`.

    Tolerates prose and code fences around the object, which the local model
    sometimes adds.

    Raises: ValueError if no JSON object can be decoded.
    @rollback: none (pure).
    """
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text or ""):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("no JSON object found in the reply")


def file_hash(path):
    """Return the SHA-256 of a file's bytes, or ABSENT if it does not exist.

    @rollback: none (read-only).
    """
    if not path.is_file():
        return ABSENT
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_text(path):
    """Return a file's text (undecodable bytes replaced), or "" if it does not exist.

    @rollback: none (read-only).
    """
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


# --------------------------------------------------------------------------- #
# Repository inspection (read-only)
# --------------------------------------------------------------------------- #


def build_repo_map(repo):
    """Return the repo's tracked and untracked (non-ignored) paths, one per line.

    The list is capped at REPO_MAP_LIMIT entries so it always fits in a prompt.

    Side effects: runs `git ls-files` (read-only).
    @rollback: none.
    """
    listing = git(repo, "ls-files", "-co", "--exclude-standard").stdout.splitlines()
    paths = sorted(set(path for path in listing if path))
    shown = paths[:REPO_MAP_LIMIT]
    if len(paths) > len(shown):
        shown.append("... (%d more paths not shown)" % (len(paths) - len(shown)))
    return "\n".join(shown)


def snapshot(repo):
    """Map every dirty path (modified, staged, deleted or untracked) to its content hash.

    A path that is clean relative to HEAD does not appear. Comparing two snapshots
    therefore shows exactly which paths changed in between.

    Side effects: runs `git diff` and `git ls-files` (read-only).
    @rollback: none.
    """
    changed = git(repo, "diff", "--name-only", "-z", "HEAD").stdout.split("\0")
    untracked = git(repo, "ls-files", "-o", "--exclude-standard", "-z").stdout.split("\0")
    result = {}
    for path in changed + untracked:
        # Bytecode caches are a by-product of running Python, not a worker edit
        if not path or "__pycache__/" in path or path.endswith(".pyc"):
            continue
        result[path] = file_hash(repo / path)
    return result


def touched_paths(before, after):
    """Return the paths whose state differs between two snapshot() results.

    @rollback: none (pure).
    """
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def is_tracked(repo, path):
    """Return True if `path` exists in the HEAD commit.

    Side effects: runs `git cat-file` (read-only).
    @rollback: none.
    """
    return git(repo, "cat-file", "-e", "HEAD:%s" % path, check=False).returncode == 0


# --------------------------------------------------------------------------- #
# Agent 0 (local model) calls
# --------------------------------------------------------------------------- #


def agent0_json(messages, validate, purpose, attempts=3):
    """Ask Agent 0 for a JSON object and return `validate(parsed)`.

    If the reply is not valid JSON, or `validate` raises ValueError, the error is fed
    back to the model and it is asked again, up to `attempts` times in total.

    Args:
        messages: conversation so far; a copy is extended on retries.
        validate: callable taking the parsed dict and returning the normalised value;
            raises ValueError with a message the model can act on.
        purpose: short label for the logs.

    Raises: ValueError if every attempt fails; OSError if LM Studio is unreachable.
    Side effects: up to `attempts` HTTP requests to LM Studio (logged in full).
    @rollback: none needed.
    """
    conversation = list(messages)
    error = None
    for attempt in range(1, attempts + 1):
        reply = lmstudio_chat(logger, conversation, purpose="%s (attempt %d)" % (purpose, attempt))
        content = reply.get("content") or ""
        try:
            return validate(extract_json(content))
        except ValueError as problem:
            error = problem
            log_event(logger, "agent0_invalid_json", "%s: %s" % (purpose, problem), level=30, reply=content)
            conversation += [
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": "That reply was rejected: %s. Send the complete corrected JSON object only." % problem,
                },
            ]
    raise ValueError("Agent 0 gave no usable %s after %d attempts: %s" % (purpose, attempts, error))


# --------------------------------------------------------------------------- #
# Phase 1: intake and clarification
# --------------------------------------------------------------------------- #


def clarify(task, repo_map):
    """Phase 1: have Agent 0 find missing details and ask the user about them.

    Returns: a list of {"question", "answer"} dicts (empty if nothing was unclear).

    Side effects: one Agent 0 call; prints questions to stdout and reads answers from
        stdin. If stdin is closed, the unanswered questions are recorded as such and
        Agent 0 is told to use its judgement.
    @rollback: none needed; nothing is written outside the logs.
    """

    def validate(parsed):
        questions = parsed.get("questions")
        if not isinstance(questions, list):
            raise ValueError('"questions" must be a list of strings')
        return [str(question).strip() for question in questions if str(question).strip()][:MAX_QUESTIONS]

    questions = agent0_json(
        [
            {"role": "system", "content": AGENT0_SYSTEM},
            {
                "role": "user",
                "content": (
                    "A user gave the task below for this repository. Before any coding agent is "
                    "dispatched, identify the missing details that would change the implementation "
                    "(target files, behaviour, interfaces, constraints). Ask at most %d targeted "
                    "questions. Do not ask about anything the repo map or the task already answers. "
                    "If the task is specific enough, return an empty list.\n\n"
                    'Reply with one JSON object only: {"questions": ["..."]}\n\n'
                    "## Task\n%s\n\n## Repo map\n%s" % (MAX_QUESTIONS, task, repo_map)
                ),
            },
        ],
        validate,
        "clarifying questions",
    )
    log_event(logger, "clarify_questions", "%d clarifying question(s)" % len(questions), questions=questions)

    clarifications = []
    for number, question in enumerate(questions, 1):
        print("\n[Agent 0] Question %d/%d: %s" % (number, len(questions), question))
        try:
            answer = input("> ").strip()
        except EOFError:
            answer = ""
        clarifications.append(
            {"question": question, "answer": answer or "(no answer given; use your best judgement)"}
        )
    if clarifications:
        log_event(logger, "clarify_answers", "clarifications recorded", clarifications=clarifications)
    return clarifications


# --------------------------------------------------------------------------- #
# Phase 2: spec generation
# --------------------------------------------------------------------------- #


def normalise_plan(parsed, repo, max_units):
    """Validate Agent 0's plan and return it in canonical form.

    Enforces the atomic-unit contract: every unit targets exactly one repo-relative
    file (optionally one symbol in it), outside FORBIDDEN_PREFIXES.

    Raises: ValueError describing the first problem, phrased for the model.
    Side effects: checks which target files exist (read-only).
    @rollback: none.
    """
    units = parsed.get("units")
    if not isinstance(units, list) or not units:
        raise ValueError('"units" must be a non-empty list')
    if len(units) > max_units:
        raise ValueError("too many units (%d); merge or cut down to at most %d" % (len(units), max_units))

    normalised = []
    created = set()
    for index, unit in enumerate(units, 1):
        if not isinstance(unit, dict):
            raise ValueError("unit %d must be an object" % index)
        file = str(unit.get("file") or "").strip().replace("\\", "/")
        if file.startswith("./"):
            file = file[2:]
        if not file or file.startswith("/") or ".." in Path(file).parts or file.endswith("/"):
            raise ValueError('unit %d: "file" must be one repo-relative file path, got %r' % (index, file))
        if any(file.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
            raise ValueError("unit %d: %s is not a path workers may edit" % (index, file))
        description = str(unit.get("description") or "").strip()
        if not description:
            raise ValueError('unit %d: "description" is required' % index)
        agent = str(unit.get("suggested_agent") or "").strip().lower()
        # Invariant: action reflects the filesystem, not the model's guess
        exists = (repo / file).is_file() or file in created
        created.add(file)
        normalised.append(
            {
                "id": "U%d" % index,
                "file": file,
                "symbol": str(unit.get("symbol") or "").strip(),
                "action": "modify" if exists else "create",
                "description": description,
                "signatures": [str(item) for item in unit.get("signatures") or []],
                "suggested_agent": agent if agent in AGENTS else "claude",
                "routing_reason": str(unit.get("routing_reason") or "").strip(),
                "acceptance": [str(item) for item in unit.get("acceptance") or []],
            }
        )
    return {
        "title": str(parsed.get("title") or "").strip() or "Untitled task",
        "summary": str(parsed.get("summary") or "").strip(),
        "units": normalised,
    }


def generate_plan(task, clarifications, repo_map, repo, max_units):
    """Phase 2a: have Agent 0 break the task into atomic units.

    Returns: the normalised plan dict (see normalise_plan()).

    Raises: ValueError if Agent 0 cannot produce a valid plan.
    Side effects: Agent 0 calls only.
    @rollback: none needed.
    """
    answers = "\n".join("- Q: %s\n  A: %s" % (item["question"], item["answer"]) for item in clarifications)
    prompt = (
        "Write the task specification for the task below as one JSON object only:\n"
        '{"title": "...", "summary": "...", "units": [{"file": "repo/relative/path.py", '
        '"symbol": "function or class name, or empty for the whole file", '
        '"description": "exactly what to implement", "signatures": ["def name(arg: type) -> type"], '
        '"suggested_agent": "agy or claude", "routing_reason": "...", "acceptance": ["checkable criterion"]}]}\n\n'
        "Rules:\n"
        "- Each unit is atomic: exactly ONE file, or ONE function/class in one file. Never two files in a unit.\n"
        "- Order units so that anything a later unit depends on comes first.\n"
        "- At most %d units. Use as few as the task needs.\n"
        "- Units are code or document edits only: no units for running commands, git operations, "
        "or files under .git/ or log/.\n"
        "- Give exact signatures for every function or class a unit must define.\n"
        "- %s\n\n"
        "## Task\n%s\n\n## Clarifications\n%s\n\n## Repo map\n%s"
        % (max_units, ROUTING_GUIDE, task, answers or "(none)", repo_map)
    )
    plan = agent0_json(
        [{"role": "system", "content": AGENT0_SYSTEM}, {"role": "user", "content": prompt}],
        lambda parsed: normalise_plan(parsed, repo, max_units),
        "task plan",
    )
    log_event(logger, "plan_generated", "plan: %d unit(s)" % len(plan["units"]), plan=plan)
    return plan


def planned_rollback(repo, unit):
    """Return the rollback command that is expected to apply to a unit, for the spec.

    The command actually valid after execution is computed again at checkpoint time
    (see rollback_command()), because earlier units may change the file's state.

    Side effects: read-only git queries.
    @rollback: none.
    """
    file = unit["file"]
    if unit["action"] == "create":
        return "rm %s" % shlex.quote(file)
    if is_tracked(repo, file) and file not in snapshot(repo):
        return "git checkout -- %s" % shlex.quote(file)
    return "cp log/backups/%s/%s-attempt<N>/%s %s" % (
        get_correlation_id(),
        unit["id"],
        shlex.quote(file),
        shlex.quote(file),
    )


def render_spec(task, clarifications, plan, repo, base_commit):
    """Render the plan as the Markdown task spec.

    Side effects: read-only git queries via planned_rollback().
    @rollback: none (returns text; write_spec() does the writing).
    """
    lines = [
        "# Task spec: %s" % plan["title"],
        "",
        "| Field | Value |",
        "|---|---|",
        "| Correlation ID | `%s` |" % get_correlation_id(),
        "| Created | %s |" % datetime.now().astimezone().isoformat(timespec="seconds"),
        "| Base commit | `%s` |" % base_commit,
        "| Planner | Agent 0 (`%s`) |" % LMSTUDIO_MODEL,
        "",
        "## Task",
        "",
        task,
        "",
        "## Clarifications",
        "",
    ]
    if clarifications:
        for item in clarifications:
            lines += ["- **Q:** %s" % item["question"], "  **A:** %s" % item["answer"]]
    else:
        lines.append("None.")
    lines += ["", "## Summary", "", plan["summary"] or "None.", "", "## Atomic units", ""]
    for unit in plan["units"]:
        target = "`%s`" % unit["file"] + (" → `%s`" % unit["symbol"] if unit["symbol"] else " (whole file)")
        lines += [
            "### %s: %s %s" % (unit["id"], unit["action"], target),
            "",
            unit["description"],
            "",
            "- **Scope:** only `%s` may be created or modified." % unit["file"],
            "- **Suggested agent:** %s — %s" % (unit["suggested_agent"], unit["routing_reason"] or "no reason given"),
        ]
        if unit["signatures"]:
            lines.append("- **Required signatures:**")
            lines += ["  - `%s`" % signature for signature in unit["signatures"]]
        if unit["acceptance"]:
            lines.append("- **Acceptance:**")
            lines += ["  - %s" % criterion for criterion in unit["acceptance"]]
        lines += [
            "- **Rollback checkpoint:** taken immediately before dispatch (HEAD hash plus a copy of the "
            "file under `log/backups/%s/%s-attempt<N>/`)." % (get_correlation_id(), unit["id"]),
            "- **Rollback:** `%s`" % planned_rollback(repo, unit),
            "",
        ]
    return "\n".join(lines)


def write_spec(repo, plan, text):
    """Phase 2b: write the spec to doc/tasks/<timestamp>_<slug>.md and return its path.

    State mutations: creates doc/tasks/ if missing and one new Markdown file in it.
    @rollback: delete the returned file.
    """
    tasks_dir = repo / "doc" / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    path = tasks_dir / ("%s_%s.md" % (datetime.now().strftime("%Y%m%d-%H%M%S"), slugify(plan["title"])))
    path.write_text(text + "\n", encoding="utf-8")
    log_event(logger, "spec_written", "spec written: %s" % path.relative_to(repo), spec_path=str(path), spec=text)
    return path


# --------------------------------------------------------------------------- #
# Worker CLIs (ask_agy / ask_claude)
# --------------------------------------------------------------------------- #


def run_cli(agent, prompt, repo, allow_edits, purpose):
    """Run one headless worker CLI call and return its outcome.

    Args:
        agent: "agy" or "claude".
        prompt: the exact prompt to send.
        repo: working directory for the CLI.
        allow_edits: True lets the CLI write files (agy `--mode accept-edits`,
            claude `--permission-mode acceptEdits`). False leaves it read-only,
            because headless mode denies anything that needs a permission prompt.
        purpose: short label for the logs.

    Returns: dict with ok, text (the agent's reply), returncode, stdout, stderr, duration_s.

    State mutations: with allow_edits=True the worker may change files in `repo`.
        Callers must take a checkpoint() first.
    Side effects: the prompt is sent to the agent's cloud service. Logs the exact
        prompt, argv flags, duration, stdout, stderr and HEAD before/after.
    @rollback: restore from the caller's checkpoint with rollback_unit().
    """
    if agent == "agy":
        flags = ["--model", AGY_MODEL, "--print-timeout", AGY_TIMEOUT]
        if allow_edits:
            flags += ["--mode", "accept-edits"]
        argv = [AGY_BIN, "--print", prompt] + flags
    else:
        flags = ["--output-format", "json"]
        if allow_edits:
            flags += ["--permission-mode", "acceptEdits"]
        if CLAUDE_MODEL:
            flags += ["--model", CLAUDE_MODEL]
        argv = [CLAUDE_BIN, "-p", prompt] + flags

    head_before = git_head(repo)
    log_event(
        logger,
        "cli_call",
        "%s <- %s" % (agent, purpose),
        agent=agent,
        purpose=purpose,
        binary=argv[0],
        flags=flags,
        allow_edits=allow_edits,
        cwd=str(repo),
        git_head_before=head_before,
        prompt=prompt,
    )
    started = time.monotonic()
    # The worker (and any MCP server it starts) logs under the same correlation ID
    environment = dict(os.environ, MAID_CORRELATION_ID=get_correlation_id())
    try:
        completed = subprocess.run(
            argv,
            cwd=str(repo),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=WORKER_TIMEOUT,
            env=environment,
        )
        returncode, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as expired:
        returncode = -1
        stdout = expired.stdout if isinstance(expired.stdout, str) else ""
        stderr = "timed out after %.0fs" % WORKER_TIMEOUT
    except OSError as error:
        returncode, stdout, stderr = -1, "", "could not start %s: %r" % (argv[0], error)

    ok = returncode == 0
    text = stdout.strip()
    if agent == "claude" and ok:
        # `claude -p --output-format json` prints one JSON object; the reply is in "result"
        try:
            parsed = extract_json(stdout)
            text = str(parsed.get("result") or "").strip()
            ok = not parsed.get("is_error", False)
        except ValueError:
            ok = False
    duration = round(time.monotonic() - started, 3)
    log_event(
        logger,
        "cli_result",
        "%s -> %s (%s, %.1fs)" % (agent, purpose, "ok" if ok else "FAILED", duration),
        level=20 if ok else 40,
        agent=agent,
        purpose=purpose,
        ok=ok,
        returncode=returncode,
        duration_s=duration,
        git_head_before=head_before,
        git_head_after=git_head(repo),
        stdout=stdout,
        stderr=stderr,
    )
    return {"ok": ok, "text": text, "returncode": returncode, "stdout": stdout, "stderr": stderr, "duration_s": duration}


# --------------------------------------------------------------------------- #
# Phase 3: routing and isolated execution
# --------------------------------------------------------------------------- #


def route_unit(plan, unit, repo_map, feedback):
    """Phase 3a: have Agent 0 pick a worker for one unit by calling ask_agy or ask_claude.

    Returns: (agent, instructions, reason). Falls back to the spec's suggested agent
        and the unit description if Agent 0 returns no usable tool call.

    Side effects: one Agent 0 call with tool_choice="required".
    @rollback: none needed; no worker has run yet.
    """
    request = (
        "Dispatch this atomic unit by calling exactly one tool: ask_agy or ask_claude. "
        "%s Write `prompt` as complete, self-contained instructions for implementing "
        "this unit only.\n\n## Spec summary\n%s: %s\n\n## Unit\n%s\n\n## Repo map\n%s"
        % (ROUTING_GUIDE, plan["title"], plan["summary"], json.dumps(unit, indent=2), repo_map)
    )
    if feedback:
        request += (
            "\n\n## Previous attempt failed and was rolled back\n%s\n"
            "Address this in the new prompt. You may switch to the other agent." % feedback
        )
    try:
        reply = lmstudio_chat(
            logger,
            [{"role": "system", "content": AGENT0_SYSTEM}, {"role": "user", "content": request}],
            tools=WORKER_TOOLS,
            tool_choice="required",
            purpose="route %s" % unit["id"],
        )
        call = (reply.get("tool_calls") or [])[0]["function"]
        arguments = json.loads(call["arguments"])
        agent = call["name"].replace("ask_", "")
        if agent not in AGENTS or not str(arguments.get("prompt") or "").strip():
            raise ValueError("unusable tool call: %r" % call)
        choice = (agent, arguments["prompt"].strip(), str(arguments.get("reason") or "").strip())
        source = "agent0_tool_call"
    except (IndexError, KeyError, TypeError, ValueError) as error:
        log_event(logger, "route_fallback", "no usable tool call; using the spec's suggestion", level=30, error=repr(error))
        choice = (unit["suggested_agent"], unit["description"], unit["routing_reason"] or "spec suggestion")
        source = "spec_fallback"
    log_event(
        logger,
        "agent_selected",
        "%s -> %s (%s)" % (unit["id"], choice[0], choice[2] or "no reason given"),
        unit=unit["id"],
        agent=choice[0],
        reason=choice[2],
        source=source,
        suggested_agent=unit["suggested_agent"],
    )
    return choice


def build_worker_prompt(unit, instructions, spec_path, spec_text, repo_map, repo):
    """Wrap Agent 0's instructions with the isolation rules, the spec and the repo map.

    Invariant: every worker call carries the same isolation rules, the full spec and
    the repo map, whatever Agent 0 wrote.

    @rollback: none (pure).
    """
    target = unit["file"] + (" (only the symbol `%s`)" % unit["symbol"] if unit["symbol"] else "")
    return (
        "You are a worker agent in MAID executing ONE atomic unit of a larger task.\n\n"
        "## Isolation rules (mandatory)\n"
        "1. %s ONLY this target: %s\n"
        "2. Do not create, edit, rename or delete any other file. Other units are handled separately.\n"
        "3. Do not run git commands that change state (commit, add, stash, reset, checkout) "
        "and do not install packages.\n"
        "4. Include production-grade rollback comments in what you write: a docstring or header "
        "comment with an `@rollback` line stating what state the code mutates, its side effects, "
        "and how to revert it.\n"
        "5. Match the style of the surrounding code. When done, reply with a short summary of what you changed.\n\n"
        "## Assigned unit (%s)\n%s\n\n"
        "## Instructions from Agent 0\n%s\n\n"
        "## Task spec (%s)\n%s\n\n"
        "## Repo map\n%s\n"
        % (
            unit["action"].capitalize(),
            target,
            unit["id"],
            json.dumps(unit, indent=2),
            instructions,
            spec_path.relative_to(repo),
            spec_text,
            repo_map,
        )
    )


# --------------------------------------------------------------------------- #
# Phase 4: checkpoint, verification, rollback
# --------------------------------------------------------------------------- #


def checkpoint(repo, unit, attempt):
    """Record the git state and back up file contents before a worker runs.

    Returns: dict with head, before (snapshot), backup_dir, target_existed, target_text.

    State mutations: copies the target file and every already-dirty file to
        log/backups/<correlation-id>/<unit>-attempt<N>/ (git-ignored).
    @rollback: delete that backup directory; nothing in the repo is changed.
    """
    before = snapshot(repo)
    backup_dir = LOG_DIR / "backups" / get_correlation_id() / ("%s-attempt%d" % (unit["id"], attempt))
    target = repo / unit["file"]
    for path in set(before) | {unit["file"]}:
        source = repo / path
        if source.is_file():
            destination = backup_dir / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    state = {
        "head": git_head(repo),
        "before": before,
        "backup_dir": backup_dir,
        "target_existed": target.is_file(),
        "target_text": read_text(target),
    }
    log_event(
        logger,
        "checkpoint",
        "%s attempt %d: checkpoint at %s" % (unit["id"], attempt, state["head"][:10]),
        unit=unit["id"],
        attempt=attempt,
        git_head=state["head"],
        dirty_paths=sorted(before),
        backup_dir=str(backup_dir),
        target_existed=state["target_existed"],
    )
    return state


def rollback_command(repo, unit, state):
    """Return the one shell command that undoes this unit's change to its target file.

    @rollback: none (pure apart from a read-only git query).
    """
    file = unit["file"]
    if not state["target_existed"]:
        return "rm %s" % shlex.quote(file)
    if file not in state["before"] and is_tracked(repo, file):
        return "git checkout -- %s" % shlex.quote(file)
    return "cp %s %s" % (shlex.quote(str(state["backup_dir"] / file)), shlex.quote(str(repo / file)))


def restore_path(repo, path, state):
    """Put one path back to its checkpointed state and return a description of how.

    State mutations: overwrites or deletes `path` in the working tree; for a path that
        was clean at the checkpoint, also resets its index entry to HEAD.
    @rollback: not reversible; the worker's version of the path is discarded. That is
        the purpose of this function, and the discarded content is in the logs' diff.
    """
    target = repo / path
    backup = state["backup_dir"] / path
    if backup.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, target)
        return "restored from backup"
    if path not in state["before"] and is_tracked(repo, path):
        # Clean at the checkpoint, so HEAD is exactly the checkpointed content
        git(repo, "checkout", "HEAD", "--", path)
        return "git checkout HEAD"
    # Did not exist at the checkpoint (new file, or a tracked file already deleted)
    if target.is_file():
        target.unlink()
    return "removed"


def rollback_unit(repo, unit, state, paths):
    """Restore every path a worker touched to its checkpointed state.

    State mutations: see restore_path(), applied to each of `paths`.
    Side effects: one log record listing what was restored and how.
    @rollback: not reversible (see restore_path()).
    """
    actions = {path: restore_path(repo, path, state) for path in paths}
    log_event(
        logger,
        "rollback",
        "%s: rolled back %d path(s)" % (unit["id"], len(actions)),
        level=30,
        unit=unit["id"],
        actions=actions,
        git_head=git_head(repo),
    )


def check_syntax(repo, file):
    """Syntax-check one file by extension and return (ok, detail).

    Python is compiled in-process (nothing is written), JSON is parsed, JavaScript
    goes through `node --check` when node is installed. Other types are skipped.

    Side effects: may run `node --check` (read-only).
    @rollback: none.
    """
    path = repo / file
    suffix = path.suffix.lower()
    try:
        if suffix == ".py":
            compile(read_text(path), str(path), "exec")
            return True, "python syntax ok"
        if suffix == ".json":
            json.loads(read_text(path))
            return True, "json parses"
        if suffix in (".js", ".mjs", ".cjs") and shutil.which("node"):
            result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
            return result.returncode == 0, (result.stderr.strip() or "node --check ok")
    except (SyntaxError, ValueError) as error:
        return False, "%s: %s" % (type(error).__name__, error)
    return True, "no syntax checker for %s files; skipped" % (suffix or "extensionless")


def review_diff(repo, unit, diff, worker_agent):
    """Have the agent that did not write the change review its diff.

    Returns: (ok, detail). An unusable review (CLI failure or no verdict) is reported
        as inconclusive and does not fail the unit, since the mechanical checks
        have already passed by the time this runs.

    Side effects: one read-only worker CLI call; the diff leaves this machine.
    @rollback: none needed (the reviewer runs without edit permission).
    """
    reviewer = "claude" if worker_agent == "agy" else "agy"
    prompt = (
        "You are reviewing one atomic change made by another agent. Do not modify any file.\n"
        "Judge only this: does the diff implement the unit below correctly, with no obvious bugs, "
        "staying inside the unit's scope, and with `@rollback` comments?\n"
        "Reply with the first line exactly `VERDICT: PASS` or `VERDICT: FAIL`, then your reasons briefly.\n\n"
        "## Unit\n%s\n\n## Diff\n%s" % (json.dumps(unit, indent=2), diff[:DIFF_LIMIT])
    )
    result = run_cli(reviewer, prompt, repo, allow_edits=False, purpose="review %s" % unit["id"])
    verdict = re.search(r"VERDICT:\s*(PASS|FAIL)", result["text"]) if result["ok"] else None
    if not verdict:
        return True, "inconclusive review by %s (no verdict returned)" % reviewer
    return verdict.group(1) == "PASS", "%s review: %s" % (reviewer, result["text"])


def verify_unit(repo, unit, state, worker_agent, args):
    """Phase 4: run every check on a finished unit.

    Checks, in order (later ones are skipped once one fails): the worker stayed in
    scope, the target actually changed, syntax, the optional --test-cmd, and the
    cross-agent diff review.

    Returns: (passed, checks, touched) where checks is a list of
        {"name", "ok", "detail"} and touched is every path the worker changed.

    Side effects: may run --test-cmd in the repo and one reviewer CLI call.
    @rollback: none needed; this function changes nothing itself.
    """
    file = unit["file"]
    touched = touched_paths(state["before"], snapshot(repo))
    after_text = read_text(repo / file)
    checks = []

    def record(name, ok, detail):
        checks.append({"name": name, "ok": ok, "detail": detail})
        return ok

    out_of_scope = [path for path in touched if path != file]
    passed = record(
        "scope",
        not out_of_scope,
        "only %s changed" % file if not out_of_scope else "out-of-scope changes: %s" % ", ".join(out_of_scope),
    )
    changed = (repo / file).is_file() and after_text != state["target_text"]
    passed = record("target_changed", changed, "%s was written" % file if changed else "%s is missing or unchanged" % file) and passed
    if passed:
        passed = record("syntax", *check_syntax(repo, file))
    if passed and args.test_cmd:
        try:
            test = subprocess.run(
                shlex.split(args.test_cmd), cwd=str(repo), capture_output=True, text=True, timeout=WORKER_TIMEOUT
            )
            output = (test.stdout + test.stderr).strip()
            passed = record("tests", test.returncode == 0, "exit %d\n%s" % (test.returncode, output[-4000:]))
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            passed = record("tests", False, "could not run %r: %r" % (args.test_cmd, error))
    diff = "".join(
        difflib.unified_diff(
            state["target_text"].splitlines(keepends=True),
            after_text.splitlines(keepends=True),
            "a/" + file,
            "b/" + file,
        )
    )
    if passed and not args.no_review:
        passed = record("review", *review_diff(repo, unit, diff, worker_agent))

    log_event(
        logger,
        "verification",
        "%s: verification %s" % (unit["id"], "passed" if passed else "FAILED"),
        level=20 if passed else 40,
        unit=unit["id"],
        passed=passed,
        checks=checks,
        touched=touched,
        diff=diff,
        git_head_before=state["head"],
        git_head_after=git_head(repo),
    )
    return passed, checks, touched


def execute_unit(plan, unit, repo, repo_map, spec_path, args):
    """Run Phases 3 and 4 for one unit, retrying after a rollback on failure.

    Returns: dict with unit, status ("passed" or "failed"), agent, attempts, checks,
        and rollback (the command to undo a passed unit, or a note for a failed one).

    State mutations: the worker's edit to the unit's file, kept only if verification
        passes. A failed attempt is rolled back before the next one starts.
    @rollback: run the returned `rollback` command to undo a passed unit.
    """
    feedback = ""
    agent, checks = unit["suggested_agent"], []
    for attempt in range(1, args.max_retries + 2):
        agent, instructions, _ = route_unit(plan, unit, repo_map, feedback)
        print("[%s] attempt %d -> %s: %s %s" % (unit["id"], attempt, agent, unit["action"], unit["file"]))
        # Invariant: the checkpoint is taken after routing and immediately before the worker runs
        state = checkpoint(repo, unit, attempt)
        prompt = build_worker_prompt(unit, instructions, spec_path, read_text(spec_path), repo_map, repo)
        result = run_cli(agent, prompt, repo, allow_edits=True, purpose="execute %s" % unit["id"])
        passed, checks, touched = verify_unit(repo, unit, state, agent, args)
        if passed:
            command = rollback_command(repo, unit, state)
            log_event(logger, "unit_passed", "%s passed" % unit["id"], unit=unit["id"], agent=agent, attempt=attempt, rollback_command=command)
            print("[%s] passed. Rollback: %s" % (unit["id"], command))
            return {"unit": unit, "status": "passed", "agent": agent, "attempts": attempt, "checks": checks, "rollback": command}

        failures = "; ".join("%s: %s" % (check["name"], check["detail"]) for check in checks if not check["ok"])
        if not result["ok"]:
            failures += "; worker CLI error: %s" % (result["stderr"].strip() or result["text"])[:1000]
        rollback_unit(repo, unit, state, touched)
        print("[%s] attempt %d FAILED and was rolled back: %s" % (unit["id"], attempt, failures[:300]))
        feedback = failures

    log_event(logger, "unit_failed", "%s failed after %d attempt(s)" % (unit["id"], args.max_retries + 1), level=40, unit=unit["id"], last_failure=feedback)
    return {
        "unit": unit,
        "status": "failed",
        "agent": agent,
        "attempts": args.max_retries + 1,
        "checks": checks,
        "rollback": "already rolled back automatically",
    }


def append_results(spec_path, results, plan):
    """Append the run's outcome table to the spec file.

    State mutations: appends an "Execution results" section to the spec.
    @rollback: delete that section from the spec file (it is always the last one).
    """
    lines = ["", "## Execution results", "", "| Unit | File | Status | Agent | Attempts | Rollback |", "|---|---|---|---|---|---|"]
    done = {result["unit"]["id"]: result for result in results}
    for unit in plan["units"]:
        result = done.get(unit["id"])
        if result:
            lines.append(
                "| %s | `%s` | %s | %s | %d | `%s` |"
                % (unit["id"], unit["file"], result["status"], result["agent"], result["attempts"], result["rollback"])
            )
        else:
            lines.append("| %s | `%s` | not run | | | |" % (unit["id"], unit["file"]))
    with spec_path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def parse_args(argv):
    """Parse the command line.

    @rollback: none (pure).
    """
    parser = argparse.ArgumentParser(description="MAID three-agent orchestrator (Agent 0 + agy + claude).")
    parser.add_argument("task", help="the task, in plain language")
    parser.add_argument("--no-clarify", action="store_true", help="skip Phase 1 clarifying questions")
    parser.add_argument("--plan-only", action="store_true", help="stop after writing the spec; run no workers")
    parser.add_argument("--no-review", action="store_true", help="skip the cross-agent diff review")
    parser.add_argument("--test-cmd", default="", help='command to run after each unit, e.g. "pytest -q"')
    parser.add_argument("--max-retries", type=int, default=1, help="retries per unit after a rollback (default 1)")
    parser.add_argument("--max-units", type=int, default=8, help="most atomic units a plan may have (default 8)")
    parser.add_argument("--repo", default=str(REPO_ROOT), help="repository to work in (default: this repo)")
    return parser.parse_args(argv)


def main(argv=None):
    """Run the four phases and return the process exit code.

    Returns: 0 if every unit passed (or --plan-only), 1 if a unit failed, 2 on a
        setup or planning error.

    State mutations / side effects: see the module docstring.
    @rollback: see the module docstring.
    """
    args = parse_args(sys.argv[1:] if argv is None else argv)
    new_correlation_id()
    repo = Path(args.repo).resolve()
    base_commit = git_head(repo)
    log_event(
        logger,
        "run_start",
        "run %s started" % get_correlation_id(),
        argv=sys.argv,
        flags=vars(args),
        repo=str(repo),
        git_head=base_commit,
        agent0_model=LMSTUDIO_MODEL,
        agy_model=AGY_MODEL,
        claude_model=CLAUDE_MODEL or "(default)",
    )
    if not base_commit:
        log_event(logger, "run_error", "%s is not a git repository with at least one commit" % repo, level=40)
        return 2

    results, spec_path = [], None
    try:
        repo_map = build_repo_map(repo)
        clarifications = [] if args.no_clarify else clarify(args.task, repo_map)
        plan = generate_plan(args.task, clarifications, repo_map, repo, args.max_units)
        spec_path = write_spec(repo, plan, render_spec(args.task, clarifications, plan, repo, base_commit))
        print("\nSpec: %s (%d unit(s))" % (spec_path.relative_to(repo), len(plan["units"])))
        if not args.plan_only:
            # The spec now exists, so workers see it in the repo map too
            repo_map = build_repo_map(repo)
            for unit in plan["units"]:
                results.append(execute_unit(plan, unit, repo, repo_map, spec_path, args))
                # Invariant: later units may depend on earlier ones, so stop at the first failure
                if results[-1]["status"] != "passed":
                    break
            append_results(spec_path, results, plan)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        log_event(logger, "run_error", "run aborted: %s" % error, level=40, error=repr(error))
        logger.debug("traceback", exc_info=True)
        return 2

    failed = [result for result in results if result["status"] != "passed"]
    log_event(
        logger,
        "run_end",
        "run %s finished: %d passed, %d failed" % (get_correlation_id(), len(results) - len(failed), len(failed)),
        level=40 if failed else 20,
        spec_path=str(spec_path),
        git_head_before=base_commit,
        git_head_after=git_head(repo),
        results=[dict(result, unit=result["unit"]["id"]) for result in results],
    )
    print("\nRun %s" % get_correlation_id())
    for result in results:
        print("  %s %-6s %s via %s, %d attempt(s). Rollback: %s"
              % (result["unit"]["id"], result["status"], result["unit"]["file"], result["agent"], result["attempts"], result["rollback"]))
    print("Logs: %s and %s" % (LOG_DIR / "maid_execution.jsonl", LOG_DIR / "maid_human.log"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

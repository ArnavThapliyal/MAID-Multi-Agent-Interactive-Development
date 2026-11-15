"""MAID benchmark suite: speed and resource metrics you can compare across setups.

Purpose
    Runs a fixed set of measurements against the three agents and writes one result
    folder per run:
        benchmarks/results/<date>_<chip>_<model>[_<label>]/
            system.json     hardware, OS, model and tool versions (no hostname or user)
            metrics.json    every number, plus a flat "headline" block for comparison
            samples.csv     CPU and memory samples taken every two seconds
            summary.md      the tables a person reads
            screenshots/    dashboard states captured during the end-to-end run

Benchmarks
    B1 local      time to first token and tokens/second over fixed prompts
    B2 planning   clarify and plan latency; whether the plan was valid first try
    B3 routing    tool-call latency and validity per unit
    B4 workers    agy and claude round-trip latency; claude cost per call
    B5 end2end    a fixed two-unit task in a throwaway repo, dashboard attached
    B6 overhead   B1's code prompt with and without the dashboard attached,
                  interleaved, to show what the dashboard costs the local model

Usage
    python3 benchmarks/bench.py                 full suite, about 20-25 minutes
    python3 benchmarks/bench.py --quick         B1 and B6 only, about 4 minutes
    python3 benchmarks/bench.py --label m4pro-48gb
    python3 benchmarks/compare.py               compare every result folder

Requirements
    LM Studio serving the model; for the full suite also agy and claude, signed in.
    A Chromium-family browser is needed only for screenshots (MAID_BROWSER_BIN
    overrides auto-detection); without one the suite still runs.

State mutations
    * Creates one result folder under benchmarks/results/ (or --out).
    * Creates and deletes a throwaway git repo under the system temp directory.
    * Serves the dashboard on 127.0.0.1:<--port> for B5 and B6, then stops it.
    * Appends to the MAID logs under log/ like any other run.

Side effects
    B4 and B5 send prompts to agy and Claude Code and use those accounts.

@rollback
    Delete the result folder. Nothing in the MAID repository itself is modified.
    To remove the suite, `git rm -r benchmarks/`. Standard library only, Python 3.9+.
"""
import argparse
import csv
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "bridge"))
sys.path.insert(0, str(BENCH_DIR))

import maid_common  # noqa: E402
import maid_ui  # noqa: E402
import orchestrator  # noqa: E402
from cdp import CDP, find_browser  # noqa: E402
from maid_runtime import RunControl  # noqa: E402

FIXTURES = BENCH_DIR / "fixtures"
SAMPLE_SECONDS = 2.0

# (label, key in the headline block, unit, which direction is better)
HEADLINE_ROWS = [
    ("Local tokens/second (median)", "local_tok_s", "tok/s", "higher"),
    ("Local time to first token (median)", "local_ttft_s", "s", "lower"),
    ("Clarify latency", "clarify_latency_s", "s", "lower"),
    ("Plan latency", "plan_latency_s", "s", "lower"),
    ("Plan valid on first try", "plan_valid_first_try", "", "higher"),
    ("Routing latency per unit (median)", "route_latency_s", "s", "lower"),
    ("Routing tool calls valid", "route_valid_pct", "%", "higher"),
    ("agy round trip (median)", "agy_latency_s", "s", "lower"),
    ("claude round trip (median)", "claude_latency_s", "s", "lower"),
    ("claude cost per call", "claude_cost_per_call_usd", "$", "lower"),
    ("End to end: wall time", "e2e_wall_s", "s", "lower"),
    ("End to end: units passed", "e2e_units_passed", "", "higher"),
    ("End to end: worker attempts", "e2e_attempts", "", "lower"),
    ("End to end: Claude cost", "e2e_cost_usd", "$", "lower"),
    ("Dashboard overhead on tokens/second", "dashboard_overhead_pct", "%", "lower"),
    ("LM Studio CPU, peak", "lmstudio_cpu_peak_pct", "%", ""),
    ("LM Studio resident memory, peak", "lmstudio_rss_peak_mb", "MB", "lower"),
    ("Available memory, minimum", "available_mem_min_mb", "MB", "higher"),
    ("MAID process CPU, average", "maid_cpu_avg_pct", "%", "lower"),
]

logger = maid_common.get_maid_logger("bench", console="stderr")


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested in tests/test_benchmarks.py)
# --------------------------------------------------------------------------- #


def slug(text, limit=40):
    """Turn free text into a short lowercase folder-name fragment.

    @rollback: none (pure).
    """
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return cleaned[:limit].strip("-") or "unknown"


def median(values, digits=2):
    """Return the median of the non-None values, rounded, or None if there are none.

    @rollback: none (pure).
    """
    values = [value for value in values if value is not None]
    return round(statistics.median(values), digits) if values else None


def pct_change(base, value, digits=1):
    """Return how much `value` differs from `base` in percent, or None if undefined.

    @rollback: none (pure).
    """
    if base in (None, 0) or value is None:
        return None
    return round((value - base) / base * 100.0, digits)


def overhead_pct(bare_tok_s, attached_tok_s):
    """Return the tokens/second lost with the dashboard attached, in percent of bare.

    Positive means the dashboard slowed generation; values within a few percent of
    zero are run-to-run noise.

    @rollback: none (pure).
    """
    change = pct_change(bare_tok_s, attached_tok_s)
    return None if change is None else round(-change, 1)


def phase_of(purpose):
    """Map a call's purpose label to an end-to-end phase name.

    @rollback: none (pure).
    """
    purpose = purpose or ""
    if purpose.startswith("clarifying"):
        return "clarify"
    if purpose.startswith("task plan"):
        return "plan"
    if purpose.startswith("route"):
        return "route"
    if purpose.startswith("execute"):
        return "execute"
    if purpose.startswith("review") or purpose.startswith("diff audit"):
        return "review"
    return "other"


def headline(results, samples):
    """Flatten the benchmark results into the comparable headline block.

    Args:
        results: {"b1": {...}, ...} as produced by the benchmark functions; a
            benchmark that was skipped or failed is simply absent or has "error".
        samples: resource samples (dicts with "groups" and "memory").

    Returns: {key: number or None} for every key in HEADLINE_ROWS.

    @rollback: none (pure).
    """
    def ok(name):
        value = results.get(name) or {}
        return {} if "error" in value else value

    b1, b2, b3, b4, b5, b6 = (ok(name) for name in ("b1", "b2", "b3", "b4", "b5", "b6"))
    runs = [run for prompt in b1.get("prompts", []) for run in prompt["runs"]]
    routes = b3.get("units", [])
    lm_cpu = [sample["groups"]["lmstudio"]["cpu"] for sample in samples]
    lm_rss = [sample["groups"]["lmstudio"]["rss_mb"] for sample in samples]
    maid_cpu = [sample["groups"]["orchestrator"]["cpu"] + sample["groups"]["dashboard"]["cpu"] for sample in samples]
    available = [sample["memory"]["available_mb"] for sample in samples if sample.get("memory", {}).get("available_mb") is not None]
    return {
        "local_tok_s": median([run["tok_s"] for run in runs]),
        "local_ttft_s": median([run["ttft_s"] for run in runs]),
        "clarify_latency_s": b2.get("clarify_latency_s"),
        "plan_latency_s": b2.get("plan_latency_s"),
        "plan_valid_first_try": b2.get("plan_valid_first_try"),
        "route_latency_s": median([route["latency_s"] for route in routes]),
        "route_valid_pct": round(100.0 * sum(1 for route in routes if route["valid"]) / len(routes)) if routes else None,
        "agy_latency_s": median([call["latency_s"] for call in b4.get("agy", []) if call["ok"]]),
        "claude_latency_s": median([call["latency_s"] for call in b4.get("claude", []) if call["ok"]]),
        "claude_cost_per_call_usd": median([call.get("cost_usd") for call in b4.get("claude", [])], 4),
        "e2e_wall_s": b5.get("wall_s"),
        "e2e_units_passed": b5.get("units_passed"),
        "e2e_attempts": b5.get("attempts"),
        "e2e_cost_usd": b5.get("cost_usd"),
        "dashboard_overhead_pct": b6.get("overhead_pct"),
        "lmstudio_cpu_peak_pct": round(max(lm_cpu)) if lm_cpu else None,
        "lmstudio_rss_peak_mb": round(max(lm_rss)) if lm_rss else None,
        "available_mem_min_mb": min(available) if available else None,
        "maid_cpu_avg_pct": round(sum(maid_cpu) / len(maid_cpu), 1) if maid_cpu else None,
    }


def show(value, unit=""):
    """Format one headline value for a table cell.

    @rollback: none (pure).
    """
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if unit == "$":
        return "$%.3f" % value
    text = ("%.2f" % value).rstrip("0").rstrip(".") if isinstance(value, float) else str(value)
    return text + (" " + unit if unit and unit != "%" else unit)


def render_summary(system, numbers, results, shots):
    """Render summary.md.

    @rollback: none (pure).
    """
    lines = [
        "# MAID benchmark: %s" % system["label"],
        "",
        "Run on %s with MAID `%s`." % (system["date"], system["maid_commit"]),
        "",
        "## System",
        "",
        "| Field | Value |",
        "|---|---|",
    ]
    for label, key in (
        ("Machine", "machine"), ("Chip", "chip"), ("CPU cores", "cores"), ("Memory", "memory_gb"), ("OS", "os"),
        ("Python", "python"), ("Local model", "local_model"), ("Quantisation", "quantization"),
        ("Context length", "context_length"), ("Model size", "model_size_gb"), ("Reasoning effort", "reasoning_effort"),
        ("agy", "agy_version"), ("agy model", "agy_model"), ("claude", "claude_version"), ("claude model", "claude_model"),
    ):
        value = system.get(key)
        if key in ("memory_gb", "model_size_gb") and value is not None:
            value = "%s GB" % value
        lines.append("| %s | %s |" % (label, "n/a" if value in (None, "") else value))
    lines += ["", "## Headline", "", "| Metric | Value |", "|---|---|"]
    lines += ["| %s | %s |" % (label, show(numbers.get(key), unit)) for label, key, unit, _ in HEADLINE_ROWS]

    b1 = results.get("b1") or {}
    if b1.get("prompts"):
        lines += ["", "## B1 Local model", "", "| Prompt | Runs | tokens/s (median) | First token (median) | Output tokens (median) |", "|---|---|---|---|---|"]
        for prompt in b1["prompts"]:
            lines.append("| %s | %d | %s | %s | %s |" % (
                prompt["id"], len(prompt["runs"]), show(prompt["tok_s"]), show(prompt["ttft_s"], "s"), show(prompt["completion_tokens"])))
    b5 = results.get("b5") or {}
    if b5.get("phases"):
        lines += ["", "## B5 End to end", "", "Task: %s" % b5["task"], "", "| Phase | Seconds | Calls |", "|---|---|---|"]
        lines += ["| %s | %s | %d |" % (name, show(phase["seconds"]), phase["calls"]) for name, phase in b5["phases"].items()]
        lines += ["", "| Unit | File | Agent | Status | Attempts |", "|---|---|---|---|---|"]
        lines += ["| %s | `%s` | %s | %s | %d |" % (unit["id"], unit["file"], unit["agent"], unit["status"], unit["attempt"]) for unit in b5["units"]]
    b6 = results.get("b6") or {}
    if b6.get("bare"):
        lines += [
            "", "## B6 Dashboard overhead", "",
            "The same prompt, alternating between no dashboard and the dashboard attached (%s)." % b6["attached_client"],
            "", "| Condition | tokens/s per run | Median |", "|---|---|---|",
            "| No dashboard | %s | %s |" % (", ".join(show(value) for value in b6["bare"]), show(b6["bare_tok_s"])),
            "| Dashboard attached | %s | %s |" % (", ".join(show(value) for value in b6["attached"]), show(b6["attached_tok_s"])),
            "", "Overhead: %s. Differences of a few percent either way are run-to-run noise." % show(b6["overhead_pct"], "%"),
        ]
    failed = {name: value["error"] for name, value in results.items() if isinstance(value, dict) and "error" in value}
    if failed:
        lines += ["", "## Benchmarks that did not complete", ""] + ["- **%s**: %s" % item for item in failed.items()]
    if shots:
        lines += ["", "## Screenshots", ""] + ["- [%s](screenshots/%s)" % (name, name) for name in shots]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# System information
# --------------------------------------------------------------------------- #


def _run(argv, timeout=15):
    """Return a command's stripped stdout, or "" if it fails or is missing.

    @rollback: none (the commands used are read-only).
    """
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def system_info(label=""):
    """Collect hardware, OS, model and tool versions. No hostname, user or paths.

    Side effects: a few read-only subprocesses (sysctl, lms, agy, claude, git).
    @rollback: none.
    """
    models = maid_common.get_models()
    info = {
        "date": datetime.now().astimezone().isoformat(timespec="minutes"),
        "machine": _run(["sysctl", "-n", "hw.model"]) or platform.machine(),
        "chip": _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or platform.processor() or platform.machine(),
        "cores": os.cpu_count(),
        "memory_gb": None,
        "os": "%s %s" % (platform.system(), platform.mac_ver()[0] or platform.release()),
        "python": platform.python_version(),
        "local_model": models["local"],
        "quantization": "",
        "context_length": None,
        "model_size_gb": None,
        "reasoning_effort": maid_common.LMSTUDIO_REASONING or "(default)",
        "max_tokens": maid_common.LMSTUDIO_MAX_TOKENS,
        "agy_model": models["agy"],
        "claude_model": models["claude"] or "default",
        "agy_version": (_run([orchestrator.AGY_BIN, "--version"]).splitlines() or [""])[0],
        "claude_version": (_run([orchestrator.CLAUDE_BIN, "--version"]).splitlines() or [""])[0],
        "maid_commit": _run(["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"]) or "unknown",
    }
    memory = _run(["sysctl", "-n", "hw.memsize"])
    if memory.isdigit():
        info["memory_gb"] = round(int(memory) / 1024 ** 3)
    performance, efficiency = _run(["sysctl", "-n", "hw.perflevel0.physicalcpu"]), _run(["sysctl", "-n", "hw.perflevel1.physicalcpu"])
    if performance.isdigit() and efficiency.isdigit():
        info["cores"] = "%s (%s performance + %s efficiency)" % (info["cores"], performance, efficiency)
    lms = shutil.which("lms") or str(Path.home() / ".lmstudio" / "bin" / "lms")
    try:
        loaded = json.loads(_run([lms, "ps", "--json"]) or "[]")
    except ValueError:
        loaded = []
    for entry in loaded:
        if entry.get("identifier") == models["local"] or entry.get("modelKey") == models["local"]:
            info["quantization"] = (entry.get("quantization") or {}).get("name", "")
            info["context_length"] = entry.get("contextLength")
            if entry.get("sizeBytes"):
                info["model_size_gb"] = round(entry["sizeBytes"] / 1e9, 2)
    info["label"] = label or "%s, %s" % (info["chip"], info["local_model"])
    return info


# --------------------------------------------------------------------------- #
# Monitoring during the run
# --------------------------------------------------------------------------- #


class Monitor(threading.Thread):
    """Samples CPU and memory every SAMPLE_SECONDS and tags each sample with the phase.

    State mutations: in-memory sample list only.
    @rollback: `stop()`.
    """

    def __init__(self):
        super().__init__(daemon=True, name="bench-monitor")
        self.sampler = maid_common.ResourceSampler(interval=SAMPLE_SECONDS, log=False)
        self.phase = "setup"
        self.started = time.time()
        self.samples = []
        self._stop_event = threading.Event()

    def run(self):
        while not self._stop_event.wait(SAMPLE_SECONDS):
            sample = self.sampler.sample_once()
            sample["phase"] = self.phase
            self.samples.append(sample)

    def stop(self):
        """Stop sampling.

        @rollback: none needed.
        """
        self._stop_event.set()

    def write_csv(self, path):
        """Write the samples to `path` as CSV.

        State mutations: creates or replaces that file.
        @rollback: delete the file.
        """
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow([
                "t_s", "phase", "lmstudio_cpu_pct", "lmstudio_rss_mb", "maid_cpu_pct", "maid_rss_mb", "agy_cpu_pct",
                "claude_cpu_pct", "available_mb", "wired_mb", "compressed_mb", "load1", "local_tok_s",
            ])
            for sample in self.samples:
                groups, memory = sample["groups"], sample.get("memory", {})
                writer.writerow([
                    round(sample["at"] - self.started, 1), sample["phase"], groups["lmstudio"]["cpu"], groups["lmstudio"]["rss_mb"],
                    round(groups["orchestrator"]["cpu"] + groups["dashboard"]["cpu"], 1),
                    round(groups["orchestrator"]["rss_mb"] + groups["dashboard"]["rss_mb"], 1),
                    groups["agy"]["cpu"], groups["claude"]["cpu"], memory.get("available_mb", ""), memory.get("wired_mb", ""),
                    memory.get("compressed_mb", ""), sample.get("load1", ""), sample.get("local_tok_s", ""),
                ])


class _Unattended:
    """Answers the prompts a run can raise, so a benchmark never waits for a person."""

    def ask_questions(self, questions):
        return [""] * len(questions)

    def ask_permission(self, payload):
        return "deny"


class BareControl(_Unattended, RunControl):
    """Run driver with no dashboard attached.

    @rollback: `orchestrator.set_control(RunControl())`.
    """


class DashboardControl(_Unattended, maid_ui.UIControl):
    """Run driver that feeds the dashboard but answers prompts itself.

    @rollback: `orchestrator.set_control(RunControl())`.
    """


# --------------------------------------------------------------------------- #
# Benchmarks
# --------------------------------------------------------------------------- #


def local_call(prompt, purpose):
    """Run one streamed local completion and return its measurements.

    Side effects: one LM Studio request.
    @rollback: none needed.
    """
    maid_common.lmstudio_chat(logger, [{"role": "user", "content": prompt}], purpose=purpose, stream=True)
    record = maid_common.TELEMETRY.snapshot()["calls"][-1]
    return {key: record.get(key) for key in ("tok_s", "ttft_s", "duration_s", "prompt_tokens", "completion_tokens")}


def b1_local(prompts, repeats):
    """B1: time to first token and tokens/second for each fixed prompt.

    @rollback: none needed.
    """
    out = []
    for prompt in prompts:
        runs = [local_call(prompt["prompt"], "bench b1 %s" % prompt["id"]) for _ in range(repeats)]
        print("  %-6s %s tok/s, first token %s s" % (prompt["id"], median([run["tok_s"] for run in runs]), median([run["ttft_s"] for run in runs])), flush=True)
        out.append({
            "id": prompt["id"], "runs": runs, "tok_s": median([run["tok_s"] for run in runs]),
            "ttft_s": median([run["ttft_s"] for run in runs]), "completion_tokens": median([run["completion_tokens"] for run in runs], 0),
        })
    return {"prompts": out, "repeats": repeats}


def b2_planning(repo, task):
    """B2: how long Agent 0 takes to ask its questions and to produce a valid plan.

    Returns: the measurements plus the plan itself under "plan" (used by B3).
    @rollback: none needed; nothing is written to the repo.
    """
    repo_map = orchestrator.build_repo_map(repo)
    calls = lambda: len(maid_common.TELEMETRY.snapshot()["calls"])  # noqa: E731
    started = time.monotonic()
    questions = orchestrator.clarify(task, repo_map)
    clarify_s = time.monotonic() - started
    before = calls()
    started = time.monotonic()
    plan = orchestrator.generate_plan(task, [], repo_map, repo, 8)
    plan_s = time.monotonic() - started
    attempts = calls() - before
    return {
        "clarify_latency_s": round(clarify_s, 1), "questions": len(questions), "plan_latency_s": round(plan_s, 1),
        "plan_attempts": attempts, "plan_valid_first_try": attempts == 1, "units": len(plan["units"]), "plan": plan,
    }


def b3_routing(repo, plan):
    """B3: per unit, how long Agent 0 takes to dispatch it and whether it made a valid tool call.

    @rollback: none needed; no worker is started.
    """
    repo_map = orchestrator.build_repo_map(repo)
    units = []
    for unit in plan["units"]:
        started = time.monotonic()
        agent, instructions, _ = orchestrator.route_unit(plan, unit, repo_map, "")
        units.append({
            "id": unit["id"], "agent": agent, "latency_s": round(time.monotonic() - started, 1),
            # route_unit falls back to the bare description when Agent 0 made no usable tool call
            "valid": instructions.strip() != unit["description"].strip(),
        })
    return {"units": units}


def b4_workers(repo, repeats):
    """B4: round-trip latency of a trivial prompt through each worker CLI.

    Side effects: `repeats` read-only calls to agy and to claude.
    @rollback: none needed.
    """
    out = {}
    for agent in orchestrator.AGENTS:
        out[agent] = []
        for _ in range(repeats):
            result = orchestrator.run_cli(agent, "Reply with exactly: BENCH_OK", repo, allow_edits=False, purpose="bench b4 %s" % agent)
            record = maid_common.TELEMETRY.snapshot()["calls"][-1]
            out[agent].append({
                "latency_s": result["duration_s"], "ok": result["ok"] and "BENCH_OK" in result["text"], "cost_usd": record.get("cost_usd"),
            })
        print("  %-6s %s s" % (agent, median([call["latency_s"] for call in out[agent]])), flush=True)
    return out


def b5_end_to_end(repo, task, control, on_worker_start):
    """B5: the fixed task through the whole orchestrator, with the dashboard attached.

    Args:
        on_worker_start: called once, the first time a cloud worker is coding. The
            local model is idle at that moment, so a screenshot taken there does
            not disturb the local numbers.

    State mutations: the worker edits files in the throwaway `repo`.
    @rollback: the repo is deleted by the caller.
    """
    fired = threading.Event()

    def watch():
        while not fired.is_set():
            agents = control.snapshot()["agents"]
            if any(agents[key]["state"] == "coding" for key in ("agy", "claude")):
                fired.set()
                on_worker_start()
                return
            time.sleep(0.5)

    threading.Thread(target=watch, daemon=True).start()
    started_at, started = time.time(), time.monotonic()
    exit_code = orchestrator.main([task, "--repo", str(repo), "--no-clarify"])
    wall = time.monotonic() - started
    fired.set()
    state = control.snapshot()
    calls = [call for call in maid_common.TELEMETRY.snapshot()["calls"] if call["at"] >= started_at]
    phases = {}
    for call in calls:
        phase = phases.setdefault(phase_of(call["purpose"]), {"seconds": 0.0, "calls": 0})
        phase["seconds"] = round(phase["seconds"] + call["duration_s"], 1)
        phase["calls"] += 1
    units = [{key: unit[key] for key in ("id", "file", "agent", "status", "attempt")} for unit in state["units"]]
    return {
        "task": task, "exit_code": exit_code, "run_status": state["run"]["status"], "wall_s": round(wall, 1),
        "units_total": len(units), "units_passed": sum(1 for unit in units if unit["status"] == "passed"),
        "attempts": sum(unit["attempt"] for unit in units), "units": units, "phases": phases,
        "cost_usd": round(sum(call.get("cost_usd") or 0.0 for call in calls), 4),
        "by_agent_s": {agent: round(sum(call["duration_s"] for call in calls if call["agent"] == agent), 1) for agent in ("local", "agy", "claude")},
    }


def b6_overhead(prompt, repeats, dashboard_control, attached_client):
    """B6: the same local prompt with and without the dashboard attached, interleaved.

    Alternating the two conditions cancels slow drift (thermal state, other load).
    @rollback: none needed.
    """
    bare, attached = [], []
    for index in range(repeats):
        orchestrator.set_control(BareControl())
        bare.append(local_call(prompt, "bench b6 bare %d" % index)["tok_s"])
        orchestrator.set_control(dashboard_control)
        attached.append(local_call(prompt, "bench b6 attached %d" % index)["tok_s"])
        print("  round %d: bare %s, attached %s tok/s" % (index + 1, bare[-1], attached[-1]), flush=True)
    bare_median, attached_median = median(bare), median(attached)
    return {
        "bare": bare, "attached": attached, "bare_tok_s": bare_median, "attached_tok_s": attached_median,
        "overhead_pct": overhead_pct(bare_median, attached_median), "attached_client": attached_client,
    }


# --------------------------------------------------------------------------- #
# Fixture repo, dashboard and screenshots
# --------------------------------------------------------------------------- #


def make_fixture_repo(files):
    """Create the throwaway git repo for B2-B5 and return (workdir, repo).

    State mutations: one new directory under the system temp directory.
    @rollback: `shutil.rmtree(workdir)`.
    """
    workdir = Path(tempfile.mkdtemp(prefix="maid-bench-"))
    repo = workdir / "bench-fixture"
    repo.mkdir()
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    maid_common.git(repo, "init", "-q")
    maid_common.git(repo, "add", ".")
    maid_common.git(repo, "-c", "user.name=maid-bench", "-c", "user.email=bench@localhost", "commit", "-q", "-m", "fixture")
    return workdir, repo


class Dashboard:
    """The real dashboard served in-process, with a browser (or a plain SSE reader) attached.

    State mutations: binds 127.0.0.1:<port>; starts a headless browser if one is found.
    @rollback: `close()`.
    """

    def __init__(self, repo, port, want_browser, sampler=None):
        self.app = maid_ui.App(repo, sample=False)
        # Share the benchmark's sampler, so the METRICS tab shows real figures without sampling twice
        self.app.sampler = sampler
        self.app.control = orchestrator.set_control(DashboardControl(self.app.bus))
        self.app.control.set_models(maid_common.get_models())
        maid_ui.Handler.app = self.app
        self.server = ThreadingHTTPServer(("127.0.0.1", port), maid_ui.Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d/" % port
        self.browser = None
        binary = find_browser() if want_browser else ""
        if binary:
            try:
                self.browser = CDP(binary)
                self.browser.goto(self.base + "?inspect=agent0&tab=stream", wait=2.0)
            except (OSError, RuntimeError) as error:
                print("  browser unavailable (%s); continuing without screenshots" % error)
                self.browser = None
        if not self.browser:
            threading.Thread(target=self._read_events, daemon=True).start()
        self.client = "headless browser showing the live stream" if self.browser else "one Server-Sent Events client, no browser"

    def _read_events(self):
        """Consume /api/stream like a browser would, so the server does real fan-out work."""
        try:
            with urllib.request.urlopen(self.base + "api/stream", timeout=3600) as stream:
                for _ in stream:
                    pass
        except OSError:
            pass

    def shot(self, directory, name, script="", wait=0.8):
        """Run `script` in the page, wait, and save a screenshot; return the file name or None.

        @rollback: delete the file.
        """
        if not self.browser:
            return None
        try:
            if script:
                self.browser.js(script)
            time.sleep(wait)
            self.browser.shot(str(directory / name))
            return name
        except (OSError, RuntimeError) as error:
            print("  screenshot %s failed: %s" % (name, error))
            return None

    def close(self):
        """Stop the browser and the server.

        @rollback: none needed.
        """
        if self.browser:
            self.browser.close()
        self.server.shutdown()
        self.server.server_close()
        orchestrator.set_control(RunControl())


AFTER_RUN_SHOTS = [
    ("03-board.png", "closeDrawer(); refresh()", 1.2),
    ("04-stream.png", "sel = {agent:'agent0', unit:'', spec:specName(run().run.spec_path), card:''}; tab = 'stream'; openDrawer()", 0.8),
    ("05-diff-checks.png", "selectCard(run().units[0]); tab = 'diff'; renderDrawer(true)", 1.2),
    ("06-messages.png", "tab = 'messages'; renderDrawer(true)", 1.0),
    ("07-traces.png", "tab = 'traces'; renderDrawer(true)", 1.2),
    ("08-metrics.png", "tab = 'metrics'; renderDrawer(true)", 2.6),
    ("09-spec.png", "tab = 'spec'; renderDrawer(true)", 1.0),
]


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def parse_args(argv):
    """Parse the command line.

    @rollback: none (pure).
    """
    parser = argparse.ArgumentParser(description="MAID benchmark suite.")
    parser.add_argument("--quick", action="store_true", help="run only B1 and B6 (no agy or claude calls)")
    parser.add_argument("--repeats", type=int, default=3, help="repeats per local prompt (default 3)")
    parser.add_argument("--label", default="", help="short name for this setup, added to the folder name")
    parser.add_argument("--out", default="", help="result folder (default: benchmarks/results/<date>_<chip>_<model>)")
    parser.add_argument("--port", type=int, default=8770, help="loopback port for the benchmark's dashboard (default 8770)")
    parser.add_argument("--no-screenshots", action="store_true", help="do not start a browser")
    return parser.parse_args(argv)


def main(argv=None):
    """Run the suite and write the result folder; return 0, or 1 if a benchmark failed.

    State mutations / side effects: see the module docstring.
    @rollback: delete the result folder.
    """
    args = parse_args(sys.argv[1:] if argv is None else argv)
    system = system_info(args.label)
    folder = Path(args.out) if args.out else BENCH_DIR / "results" / "_".join(
        part for part in (datetime.now().strftime("%Y%m%d-%H%M"), slug(system["chip"], 24), slug(system["local_model"].split("/")[-1], 24), slug(args.label) if args.label else "") if part
    )
    shots_dir = folder / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    prompts = json.loads((FIXTURES / "prompts.json").read_text(encoding="utf-8"))
    fixture = json.loads((FIXTURES / "e2e_task.json").read_text(encoding="utf-8"))
    print("MAID benchmark -> %s\n%s, %s" % (folder, system["chip"], system["local_model"]), flush=True)

    maid_common.new_correlation_id()
    orchestrator.set_control(BareControl())
    monitor = Monitor()
    monitor.start()
    results, shots = {}, []
    workdir, repo = make_fixture_repo(fixture["repo_files"])
    dashboard = None

    def step(name, title, function):
        monitor.phase = name
        print("\n[%s] %s" % (name.upper(), title), flush=True)
        started = time.monotonic()
        try:
            results[name] = function()
        except Exception as error:  # noqa: BLE001 - one failed benchmark must not lose the others
            results[name] = {"error": "%s: %s" % (type(error).__name__, error)}
            print("  FAILED: %s" % results[name]["error"], flush=True)
        print("  done in %.0fs" % (time.monotonic() - started), flush=True)

    try:
        step("b1", "local model: tokens/second and time to first token", lambda: b1_local(prompts["prompts"], 1 if args.quick else args.repeats))
        if not args.quick:
            step("b2", "planning: clarify and plan latency", lambda: b2_planning(repo, fixture["task"]))
            plan = (results["b2"] or {}).pop("plan", None)
            if plan:
                step("b3", "routing: tool-call latency per unit", lambda: b3_routing(repo, plan))
            step("b4", "workers: agy and claude round trip", lambda: b4_workers(repo, 2))

        monitor.phase = "dashboard-start"
        dashboard = Dashboard(repo, args.port, not args.no_screenshots, monitor.sampler)
        if not args.quick:
            def worker_started():
                name = dashboard.shot(shots_dir, "02-running.png", "closeDrawer()", 0.6)
                if name:
                    shots.append(name)

            name = dashboard.shot(shots_dir, "01-idle.png", "closeDrawer()", 0.5)
            shots.extend([name] if name else [])
            step("b5", "end to end: fixed two-unit task, dashboard attached", lambda: b5_end_to_end(repo, fixture["task"], dashboard.app.control, worker_started))
            monitor.phase = "screenshots"
            for name, script, wait in AFTER_RUN_SHOTS:
                saved = dashboard.shot(shots_dir, name, script, wait)
                shots.extend([saved] if saved else [])
            if dashboard.browser:
                # Back to the live stream, the view B6 measures with
                dashboard.browser.js("sel = {agent:'agent0', unit:'', spec:'', card:''}; tab = 'stream'; openDrawer()")
        overhead_prompt = next(item["prompt"] for item in prompts["prompts"] if item["id"] == prompts["overhead_prompt"])
        step("b6", "dashboard overhead on the local model", lambda: b6_overhead(overhead_prompt, 2 if args.quick else args.repeats, dashboard.app.control, dashboard.client))
    finally:
        monitor.stop()
        if dashboard:
            dashboard.close()
        shutil.rmtree(workdir, ignore_errors=True)

    numbers = headline(results, monitor.samples)
    (folder / "system.json").write_text(json.dumps(system, indent=2) + "\n", encoding="utf-8")
    (folder / "metrics.json").write_text(json.dumps({"headline": numbers, "results": results, "quick": args.quick}, indent=2, default=str) + "\n", encoding="utf-8")
    monitor.write_csv(folder / "samples.csv")
    shots = sorted(set(shots))
    (folder / "summary.md").write_text(render_summary(system, numbers, results, shots), encoding="utf-8")
    if not shots:
        shutil.rmtree(shots_dir, ignore_errors=True)

    print("\n%-40s %s" % ("METRIC", "VALUE"))
    for label, key, unit, _ in HEADLINE_ROWS:
        print("%-40s %s" % (label, show(numbers.get(key), unit)))
    print("\nResults: %s" % folder)
    return 1 if any(isinstance(value, dict) and "error" in value for value in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())

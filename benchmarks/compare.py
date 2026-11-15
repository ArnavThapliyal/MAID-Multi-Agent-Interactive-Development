"""Compare MAID benchmark results side by side.

Purpose
    Reads the headline block of two or more result folders written by
    benchmarks/bench.py and prints one Markdown table: a column per run, with the
    change against the first run in brackets.

Usage
    python3 benchmarks/compare.py                       every folder in benchmarks/results/
    python3 benchmarks/compare.py results/A results/B   the first one is the baseline
    python3 benchmarks/compare.py --out comparison.md

State mutations
    None, unless --out is given, in which case that one file is written.

@rollback
    Delete the --out file. To remove this script, `git rm benchmarks/compare.py`.
    Standard library only, Python 3.9+.
"""
import argparse
import json
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH_DIR))
from bench import HEADLINE_ROWS, pct_change, show  # noqa: E402


def load(folder):
    """Return {"name", "system", "headline"} for one result folder.

    Raises: OSError or ValueError if the folder has no readable metrics.json.
    @rollback: none (read-only).
    """
    folder = Path(folder)
    metrics = json.loads((folder / "metrics.json").read_text(encoding="utf-8"))
    system_path = folder / "system.json"
    system = json.loads(system_path.read_text(encoding="utf-8")) if system_path.is_file() else {}
    return {"name": folder.name, "system": system, "headline": metrics.get("headline", {}), "quick": metrics.get("quick", False)}


def cell(value, unit, base, better):
    """Format one value, with its change against the baseline when both are numbers.

    The change is marked "better" or "worse" when the metric has a direction.
    @rollback: none (pure).
    """
    text = show(value, unit)
    if isinstance(value, bool) or isinstance(base, bool):
        return text
    change = pct_change(base, value)
    if change is None or change == 0:
        return text
    verdict = ""
    if better:
        verdict = " better" if (change > 0) == (better == "higher") else " worse"
    return "%s (%+.1f%%%s)" % (text, change, verdict)


def compare(runs):
    """Render the comparison table for a list of load() results; the first is the baseline.

    @rollback: none (pure).
    """
    lines = ["# MAID benchmark comparison", "", "Baseline: `%s`" % runs[0]["name"], ""]
    lines += ["| | " + " | ".join("`%s`" % run["name"] for run in runs) + " |", "|---|" + "---|" * len(runs)]
    for label, key in (("Chip", "chip"), ("Memory (GB)", "memory_gb"), ("Local model", "local_model"), ("Quantisation", "quantization"), ("MAID commit", "maid_commit")):
        lines.append("| %s | %s |" % (label, " | ".join(str(run["system"].get(key) or "n/a") for run in runs)))
    lines += ["", "| Metric | " + " | ".join("`%s`" % run["name"] for run in runs) + " |", "|---|" + "---|" * len(runs)]
    for label, key, unit, better in HEADLINE_ROWS:
        base = runs[0]["headline"].get(key)
        cells = [show(base, unit)] + [cell(run["headline"].get(key), unit, base, better) for run in runs[1:]]
        lines.append("| %s | %s |" % (label, " | ".join(cells)))
    if any(run["quick"] for run in runs):
        lines += ["", "Runs made with `--quick` only have the local-model and dashboard-overhead rows."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    """Print (or write) the comparison; return 0, or 2 if there is nothing to compare.

    @rollback: delete the --out file.
    """
    parser = argparse.ArgumentParser(description="Compare MAID benchmark result folders.")
    parser.add_argument("folders", nargs="*", help="result folders; the first is the baseline (default: all, oldest first)")
    parser.add_argument("--out", default="", help="write the table to this file instead of printing it")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    folders = args.folders or sorted(path for path in (BENCH_DIR / "results").glob("*") if (path / "metrics.json").is_file())
    runs = []
    for folder in folders:
        try:
            runs.append(load(folder))
        except (OSError, ValueError) as error:
            print("skipping %s: %s" % (folder, error), file=sys.stderr)
    if not runs:
        print("no benchmark results found", file=sys.stderr)
        return 2
    table = compare(runs)
    if args.out:
        Path(args.out).write_text(table, encoding="utf-8")
    else:
        print(table, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())

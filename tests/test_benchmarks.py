"""Unit tests for the benchmark suite's arithmetic and reporting (benchmarks/).

Purpose
    Offline. Covers the helpers that turn raw measurements into the headline
    numbers, the summary and the comparison table. No model, browser or worker CLI
    is used.

State mutations
    Temporary directories only.

@rollback
    Nothing to undo. To remove this file, `git rm tests/test_benchmarks.py`.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bridge"))
sys.path.insert(0, str(ROOT / "benchmarks"))
import bench  # noqa: E402
import compare  # noqa: E402


def sample(lm_cpu, lm_rss, maid_cpu, free):
    groups = {name: {"cpu": 0.0, "rss_mb": 0.0, "procs": 0} for name in ("lmstudio", "dashboard", "orchestrator", "agy", "claude")}
    groups["lmstudio"].update(cpu=lm_cpu, rss_mb=lm_rss)
    groups["orchestrator"].update(cpu=maid_cpu)
    return {"at": 0, "phase": "b1", "groups": groups, "memory": {"available_mb": free}}


RESULTS = {
    "b1": {"prompts": [
        {"id": "short", "runs": [{"tok_s": 10.0, "ttft_s": 1.0}, {"tok_s": 12.0, "ttft_s": 3.0}], "tok_s": 11.0, "ttft_s": 2.0, "completion_tokens": 40},
        {"id": "long", "runs": [{"tok_s": 11.0, "ttft_s": 2.0}], "tok_s": 11.0, "ttft_s": 2.0, "completion_tokens": 400},
    ]},
    "b2": {"clarify_latency_s": 40.0, "plan_latency_s": 90.5, "plan_valid_first_try": True},
    "b3": {"units": [{"latency_s": 50.0, "valid": True}, {"latency_s": 70.0, "valid": False}]},
    "b4": {
        "agy": [{"latency_s": 20.0, "ok": True}, {"latency_s": 99.0, "ok": False}],
        "claude": [{"latency_s": 30.0, "ok": True, "cost_usd": 0.1}, {"latency_s": 34.0, "ok": True, "cost_usd": 0.2}],
    },
    "b5": {"wall_s": 400.0, "units_passed": 2, "attempts": 3, "cost_usd": 0.42},
    "b6": {"overhead_pct": 1.5},
}
SAMPLES = [sample(300.0, 1200.0, 2.0, 24000), sample(410.0, 1300.0, 4.0, 20000)]


class HelperTest(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(bench.slug("Apple M4 Pro"), "apple-m4-pro")
        self.assertEqual(bench.slug("qwen3.8-27b"), "qwen3-8-27b")
        self.assertEqual(bench.slug("///"), "unknown")
        self.assertLessEqual(len(bench.slug("x" * 100)), 40)

    def test_median_ignores_missing_values(self):
        self.assertEqual(bench.median([3, None, 1, 2]), 2)
        self.assertEqual(bench.median([1.0, 2.0]), 1.5)
        self.assertIsNone(bench.median([None]))
        self.assertIsNone(bench.median([]))

    def test_pct_change_and_overhead(self):
        self.assertEqual(bench.pct_change(10.0, 11.0), 10.0)
        self.assertIsNone(bench.pct_change(0, 5))
        self.assertIsNone(bench.pct_change(None, 5))
        self.assertEqual(bench.overhead_pct(10.0, 9.8), 2.0)
        self.assertEqual(bench.overhead_pct(10.0, 10.3), -3.0)
        self.assertIsNone(bench.overhead_pct(None, 9.0))

    def test_phase_of(self):
        self.assertEqual(
            [bench.phase_of(purpose) for purpose in ("task plan (attempt 1)", "route U2", "execute U1", "review U1", "diff audit U1 (attempt 1)", "clarifying questions (attempt 1)", "bench b1", "")],
            ["plan", "route", "execute", "review", "review", "clarify", "other", "other"],
        )

    def test_show(self):
        self.assertEqual([bench.show(None), bench.show(True), bench.show(False)], ["n/a", "yes", "no"])
        self.assertEqual([bench.show(10.0, "tok/s"), bench.show(2.5, "s"), bench.show(3, "%"), bench.show(0.1234, "$")], ["10 tok/s", "2.5 s", "3%", "$0.123"])


class HeadlineTest(unittest.TestCase):
    def test_headline_from_full_results(self):
        numbers = bench.headline(RESULTS, SAMPLES)
        self.assertEqual(sorted(numbers), sorted(key for _, key, _, _ in bench.HEADLINE_ROWS))
        self.assertEqual((numbers["local_tok_s"], numbers["local_ttft_s"]), (11.0, 2.0))
        self.assertEqual((numbers["route_latency_s"], numbers["route_valid_pct"]), (60.0, 50))
        self.assertEqual(numbers["agy_latency_s"], 20.0)
        self.assertEqual((numbers["claude_latency_s"], numbers["claude_cost_per_call_usd"]), (32.0, 0.15))
        self.assertEqual((numbers["e2e_wall_s"], numbers["e2e_units_passed"], numbers["dashboard_overhead_pct"]), (400.0, 2, 1.5))
        self.assertEqual((numbers["lmstudio_cpu_peak_pct"], numbers["lmstudio_rss_peak_mb"], numbers["available_mem_min_mb"], numbers["maid_cpu_avg_pct"]), (410, 1300, 20000, 3.0))

    def test_headline_tolerates_skipped_and_failed_benchmarks(self):
        numbers = bench.headline({"b1": RESULTS["b1"], "b4": {"error": "agy not found"}}, [])
        self.assertEqual(numbers["local_tok_s"], 11.0)
        for key in ("plan_latency_s", "agy_latency_s", "e2e_wall_s", "dashboard_overhead_pct", "lmstudio_cpu_peak_pct", "available_mem_min_mb"):
            self.assertIsNone(numbers[key], key)

    def test_summary_lists_every_headline_row_and_failures(self):
        system = {"label": "test rig", "date": "2026-01-01T00:00", "maid_commit": "abc1234", "chip": "Test Chip", "memory_gb": 48}
        results = dict(RESULTS, b4={"error": "agy not found"})
        text = bench.render_summary(system, bench.headline(results, SAMPLES), results, ["01-idle.png"])
        for label, _, _, _ in bench.HEADLINE_ROWS:
            self.assertIn("| %s |" % label, text)
        self.assertIn("| Memory | 48 GB |", text)
        self.assertIn("- **b4**: agy not found", text)
        self.assertIn("(screenshots/01-idle.png)", text)


class CompareTest(unittest.TestCase):
    def write(self, root, name, tok_s, ttft, chip):
        folder = Path(root) / name
        folder.mkdir()
        numbers = dict(bench.headline(RESULTS, SAMPLES), local_tok_s=tok_s, local_ttft_s=ttft)
        (folder / "metrics.json").write_text(json.dumps({"headline": numbers}))
        (folder / "system.json").write_text(json.dumps({"chip": chip, "memory_gb": 48, "local_model": "m"}))
        return folder

    def test_table_marks_better_and_worse_against_the_baseline(self):
        with tempfile.TemporaryDirectory() as root:
            runs = [compare.load(self.write(root, "a", 10.0, 2.0, "Chip A")), compare.load(self.write(root, "b", 12.0, 3.0, "Chip B"))]
            table = compare.compare(runs)
        self.assertIn("Baseline: `a`", table)
        self.assertIn("| Chip | Chip A | Chip B |", table)
        self.assertIn("| Local tokens/second (median) | 10 tok/s | 12 tok/s (+20.0% better) |", table)
        self.assertIn("| Local time to first token (median) | 2 s | 3 s (+50.0% worse) |", table)
        self.assertIn("| Plan valid on first try | yes | yes |", table)

    def test_cell_without_direction_or_change(self):
        self.assertEqual(compare.cell(410, "%", 300, ""), "410% (+36.7%)")
        self.assertEqual(compare.cell(5.0, "s", 5.0, "lower"), "5 s")
        self.assertEqual(compare.cell(None, "s", 5.0, "lower"), "n/a")

    def test_main_reports_when_there_is_nothing_to_compare(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(compare.main([str(Path(root) / "missing")]), 2)


if __name__ == "__main__":
    unittest.main()

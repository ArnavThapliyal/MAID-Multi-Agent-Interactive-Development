# MAID benchmarks

A fixed suite that measures how fast a MAID setup is, so that one machine, model or configuration can be compared with another.

## Run it

```bash
python3 benchmarks/bench.py                      # full suite, about 20-25 minutes
python3 benchmarks/bench.py --quick              # local model only (B1 + B6), about 4 minutes
python3 benchmarks/bench.py --label m4pro-48gb   # add a name for this setup to the folder
python3 benchmarks/compare.py                    # table comparing every result folder
```

Before a run: LM Studio must be serving the model, and for the full suite `agy` and `claude` must be signed in. Close other heavy applications; the local numbers are sensitive to whatever else is using the machine. The full suite makes about ten calls to agy and to Claude Code (roughly $0.50 of Claude usage).

## What is measured

| Benchmark | What it does | Numbers |
|---|---|---|
| **B1 Local model** | Three fixed prompts (`fixtures/prompts.json`), three repeats each, streamed. | Tokens/second, time to first token, output tokens. Medians. |
| **B2 Planning** | Agent 0 asks its clarifying questions and writes the plan for the fixed task. | Clarify latency, plan latency, whether the plan was valid JSON on the first try. |
| **B3 Routing** | Agent 0 dispatches each planned unit with a tool call. No worker is started. | Latency per unit, share of valid tool calls. |
| **B4 Workers** | A trivial prompt through `agy -p` and `claude -p`, twice each. | Round-trip latency, Claude cost per call. |
| **B5 End to end** | The fixed two-unit task (`fixtures/e2e_task.json`) through the whole orchestrator in a throwaway repo, with the dashboard attached. | Wall time, time per phase, units passed, attempts, Claude cost. |
| **B6 Dashboard overhead** | B1's code prompt, alternating between no dashboard and the dashboard attached with a browser showing the live stream. | Tokens/second in each condition and the difference in percent. |

Tokens/second is generation speed: completion tokens (reasoning included) divided by the time after the first token arrived. Time to first token includes prompt processing.

Throughout the run, CPU and memory of LM Studio, the MAID process and the worker CLIs are sampled every two seconds with one `ps` and one `vm_stat` call.

## What a result folder contains

```
results/<date>_<chip>_<model>[_<label>]/
  system.json     chip, cores, memory, OS, model, quantisation, context length, tool versions
  metrics.json    all measurements; the "headline" block holds the comparable numbers
  samples.csv     resource samples: time, phase, LM Studio CPU and memory, MAID CPU, available memory
  summary.md      the same results as readable tables
  screenshots/    the dashboard during and after the end-to-end run
```

Result folders contain no hostname, user name or home-directory path. The end-to-end task runs in a temporary repository, so screenshots show only that.

## Comparing setups

Copy a result folder from another machine into `benchmarks/results/` and run:

```bash
python3 benchmarks/compare.py                                   # all folders, oldest is the baseline
python3 benchmarks/compare.py results/<baseline> results/<other>
python3 benchmarks/compare.py --out comparison.md
```

Each column shows the value and its change against the baseline, marked better or worse where the metric has a direction.

## Reading the numbers

- **Compare the local rows first.** B1 and B6 depend only on your hardware, the model and its settings. They are the rows that transfer between setups.
- **B2 and B3 mix speed and model behaviour.** A model that reasons longer is slower here even on the same hardware.
- **B4 and B5 include the network and the vendors' services.** They vary from run to run and by time of day; treat differences under about 20% as noise.
- **B6 near zero means the dashboard is not slowing the model.** With three repeats per condition, anything within about ±5% is noise.
- **GPU load and power draw are not measured.** macOS only exposes them through `sudo powermetrics`.
- **The MAID CPU figure includes the dashboard**, because the benchmark serves it from its own process.

## Files

| File | Purpose |
|---|---|
| `bench.py` | Runs the suite and writes the result folder. |
| `compare.py` | Side-by-side table of result folders. |
| `cdp.py` | Minimal Chrome DevTools client used for screenshots. Needs a Chromium-family browser (`MAID_BROWSER_BIN` overrides auto-detection); without one the suite runs and skips screenshots. |
| `fixtures/` | The fixed prompts and the fixed end-to-end task. Changing them makes results incomparable with earlier ones. |

# MAID benchmark: m4pro-48gb

Run on 2026-11-15T18:55+05:30 with MAID `9d77357`.

## System

| Field | Value |
|---|---|
| Machine | Mac16,8 |
| Chip | Apple M4 Pro |
| CPU cores | 14 (10 performance + 4 efficiency) |
| Memory | 48 GB |
| OS | Darwin 27.2.1 |
| Python | 3.11.15 |
| Local model | qwen/qwen3.8-27b |
| Quantisation | 6bit |
| Context length | 132000 |
| Model size | 22.81 GB |
| Reasoning effort | low |
| agy | 1.5.9 |
| agy model | gemini-3.8-flash-medium |
| claude | 2.1.311 (Claude Code) |
| claude model | default |

## Headline

| Metric | Value |
|---|---|
| Local tokens/second (median) | 10.57 tok/s |
| Local time to first token (median) | 1.41 s |
| Clarify latency | 29.7 s |
| Plan latency | 55.7 s |
| Plan valid on first try | yes |
| Routing latency per unit (median) | 34.75 s |
| Routing tool calls valid | 100% |
| agy round trip (median) | 11.99 s |
| claude round trip (median) | 5.31 s |
| claude cost per call | $0.131 |
| End to end: wall time | 319.3 s |
| End to end: units passed | 2 |
| End to end: worker attempts | 3 |
| End to end: Claude cost | $0.453 |
| Dashboard overhead on tokens/second | -1% |
| LM Studio CPU, peak | 103% |
| LM Studio resident memory, peak | 10714 MB |
| Available memory, minimum | 5543 MB |
| MAID process CPU, average | 0.5% |

## B1 Local model

| Prompt | Runs | tokens/s (median) | First token (median) | Output tokens (median) |
|---|---|---|---|---|
| short | 3 | 11.4 | 1.05 s | 76 |
| code | 3 | 10.52 | 1.43 s | 266 |
| long | 3 | 10.34 | 1.65 s | 1190 |

## B5 End to end

Task: Create the file doc/NOTES.md containing a five-line plain-language explanation of what retry with exponential backoff is. Then add the line doc/NOTES.md to .gitignore so the notes are not tracked. These are two separate units: one for doc/NOTES.md and one for .gitignore.

| Phase | Seconds | Calls |
|---|---|---|
| plan | 57.5 | 1 |
| route | 99.6 | 3 |
| execute | 126.9 | 3 |
| review | 34.9 | 3 |

| Unit | File | Agent | Status | Attempts |
|---|---|---|---|---|
| U1 | `doc/NOTES.md` | agy | passed | 1 |
| U2 | `.gitignore` | agy | passed | 2 |

## B6 Dashboard overhead

The same prompt, alternating between no dashboard and the dashboard attached (headless browser showing the live stream).

| Condition | tokens/s per run | Median |
|---|---|---|
| No dashboard | 10.01, 9.21, 8.81 | 9.21 |
| Dashboard attached | 9.8, 9.3, 9.18 | 9.3 |

Overhead: -1%. Differences of a few percent either way are run-to-run noise.

## Screenshots

- [01-idle.png](screenshots/01-idle.png)
- [02-running.png](screenshots/02-running.png)
- [03-board.png](screenshots/03-board.png)
- [04-stream.png](screenshots/04-stream.png)
- [05-diff-checks.png](screenshots/05-diff-checks.png)
- [06-messages.png](screenshots/06-messages.png)
- [07-traces.png](screenshots/07-traces.png)
- [08-metrics.png](screenshots/08-metrics.png)
- [09-spec.png](screenshots/09-spec.png)

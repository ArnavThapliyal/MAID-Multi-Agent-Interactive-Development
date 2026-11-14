# Task specs

`bridge/orchestrator.py` writes one spec per run here, named `<timestamp>_<slug>.md` (for example `20261115-142530_add-retry-helper.md`).

Each spec records the original task, the clarifying questions and answers, the git commit the run started from, and the atomic units: one file or one function each, with required signatures, the agent Agent 0 suggested, acceptance criteria and the rollback command.

Workers receive the spec in full with every call, so treat a spec as the contract for that run.

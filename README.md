# pbt-agents

AI Disclosure: Most of the code and documentation in this repository was AI-generated with some human review.

## Docs

- [docs/MUTATE.md](docs/MUTATE.md): how the dataset of subtly incorrect HumanEval+ solutions is generated, and which mutation types it covers.
- [docs/SANDBOX.md](docs/SANDBOX.md): Docker-based service for running generated code safely.
- `.claude/workflows/witnesses.js`: subagent workflow that generates witness implementations, witness property sets and one shared set of properties over a list of tasks.
- `src/pbt_agents/harness.py`: runs the shared properties as Hypothesis tests on the canonical solution and each mutant (inside the sandbox); on a failure it runs the witnesses on the failing input to help debug.
- `src/pbt_agents/vote.py`: for each failing input, property sets vote on whether the input is valid, witnesses vote on the intended output y* (accepted if the top output leads the runner-up by `--tau`), and otherwise the user is asked; the votes are on the program inputs the failing property run actually used (its calls), and each failing run is labelled `bug` (the program returned something other than y*) or `property_wrong` (every call returned y*, yet the property raised). The harness runs it automatically (`--tau`, `--labels`, `--no-vote`).
- `src/pbt_agents/repair.py` + `.claude/workflows/repair.js`: a subagent repairs every program labelled `bug` (from the spec and the confirmed (input, y*) cases) and every property labelled `property_wrong` (it must accept the outputs the witnesses agreed on, still reject the real bugs, and not fail the witnesses), checking each attempt in the sandbox. `repair evaluate` scores the repairs afterwards, including agreement with the canonical solution (never shown to the agent). Jobs come from `harness --jobs-out`.
- `src/pbt_agents/private.py`: keeps canonical solutions and mutation details in a private file outside the project, so no agent is handed them (see docs/MUTATE.md).

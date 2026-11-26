# pbt-agents

AI Disclosure: Most of the code and documentation in this repository was AI-generated with some human review.

## Docs

- [docs/MUTATE.md](docs/MUTATE.md): how the dataset of subtly incorrect HumanEval+ solutions is generated, and which mutation types it covers.
- [docs/SANDBOX.md](docs/SANDBOX.md): Docker-based service for running generated code safely.
- `.claude/workflows/witnesses.js`: subagent workflow that generates witness implementations, witness property sets and one shared set of properties over a list of tasks.
- `src/pbt_agents/harness.py`: runs the shared properties as Hypothesis tests on the canonical solution and each mutant (inside the sandbox); on a failure it runs the witnesses on the failing input to help debug.

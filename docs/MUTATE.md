# Mutant dataset (`pbt_agents.mutate`)

Part of the dataset for this project: a set of subtly incorrect solutions built from [HumanEval+](https://huggingface.co/datasets/evalplus/humanevalplus).
Each mutant is the canonical solution with one small, single-site edit that makes it fail the HumanEval+ tests.

## Keeping the canonical solutions away from agents

After generating, `mutate` splits the dataset (`pbt_agents.private`). The public file `data/humanevalplus_mutants.jsonl` keeps only `id`, `task_id`, `prompt`, `entry_point` and `mutated_solution`. `canonical_solution` and the fields that reveal the fix (`operator`, `description`, `line`, snippets, `failure_*`) go to `~/.cache/pbt-agents/mutants_full.jsonl` (override with `PBT_PRIVATE_DIR`). Only the harness, the vote and `repair evaluate` read it; no agent is given it, `.claude/settings.json` denies agents read access to it and to the dataset caches (a guardrail, not a sandbox). Split an existing file with `uv run python -m pbt_agents.private split`.

## Generating the mutants

```
uv run python -m pbt_agents.mutate                     # writes data/humanevalplus_mutants.jsonl
uv run python -m pbt_agents.mutate --level token       # token-level edits only
uv run python -m pbt_agents.mutate --level statement   # statement-level edits only
```

Options: `--max-per-task N` (default 3), `--seed`, `--out`, `--workers`, and
`--include-exceptions` (also keep mutants that crash or time out; by default only wrong-answer mutants are kept).

Pipeline:
1. Propose single-site edits to the canonical solution. Edits are spliced into the source text, so the diff is only the changed code and formatting is untouched.
2. Drop edits that don't parse.
3. Run each mutant against the full HumanEval+ `check()` in a subprocess (15s timeout). Keep only mutants that fail, which guarantees they are incorrect and discards equivalent mutants.
4. Keep up to `--max-per-task` mutants per problem, rotating across mutation types for variety.

Each output row has the prompt, original and mutated solutions, the mutation type (`operator`), a description such as `'<' -> '<='`, the line number, and the failure kind.

## Mutation types covered

### Token-level (`--level token`)

| Operator | What it does | Example |
|---|---|---|
| `compare_op` | Swap a comparison operator | `<` to `<=`, `>` to `<`, `==` to `!=` |
| `arith_op` | Swap an arithmetic operator (including `+=` style) | `+` to `-`, `//` to `/`, `%` to `//` |
| `bool_op` | Swap `and`/`or` | `and` to `or` |
| `remove_not` | Remove a `not` | `not x` to `x` |
| `bool_const` | Flip a boolean literal (also `reverse=`) | `True` to `False` |
| `int_const` | Shift an integer literal by 1 | `2` to `3` or `1` |
| `float_const` | Shift a float literal by 1.0 | `0.5` to `1.5` |
| `str_const` | Extend a short string literal by one character | `"1"` to `"11"` |
| `name_swap` | Swap a function/method for its opposite | `min`/`max`, `any`/`all`, `lower`/`upper`, `startswith`/`endswith`, `lstrip`/`rstrip` |

### Statement-level (`--level statement`)

| Operator | What it does | Example |
|---|---|---|
| `stmt_delete` | Replace an assignment, augmented assignment, expression statement, `break`/`continue`, small `if` guard, or early `return` with `pass` | `total += x` to `pass` |
| `return_value` | Replace a returned value with `None`, or negate a boolean return | `return x` to `return None`, `return a < b` to `return not (a < b)` |
| `loop_bound` | Shift a `range` bound, start the range at 1, or trim/reverse the iterable | `range(n)` to `range(1, n)`, `range(n)` to `range(n+1)`, `for x in xs` to `xs[1:]`, `xs[:-1]`, `reversed(xs)` |
| `index_shift` | Shift a non-constant index or slice bound by 1, or un-reverse a `[::-1]` slice | `xs[i]` to `xs[i+1]`, `xs[:n]` to `xs[:n-1]`, `s[::-1]` to `s[::1]` |
| `wrong_variable` | Use a different variable from the same function's scope (capped at 10 candidates per task) | `a` to `b` |

## Known limitations

- Mutants that are wrong but still pass the HumanEval+ tests are discarded as equivalent, so the set only contains bugs those tests can detect.
- `return_value` mutants replacing a value with `None` are often obvious breakages rather than subtle bugs; filter on `operator` if you want subtle ones only.
- HumanEval/32's canonical solution fails its own tests, so it has no mutants.
- Mutants run in a subprocess with a timeout but no memory limit.

## Not yet covered

Ideas from standard mutation testing that aren't implemented: forcing or inverting `if`/`while` conditions, swapping `break`/`continue`, removing an `elif`/`else` branch, swapping adjacent statements, swapping call arguments, and mutable-state bugs (`sorted(x)` to `x.sort()`).

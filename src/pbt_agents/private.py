"""Keep the canonical solutions (and anything that reveals them) out of reach of the agents.

The mutants file that agents' tooling reads (`data/humanevalplus_mutants.jsonl`, the "public" file)
holds only `id`, `task_id`, `prompt`, `entry_point` and `mutated_solution`. The canonical solution and
the fields that give away the fix (`canonical_solution`, `operator`, `description`, `line`,
`original_snippet`, `mutated_snippet`, `failure_*`) live in a private file outside the project:

    $PBT_PRIVATE_DIR/mutants_full.jsonl      (default ~/.cache/pbt-agents)

Only trusted, non-agent code reads it: the harness (to sanity-check the canonical solution), the vote
(to label a canonical failure) and `repair evaluate` (to score repairs against the held-out original).

    uv run python -m pbt_agents.private split [data/humanevalplus_mutants.jsonl]

`mutate` calls split itself. Splitting an already-split file does nothing.
`.claude/settings.json` also denies the agents read access to the private directory and to the places
the dataset is cached; that is a guardrail rather than a sandbox, since a shell can still go looking.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PUBLIC_FIELDS = ("id", "task_id", "prompt", "entry_point", "mutated_solution")


def private_dir() -> Path:
    return Path(os.environ.get("PBT_PRIVATE_DIR", "~/.cache/pbt-agents")).expanduser()


def full_path() -> Path:
    return private_dir() / "mutants_full.jsonl"


def split(public: Path) -> int:
    """Move the sensitive fields of `public` to the private file; rewrite `public` without them."""
    rows = [json.loads(l) for l in Path(public).read_text().splitlines() if l.strip()]
    if not rows or all("canonical_solution" not in r for r in rows):
        return 0
    fp = full_path()
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    fp.chmod(0o600)
    Path(public).write_text("".join(json.dumps({k: r[k] for k in PUBLIC_FIELDS}) + "\n" for r in rows))
    return len(rows)


def load_full() -> list[dict]:
    fp = full_path()
    return [json.loads(l) for l in fp.read_text().splitlines() if l.strip()] if fp.exists() else []


def load_canonical() -> dict[str, str]:
    """-> {task_id: full canonical source (prompt + solution)}; empty if the private file is missing."""
    return {r["task_id"]: r["prompt"] + r["canonical_solution"] for r in load_full()}


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] != "split":
        sys.exit(__doc__)
    path = Path(args[1] if len(args) > 1 else "data/humanevalplus_mutants.jsonl")
    n = split(path)
    print(f"split {n} mutants: public {path}, private {full_path()}" if n else f"{path} has nothing to split")


if __name__ == "__main__":
    main()

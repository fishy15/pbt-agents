"""Build a test set of subtly-broken HumanEval+ solutions.

For every problem we apply single-site, source-level mutations (operator swaps,
off-by-one constants, ...) to the canonical solution, then run each mutant
against the full HumanEval+ tests. Only mutants that FAIL the tests are kept,
which guarantees they are genuinely incorrect (and drops equivalent mutants).

Usage:  uv run python -m pbt_agents.mutate [--out PATH] [--max-per-task N]
"""

from __future__ import annotations

import argparse
import ast
import json
import random
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

DATASET = "evalplus/humanevalplus"
TIMEOUT_S = 15

CMP_SWAPS = {
    "<": ["<=", ">"],
    "<=": ["<", ">="],
    ">": [">=", "<"],
    ">=": [">", "<="],
    "==": ["!="],
    "!=": ["=="],
}
BIN_SWAPS = {
    "+": ["-"],
    "-": ["+"],
    "*": ["+"],
    "//": ["/", "%"],
    "/": ["//"],
    "%": ["//"],
}
NAME_SWAPS = {
    "min": "max",
    "max": "min",
    "any": "all",
    "all": "any",
    "lower": "upper",
    "upper": "lower",
    "startswith": "endswith",
    "endswith": "startswith",
    "ljust": "rjust",
    "rjust": "ljust",
    "lstrip": "rstrip",
    "rstrip": "lstrip",
    "append": "insert",
}


@dataclass
class Mutant:
    task_id: str
    operator: str
    description: str
    line: int  # 1-based line within the canonical solution
    original_snippet: str
    mutated_snippet: str
    canonical_solution: str
    mutated_solution: str
    failure_kind: str = ""  # wrong_answer | exception | timeout
    failure_detail: str = ""


class _Source:
    """Offset helper: AST (line, utf8-col) positions -> str indices."""

    def __init__(self, text: str):
        self.text = text
        self.starts = [0]
        for line in text.splitlines(keepends=True):
            self.starts.append(self.starts[-1] + len(line))
        self.lines = text.splitlines(keepends=True)

    def idx(self, lineno: int, col: int) -> int:
        line = self.lines[lineno - 1]
        return self.starts[lineno - 1] + len(line.encode()[:col].decode())

    def span(self, node: ast.AST) -> tuple[int, int]:
        return (
            self.idx(node.lineno, node.col_offset),
            self.idx(node.end_lineno, node.end_col_offset),
        )


def _find_op(src: _Source, a: int, b: int, op: str) -> int | None:
    """Locate operator `op` in text[a:b] (between two operands). None if ambiguous."""
    seg = src.text[a:b]
    if seg.strip("() \t\n\\") != op:
        return None
    return a + seg.index(op)


def propose(solution: str, prompt: str) -> list[tuple[str, str, int, int, str]]:
    """Return (operator, description, start, end, replacement) edits on `solution`.

    The solution is a function *body*, so we parse it together with the prompt
    and only keep nodes lying after the prompt.
    """
    full = prompt + solution
    tree = ast.parse(full)
    src = _Source(full)
    offset = len(prompt)
    edits: list[tuple[str, str, int, int, str]] = []

    def add(kind: str, a: int, b: int, new: str):
        if a >= offset and new != full[a:b]:
            edits.append((kind, f"{full[a:b]!r} -> {new!r}", a - offset, b - offset, new))

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            lefts = [node.left] + node.comparators[:-1]
            for op, l, r in zip(node.ops, lefts, node.comparators):
                sym = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=",
                       ast.Eq: "==", ast.NotEq: "!="}.get(type(op))
                if sym is None:
                    continue
                a, b = src.span(l)[1], src.span(r)[0]
                p = _find_op(src, a, b, sym)
                if p is not None:
                    for new in CMP_SWAPS[sym]:
                        add("compare_op", p, p + len(sym), new)
        elif isinstance(node, (ast.BinOp, ast.AugAssign)):
            sym = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.FloorDiv: "//",
                   ast.Div: "/", ast.Mod: "%"}.get(type(node.op))
            if sym is None:
                continue
            if isinstance(node, ast.BinOp):
                a, b = src.span(node.left)[1], src.span(node.right)[0]
                p = _find_op(src, a, b, sym)
                end = p + len(sym) if p is not None else None
            else:  # x += y
                a, b = src.span(node.target)[1], src.span(node.value)[0]
                seg = full[a:b]
                m = re.search(re.escape(sym) + "=", seg)
                p = a + m.start() if m else None
                end = p + len(sym) if p is not None else None
            if p is not None:
                for new in BIN_SWAPS[sym]:
                    add("arith_op", p, end, new)
        elif isinstance(node, ast.BoolOp):
            sym = "and" if isinstance(node.op, ast.And) else "or"
            new = "or" if sym == "and" else "and"
            for l, r in zip(node.values, node.values[1:]):
                a, b = src.span(l)[1], src.span(r)[0]
                seg = full[a:b]
                m = re.search(rf"\b{sym}\b", seg)
                if m:
                    add("bool_op", a + m.start(), a + m.end(), new)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            a, b = src.span(node)
            ostart, oend = src.span(node.operand)
            add("remove_not", a, ostart, "")
        elif isinstance(node, ast.Constant):
            a, b = src.span(node)
            v = node.value
            if isinstance(v, bool):
                add("bool_const", a, b, str(not v))
            elif isinstance(v, int):
                for new in {v + 1, v - 1}:
                    add("int_const", a, b, str(new))
            elif isinstance(v, float):
                add("float_const", a, b, repr(v + 1.0))
            elif isinstance(v, str) and 0 < len(v) <= 3 and v.strip():
                # swap a short literal (e.g. a delimiter) for another one
                add("str_const", a, b, repr(v + v[0]))
        elif isinstance(node, (ast.Name, ast.Attribute)):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name in NAME_SWAPS:
                a, b = src.span(node)
                if isinstance(node, ast.Attribute):
                    a = b - len(name)
                add("name_swap", a, b, NAME_SWAPS[name])
        elif isinstance(node, ast.keyword) and node.arg == "reverse":
            a, b = src.span(node.value)
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, bool):
                add("bool_const", a, b, str(not node.value.value))
    # dedupe
    seen, out = set(), []
    for e in edits:
        k = (e[2], e[3], e[4])
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


def _run(program: str) -> tuple[str, str]:
    """Execute a program; return (status, detail). status in ok/wrong_answer/exception/timeout."""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "prog.py"
        f.write_text(program)
        try:
            r = subprocess.run(
                [sys.executable, "-I", str(f)],
                cwd=d, capture_output=True, text=True, timeout=TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return "timeout", ""
    if r.returncode == 0:
        return "ok", ""
    tail = r.stderr.strip().splitlines()
    last = tail[-1] if tail else ""
    kind = "wrong_answer" if last.startswith("AssertionError") else "exception"
    return kind, last[:200]


def _program(row: dict, solution: str) -> str:
    return f"{row['prompt']}{solution}\n\n{row['test']}\n\ncheck({row['entry_point']})\n"


def build(out: Path, max_per_task: int, seed: int, workers: int, include_exceptions: bool):
    from datasets import load_dataset

    rows = list(load_dataset(DATASET, split="test"))
    rng = random.Random(seed)

    # 1. sanity: canonical solutions must pass.
    with ThreadPoolExecutor(workers) as ex:
        base = list(ex.map(lambda r: _run(_program(r, r["canonical_solution"])), rows))
    bad = {r["task_id"] for r, (s, _) in zip(rows, base) if s != "ok"}
    if bad:
        print(f"warning: canonical solution fails for {sorted(bad)}; skipping those")

    # 2. generate candidate mutants.
    cands: list[tuple[dict, tuple]] = []
    for r in rows:
        if r["task_id"] in bad:
            continue
        sol = r["canonical_solution"]
        for kind, desc, a, b, new in propose(sol, r["prompt"]):
            msol = sol[:a] + new + sol[b:]
            try:
                ast.parse(r["prompt"] + msol)
            except (SyntaxError, ValueError):
                continue
            cands.append((r, (kind, desc, a, b, new, msol)))
    print(f"{len(cands)} candidate mutants")

    # 3. keep only those that fail the tests.
    with ThreadPoolExecutor(workers) as ex:
        res = list(ex.map(lambda c: _run(_program(c[0], c[1][5])), cands))

    by_task: dict[str, list[Mutant]] = defaultdict(list)
    for (r, (kind, desc, a, b, new, msol)), (status, detail) in zip(cands, res):
        if status == "ok":
            continue  # equivalent (or undetected) mutant
        if status != "wrong_answer" and not include_exceptions:
            continue
        sol = r["canonical_solution"]
        by_task[r["task_id"]].append(Mutant(
            task_id=r["task_id"], operator=kind, description=desc,
            line=sol.count("\n", 0, a) + 1,
            original_snippet=sol[a:b], mutated_snippet=new,
            canonical_solution=sol, mutated_solution=msol,
            failure_kind=status, failure_detail=detail,
        ))

    # 4. pick up to N per task, round-robin over operator kinds for diversity.
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out.open("w") as f:
        for r in rows:
            ms = by_task.get(r["task_id"], [])
            groups: dict[str, list[Mutant]] = defaultdict(list)
            for m in ms:
                groups[m.operator].append(m)
            for g in groups.values():
                rng.shuffle(g)
            kinds = sorted(groups)
            rng.shuffle(kinds)
            picked: list[Mutant] = []
            while len(picked) < max_per_task and any(groups.values()):
                for k in kinds:
                    if groups[k] and len(picked) < max_per_task:
                        picked.append(groups[k].pop())
            for i, m in enumerate(picked):
                row = {"id": f"{m.task_id}/mut{i}", "prompt": r["prompt"],
                       "entry_point": r["entry_point"], **asdict(m)}
                f.write(json.dumps(row) + "\n")
                n += 1
    covered = sum(1 for t in by_task if by_task[t])
    print(f"wrote {n} mutants covering {covered}/{len(rows)} tasks -> {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=Path("data/humanevalplus_mutants.jsonl"))
    ap.add_argument("--max-per-task", type=int, default=3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--include-exceptions", action="store_true",
                    help="also keep mutants that crash/time out (default: wrong-answer only)")
    a = ap.parse_args()
    build(a.out, a.max_per_task, a.seed, a.workers, a.include_exceptions)


if __name__ == "__main__":
    main()

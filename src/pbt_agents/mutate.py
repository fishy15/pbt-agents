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


STATEMENT_KINDS = {"stmt_delete", "return_value", "loop_bound", "index_shift", "wrong_variable"}
MAX_WRONG_VAR = 10  # cap per task; there are many candidates and they are cheap variations


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


def _bound_names(fn: ast.AST) -> set[str]:
    a = fn.args
    names = {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            names.add(n.id)
    return names


def propose(solution: str, prompt: str, rng: random.Random | None = None,
            level: str = "all") -> list[tuple[str, str, int, int, str]]:
    """Return (operator, description, start, end, replacement) edits on `solution`.

    The solution is a function *body*, so we parse it together with the prompt
    and only keep nodes lying after the prompt.
    """
    full = prompt + solution
    tree = ast.parse(full)
    src = _Source(full)
    offset = len(prompt)
    rng = rng or random.Random(0)
    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    edits: list[tuple[str, str, int, int, str]] = []

    def add(kind: str, a: int, b: int, new: str):
        if a >= offset and new != full[a:b]:
            old = full[a:b]
            old = old if len(old) <= 50 else old[:47] + "..."
            edits.append((kind, f"{old!r} -> {new!r}", a - offset, b - offset, new))

    def text(n: ast.AST) -> str:
        a, b = src.span(n)
        return full[a:b]

    def wrapped(n: ast.AST) -> str:
        t = text(n)
        simple = (ast.Name, ast.Constant, ast.Call, ast.Attribute, ast.Subscript)
        return t if isinstance(n, simple) else f"({t})"

    def enclosing_fn(n: ast.AST):
        while n in parents:
            n = parents[n]
            if isinstance(n, ast.FunctionDef):
                return n
        return None

    wrong_vars: list[tuple[str, str, int, int, str]] = []

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

    # ---- statement-level mutations ----
    for node in ast.walk(tree):
        # delete a statement / guard / early return
        if isinstance(node, ast.stmt) and hasattr(node, "lineno"):
            par = parents.get(node)
            deletable = (
                isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Continue, ast.Break))
                or (isinstance(node, ast.Expr) and not isinstance(node.value, ast.Constant))
                or (isinstance(node, ast.If) and not node.orelse
                    and node.end_lineno - node.lineno <= 2
                    and not (isinstance(par, ast.If) and par.orelse == [node]))
                or (isinstance(node, ast.Return) and not isinstance(par, ast.FunctionDef))
            )
            if deletable:
                a, b = src.span(node)
                add("stmt_delete", a, b, "pass")

        # replace the returned value
        if isinstance(node, ast.Return) and node.value is not None:
            a, b = src.span(node.value)
            if not (isinstance(node.value, ast.Constant) and node.value.value is None):
                add("return_value", a, b, "None")
            if isinstance(node.value, (ast.Compare, ast.BoolOp)):
                add("return_value", a, b, f"not ({full[a:b]})")

        # loop bounds
        if isinstance(node, (ast.For, ast.comprehension)):
            it = node.iter
            a, b = src.span(it)
            if (isinstance(it, ast.Call) and isinstance(it.func, ast.Name)
                    and it.func.id == "range" and 1 <= len(it.args) <= 2):
                if len(it.args) == 1:
                    add("loop_bound", src.span(it.args[0])[0], src.span(it.args[0])[1],
                        f"1, {text(it.args[0])}")
                for arg in it.args:
                    if isinstance(arg, ast.Constant):
                        continue  # int_const already covers these
                    s0, s1 = src.span(arg)
                    add("loop_bound", s0, s1, wrapped(arg) + "+1")
                    add("loop_bound", s0, s1, wrapped(arg) + "-1")
            elif isinstance(it, (ast.Name, ast.Attribute, ast.Subscript)):
                t = full[a:b]
                add("loop_bound", a, b, f"{t}[1:]")
                add("loop_bound", a, b, f"{t}[:-1]")
                add("loop_bound", a, b, f"reversed({t})")

        # index / slice shifts
        if isinstance(node, ast.Subscript):
            sl = node.slice
            if isinstance(sl, ast.Slice):
                for part in (sl.lower, sl.upper):
                    if part is not None and not isinstance(part, (ast.Constant, ast.UnaryOp)):
                        s0, s1 = src.span(part)
                        add("index_shift", s0, s1, wrapped(part) + "+1")
                        add("index_shift", s0, s1, wrapped(part) + "-1")
                if sl.step is not None and text(sl.step).replace(" ", "") == "-1":
                    s0, s1 = src.span(sl.step)
                    add("index_shift", s0, s1, "1")
            elif isinstance(sl, (ast.Name, ast.BinOp)):
                s0, s1 = src.span(sl)
                add("index_shift", s0, s1, wrapped(sl) + "+1")
                add("index_shift", s0, s1, wrapped(sl) + "-1")

        # use the wrong variable
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            par = parents.get(node)
            fn = enclosing_fn(node)
            if fn is None or (isinstance(par, ast.Call) and par.func is node):
                continue
            scope = set()
            f = fn
            while f is not None:
                scope |= _bound_names(f)
                f = enclosing_fn(f)
            if node.id in scope:
                a, b = src.span(node)
                for other in sorted(scope - {node.id}):
                    if a >= offset:
                        wrong_vars.append(("wrong_variable", f"{node.id!r} -> {other!r}",
                                           a - offset, b - offset, other))
    rng.shuffle(wrong_vars)
    edits.extend(wrong_vars[:MAX_WRONG_VAR])
    if level == "token":
        edits = [e for e in edits if e[0] not in STATEMENT_KINDS]
    elif level == "statement":
        edits = [e for e in edits if e[0] in STATEMENT_KINDS]
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


def build(out: Path, max_per_task: int, seed: int, workers: int, include_exceptions: bool,
          level: str = "all"):
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
        for kind, desc, a, b, new in propose(sol, r["prompt"], random.Random(f"{seed}/{r['task_id']}"), level):
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
    ap.add_argument("--level", choices=["token", "statement", "all"], default="all",
                    help="which mutation families to use (default: all)")
    ap.add_argument("--include-exceptions", action="store_true",
                    help="also keep mutants that crash/time out (default: wrong-answer only)")
    a = ap.parse_args()
    build(a.out, a.max_per_task, a.seed, a.workers, a.include_exceptions, a.level)
    from pbt_agents import private
    if private.split(a.out):
        print(f"canonical solutions and mutation details moved to {private.full_path()} (agents only get the public file)")


if __name__ == "__main__":
    main()

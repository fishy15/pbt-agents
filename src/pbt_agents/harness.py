"""Run property-based tests on HumanEval+ solutions and mutants, using witnesses to debug failures.

The program under test (the canonical solution, or a mutant) is tested only against the shared
properties written from the spec. For every (property set, property) pair one Hypothesis test runs:

    @given(strategy())
    def test(args):
        assume(precondition(*args))
        prop_<name>(candidate, *args)      # raises on violation

`candidate` calls deep-copy their arguments. Witness implementations are not oracles and play no
part in pass/fail. After a failure they are run on the (shrunk) failing input, so the report shows
what the program returned next to what each witness returns. If the witnesses agree with the
program, the property is the likelier culprit; if they disagree, the program is.

Outcome per (candidate, property set, property):
    failed    Hypothesis found a precondition-valid input on which the property raised
              (`failure` says how: assertion, exception in the program or property, or timeout)
    passed    no failure in `examples` tries
    no_valid  the precondition rejected every generated input
    error     the propset or the property could not be loaded

The canonical solution should pass everything: a failure there points at a wrong property, a
wrong precondition, or a wrong generator.

    uv run python -m pbt_agents.harness WITNESSES.json [--mutants data/humanevalplus_mutants.jsonl]
        [--out results.jsonl] [--examples 200] [--timeout 120]
        [--tau 0.5] [--labels data/labels.json] [--votes-out votes.jsonl] [--no-ask] [--no-vote]

Every distinct failing input then goes through the voting procedure in `pbt_agents.vote` (property
set preconditions vote on validity, witnesses vote on the intended output y*, the user is asked when
the witnesses are not decisive by `tau`). Pass --no-vote to skip it.

WITNESSES.json is the workflow output: a list of
{id, entry_point, properties: [{name, code}], propertySets: [{name, precondition, generator}],
 implementations: [{name, code}]}.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pbt_agents.sandbox.runner import MAX_CONCURRENT, SandboxUnavailable, run_code

# Runs inside the sandbox. __DATA__ is replaced by a Python string literal holding the job as JSON.
_SCRIPT = r'''
import copy, json, math, signal, sys
from hypothesis import HealthCheck, assume, given, settings

DATA = json.loads(__DATA__)
EP, EXAMPLES, CALL_TIMEOUT = DATA["entry_point"], DATA["examples"], DATA["call_timeout"]

class CallTimeout(Exception):
    pass

def _alarm(*_):
    raise CallTimeout("call exceeded %ss" % CALL_TIMEOUT)

signal.signal(signal.SIGALRM, _alarm)

def timed(fn):
    """Per-call timeout and per-call deepcopy of the arguments."""
    def wrapper(*a):
        signal.setitimer(signal.ITIMER_REAL, CALL_TIMEOUT)
        try:
            return fn(*copy.deepcopy(a))
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
    return wrapper

def outcome(fn, args):
    """-> ("ok", value) | ("raise", text) | ("timeout", text)"""
    try:
        return "ok", timed(fn)(*args)
    except CallTimeout as e:
        return "timeout", str(e)
    except Exception as e:
        return "raise", repr(e)[:200]

def eq(a, b):
    if isinstance(a, float) or isinstance(b, float):
        try:
            return a == b or (math.isnan(a) and math.isnan(b)) or math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6)
        except TypeError:
            return False
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return type(a) is type(b) and len(a) == len(b) and all(eq(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(eq(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b

def load(src):
    ns = {"__name__": "mod"}
    exec(src, ns)
    return ns

def short(x, n=300):
    s = x if isinstance(x, str) else repr(x)
    return s if len(s) <= n else s[:n] + "..."

def err(ps, pr, detail):
    return {"propset": ps, "property": pr, "status": "error", "detail": short(detail)}

out = []
raw = None
try:
    raw = load(DATA["candidate"])[EP]
except BaseException as e:
    for ps in DATA["propsets"]:
        for pr in DATA["properties"]:
            out.append(err(ps["name"], pr["name"], "candidate failed to load: %r" % (e,)))
    print(json.dumps(out)); sys.exit(0)
cand = timed(raw)

wits = []  # (name, function or None)
for w in DATA["witnesses"]:
    try:
        wits.append((w["name"], load(w["code"])[EP]))
    except BaseException:
        wits.append((w["name"], None))

props = []  # (name, function or error text)
for pr in DATA["properties"]:
    try:
        ns = load(pr["code"])
        fn = ns.get("prop_" + pr["name"])
        if fn is None:
            found = [v for k, v in ns.items() if k.startswith("prop_") and callable(v)]
            fn = found[0] if len(found) == 1 else None
        props.append((pr["name"], fn if callable(fn) else "no function prop_" + pr["name"]))
    except BaseException as e:
        props.append((pr["name"], "property failed to load: %r" % (e,)))

for ps in DATA["propsets"]:
    try:
        ns = {"__name__": "props"}
        exec(ps["precondition"], ns)
        exec(ps["generator"], ns)
        pre, strat = ns["precondition"], ns["strategy"]()
    except BaseException as e:
        for name, _ in props:
            out.append(err(ps["name"], name, "propset failed to load: %r" % (e,)))
        continue
    for name, fn in props:
        row = {"propset": ps["name"], "property": name}
        if isinstance(fn, str):
            out.append(err(ps["name"], name, fn)); continue
        st = {"valid": 0, "total": 0, "bad": None}

        @settings(max_examples=EXAMPLES, deadline=None, database=None, derandomize=True,
                  suppress_health_check=list(HealthCheck))
        @given(strat)
        def test(args):
            st["total"] += 1
            try:
                ok = bool(pre(*args))
            except Exception:
                ok = False
            assume(ok)
            st["valid"] += 1
            try:
                fn(cand, *copy.deepcopy(args))
            except AssertionError as e:
                st["bad"] = (args, "assertion", short(str(e) or "AssertionError", 200))
                raise
            except CallTimeout as e:
                st["bad"] = (args, "timeout", str(e))
                raise
            except Exception as e:
                st["bad"] = (args, "exception", repr(e)[:200])
                raise

        try:
            test()
            out.append({**row, "status": "passed" if st["valid"] else "no_valid", "n_valid": st["valid"], "n_total": st["total"]})
        except BaseException as e:
            if st["bad"] is None:
                out.append({**row, "status": "error", "detail": short("test harness failure: %r" % (e,))}); continue
            args, kind, msg = st["bad"]
            ck, cv = outcome(raw, args)
            witnesses, agree, total = {}, 0, 0
            for wn, wf in wits:
                if wf is None:
                    witnesses[wn] = "failed to load"; continue
                wk, wv = outcome(wf, args)
                witnesses[wn] = short(wv) if wk == "ok" else wk + ": " + wv
                if wk == "ok":
                    total += 1
                    agree += int(ck == "ok" and eq(cv, wv))
            out.append({**row, "status": "failed", "failure": kind, "message": msg, "input": short(args),
                        "input_repr": repr(args) if len(repr(args)) <= 20000 else None,
                        "candidate_out": short(cv) if ck == "ok" else ck + ": " + cv,
                        "witnesses": witnesses, "witnesses_agree": agree, "witnesses_total": total})
print(json.dumps(out))
'''


def build_script(candidate: str, entry_point: str, propsets: list[dict], properties: list[dict],
                 witnesses: list[dict], examples: int = 200, call_timeout: float = 2.0) -> str:
    data = {"candidate": candidate, "entry_point": entry_point, "examples": examples,
            "call_timeout": call_timeout,
            "propsets": [{k: p[k] for k in ("name", "precondition", "generator")} for p in propsets],
            "properties": [{k: p[k] for k in ("name", "code")} for p in properties],
            "witnesses": [{"name": w["name"], "code": w["code"]} for w in witnesses]}
    return _SCRIPT.replace("__DATA__", repr(json.dumps(data)))


def check_candidate(candidate: str, entry_point: str, propsets: list[dict], properties: list[dict],
                    witnesses: list[dict], examples: int = 200, timeout: float = 120) -> list[dict]:
    """Run one candidate through every (property set, property) pair in one sandbox run."""
    res = run_code(build_script(candidate, entry_point, propsets, properties, witnesses, examples), timeout=timeout)
    pairs = [(s["name"], p["name"]) for s in propsets for p in properties]
    if res["timed_out"]:
        return [{"propset": s, "property": p, "status": "error", "detail": "sandbox timeout"} for s, p in pairs]
    try:
        return json.loads(res["stdout"].strip().splitlines()[-1])
    except (IndexError, ValueError):
        detail = (res["stderr"] or res["stdout"]).strip()[-300:] or f"exit {res['exit_code']}"
        return [{"propset": s, "property": p, "status": "error", "detail": detail} for s, p in pairs]


def run(tasks: list[dict], mutants: list[dict], examples: int = 200, timeout: float = 120,
        workers: int = MAX_CONCURRENT) -> list[dict]:
    """-> one row per (candidate, property set, property)."""
    by_task = defaultdict(list)
    for m in mutants:
        by_task[m["task_id"]].append(m)
    jobs = []  # (task, candidate id, kind, source)
    for t in tasks:
        if not t.get("properties") or not t.get("propertySets"):
            print(f"warning: {t['id']} needs at least one property and one property set; skipped", file=sys.stderr)
            continue
        ms = by_task.get(t["id"], [])
        if not ms:
            print(f"warning: no mutants for {t['id']}", file=sys.stderr)
            continue
        m0 = ms[0]
        jobs.append((t, t["id"], "canonical", m0["prompt"] + m0["canonical_solution"]))
        jobs += [(t, m["id"], "mutant", m["prompt"] + m["mutated_solution"]) for m in ms]

    def one(job):
        t, cid, kind, src = job
        rows = check_candidate(src, t["entry_point"], t["propertySets"], t["properties"],
                               t.get("implementations") or [], examples, timeout)
        return [{"task_id": t["id"], "candidate": cid, "kind": kind, **r} for r in rows]

    with ThreadPoolExecutor(max(1, workers)) as ex:
        return [r for rows in ex.map(one, jobs) for r in rows]


def summarize(rows: list[dict]) -> str:
    lines = []
    canon = [r for r in rows if r["kind"] == "canonical"]
    bad = [r for r in canon if r["status"] == "failed"]
    lines.append(f"canonical solutions failing a property: {len(bad)} of {len(canon)} checks (should be 0)")
    for r in bad[:10]:
        lines.append(f"  {r['task_id']} {r['propset']}/{r['property']} [{r['failure']}] input={r['input']} "
                     f"program={r['candidate_out']} witnesses_agree={r['witnesses_agree']}/{r['witnesses_total']}")
    errs = [r for r in rows if r["status"] == "error"]
    if errs:
        lines.append(f"errors: {len(errs)} rows, e.g. {errs[0]['task_id']} {errs[0]['propset']}/{errs[0]['property']}: {errs[0].get('detail')}")
    mut = [r for r in rows if r["kind"] == "mutant"]
    cands = defaultdict(list)
    for r in mut:
        cands[r["candidate"]].append(r)
    caught = {c for c, rs in cands.items() if any(r["status"] == "failed" for r in rs)}
    lines.append(f"mutants caught by at least one property: {len(caught)} of {len(cands)}")
    per = defaultdict(set)
    for r in mut:
        if r["status"] == "failed":
            per[(r["task_id"], r["property"])].add(r["candidate"])
    n_mut = defaultdict(int)
    for c in cands:
        n_mut[c.rsplit("/mut", 1)[0]] += 1
    for (task, prop), ks in sorted(per.items()):
        lines.append(f"  {task} {prop}: catches {len(ks)} of {n_mut[task]}")
    survivors = sorted(set(cands) - caught)
    if survivors:
        lines.append("uncaught mutants: " + ", ".join(survivors[:30]) + (" ..." if len(survivors) > 30 else ""))
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("witnesses", help="workflow output JSON (list of tasks)")
    ap.add_argument("--mutants", default="data/humanevalplus_mutants.jsonl")
    ap.add_argument("--out", help="write one JSON row per (candidate, property set, property) here")
    ap.add_argument("--examples", type=int, default=200)
    ap.add_argument("--timeout", type=float, default=120, help="sandbox timeout per candidate (seconds)")
    ap.add_argument("--no-vote", action="store_true", help="skip the voting step on failing inputs")
    ap.add_argument("--tau", type=float, default=0.5, help="required lead of the top witness output over the runner-up")
    ap.add_argument("--labels", default="data/labels.json", help="saved human answers (read and updated)")
    ap.add_argument("--votes-out", help="write one JSON record per failing input (voting result) here")
    ap.add_argument("--no-ask", action="store_true", help="never prompt; undecided inputs stay pending")
    a = ap.parse_args()
    tasks = json.loads(Path(a.witnesses).read_text())
    mutants = [json.loads(l) for l in Path(a.mutants).read_text().splitlines() if l.strip()]
    try:
        rows = run(tasks, mutants, a.examples, a.timeout)
    except SandboxUnavailable as e:
        sys.exit(f"sandbox unavailable: {e}")
    if a.out:
        Path(a.out).write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(summarize(rows))
    if a.no_vote or not any(r["status"] == "failed" for r in rows):
        return
    from pbt_agents import vote as V
    lp = Path(a.labels)
    labels = json.loads(lp.read_text()) if lp.exists() else {}
    try:
        recs = V.vote(tasks, rows, mutants, a.tau, labels, interactive=not a.no_ask and sys.stdin.isatty())
    except SandboxUnavailable as e:
        sys.exit(f"sandbox unavailable: {e}")
    if labels:
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text(json.dumps(labels, indent=1))
    if a.votes_out:
        Path(a.votes_out).write_text("".join(json.dumps(r) + "\n" for r in recs))
    print("\nvoting on failing inputs (tau=%s)\n%s" % (a.tau, V.summarize(recs)))
    pend = [r for r in recs if r["decision"] == "pending"]
    if pend:
        print(f"{len(pend)} inputs need a human answer: rerun in a terminal, or add them to {a.labels}")


if __name__ == "__main__":
    main()

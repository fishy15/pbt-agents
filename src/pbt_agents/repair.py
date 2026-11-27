"""Repair programs that the vote found to be wrong (their output differs from y*).

`vote.py` labels a failing program "bug" when, on a legitimate input, its output differs from the
intended output y*. This module turns those labels into repair jobs, checks proposed repairs in the
sandbox, and scores finished repairs. The repair itself is done by subagents (the `repair` workflow,
`.claude/workflows/repair.js`), which call `show` and `check` below.

Only mutants are repaired (a canonical solution voted wrong gets no job; the private canonical
solutions are never put in a job); the witnesses are not touched. A repair is judged only from the spec, the confirmed cases and the shared
properties. The canonical solution is never shown to the repair agent. `evaluate` uses it afterwards,
as a held-out measure of whether the repair really restored the original behaviour.

    # 1. one job per program with a confirmed bug (harness --jobs-out does this too)
    uv run python -m pbt_agents.repair jobs WITNESSES.json VOTES.jsonl --out data/repair_jobs.json
    # 2. what a repair agent sees / how it checks a candidate fix (sandboxed)
    uv run python -m pbt_agents.repair show  JOBS.json CANDIDATE_ID
    uv run python -m pbt_agents.repair check JOBS.json CANDIDATE_ID FIXED.py
    # 3. score the repairs the workflow returned: [{id, code}, ...]
    uv run python -m pbt_agents.repair evaluate JOBS.json REPAIRS.json [--out scored.jsonl]

A repair check has two parts. Confirmed cases: on every (input, y*) the program failed on, the fix
must return y* (y* is recomputed exactly from a witness of the winning group, so truncated reprs
don't matter). Properties: the shared properties are rerun on the fix; failures there are reported
as warnings, since the property itself may be the culprit.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pbt_agents import harness, private
from pbt_agents.sandbox.runner import MAX_CONCURRENT, SandboxUnavailable, run_code

_PRELUDE = r'''
import copy, json, math, signal, sys

DATA = json.loads(__DATA__)
EP, T = DATA["entry_point"], DATA["call_timeout"]

class CallTimeout(Exception):
    pass

def _alarm(*_):
    raise CallTimeout("call exceeded %ss" % T)

signal.signal(signal.SIGALRM, _alarm)

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

def short(x, n=300):
    s = x if isinstance(x, str) else repr(x)
    return s if len(s) <= n else s[:n] + "..."

def call(fn, args):
    signal.setitimer(signal.ITIMER_REAL, T)
    try:
        return "ok", fn(*copy.deepcopy(args))
    except CallTimeout as e:
        return "timeout", str(e)
    except Exception as e:
        return "raise", repr(e)[:200]
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)

def load(src):
    ns = {"__name__": "mod"}
    exec(src, ns)
    return ns

def parse(rep):
    return eval(rep, {"nan": float("nan"), "inf": float("inf")})
'''

# Does the fix return y* on each confirmed case? y* is recomputed by running the case's witness.
_CASES = _PRELUDE + r'''
out = []
try:
    fix = load(DATA["code"])[EP]
except BaseException as e:
    print(json.dumps([{"ok": False, "got": "failed to load: %r" % (e,)} for _ in DATA["cases"]])); sys.exit(0)
wits = {}
for w in DATA["witnesses"]:
    try:
        wits[w["name"]] = load(w["code"])[EP]
    except BaseException:
        pass
for c in DATA["cases"]:
    try:
        args = parse(c["input_repr"])
    except BaseException as e:
        out.append({"ok": False, "got": "bad input repr: %r" % (e,)}); continue
    wk, want = call(wits[c["witness"]], args) if c["witness"] in wits else ("raise", "witness missing")
    gk, got = call(fix, args)
    out.append({"ok": wk == "ok" and gk == "ok" and eq(got, want),
                "got": short(got) if gk == "ok" else gk + ": " + got,
                "want": short(want) if wk == "ok" else wk + ": " + want})
print(json.dumps(out))
'''

# Held-out score: does the fix agree with the canonical solution on inputs the property sets generate?
_VS_CANONICAL = _PRELUDE + r'''
from hypothesis import HealthCheck, assume, given, settings

fix = load(DATA["code"])[EP]
ref = load(DATA["reference"])[EP]
checked, bad = 0, []
for ps in DATA["propsets"]:
    try:
        ns = {"__name__": "props"}
        exec(ps["precondition"], ns); exec(ps["generator"], ns)
        pre, strat = ns["precondition"], ns["strategy"]()
    except BaseException:
        continue

    @settings(max_examples=DATA["examples"], deadline=None, database=None, derandomize=True,
              suppress_health_check=list(HealthCheck))
    @given(strat)
    def test(args):
        global checked
        try:
            assume(bool(pre(*args)))
        except Exception:
            assume(False)
        rk, rv = call(ref, args)
        if rk != "ok":
            return
        checked += 1
        fk, fv = call(fix, args)
        if not (fk == "ok" and eq(rv, fv)) and len(bad) < 3:
            bad.append({"input": short(args), "canonical": short(rv), "fix": short(fv) if fk == "ok" else fk + ": " + fv})

    try:
        test()
    except BaseException:
        pass
print(json.dumps({"checked": checked, "mismatches": bad}))
'''


def _data(script: str, data: dict) -> str:
    return script.replace("__DATA__", repr(json.dumps(data)))


def _last_json(res: dict):
    try:
        return json.loads(res["stdout"].strip().splitlines()[-1])
    except (IndexError, ValueError):
        return None


# --------------------------------------------------------------------------- jobs

def make_jobs(tasks: list[dict], votes: list[dict], mutants: list[dict]) -> dict:
    """-> {"tasks": {id: {...}}, "jobs": [...]} with one job per program that has a confirmed bug."""
    tmap = {t["id"]: t for t in tasks}
    info = {}
    for m in mutants:
        info[m["id"]] = (m["prompt"], "mutant", m["prompt"] + m["mutated_solution"])
    jobs = {}
    for v in votes:
        ys = v.get("y_star")
        members = (v.get("witness_outputs") or {}).get(ys) if ys is not None else None
        if not members:
            continue
        for cid, vd in (v.get("verdicts") or {}).items():
            if vd["verdict"] != "bug" or cid not in info or v["task_id"] not in tmap:
                continue
            prompt, kind, code = info[cid]
            j = jobs.setdefault(cid, {"id": cid, "task_id": v["task_id"], "kind": kind,
                                      "entry_point": tmap[v["task_id"]]["entry_point"],
                                      "prompt": prompt, "code": code, "cases": []})
            j["cases"].append({"input_repr": v["input_repr"], "expected": ys, "witness": members[0],
                               "got": vd["output"], "failed_properties": vd["properties"]})
    used = {j["task_id"] for j in jobs.values()}
    keep = ("entry_point", "propertySets", "properties", "implementations")
    return {"tasks": {t: {k: tmap[t].get(k) or [] for k in keep} for t in sorted(used)},
            "jobs": sorted(jobs.values(), key=lambda j: j["id"])}


def _job(jobs: dict, cid: str) -> dict:
    for j in jobs["jobs"]:
        if j["id"] == cid:
            return j
    sys.exit(f"no repair job {cid!r}; jobs: {', '.join(j['id'] for j in jobs['jobs'])}")


def show(job: dict) -> str:
    lines = [f"# {job['id']} (entry point {job['entry_point']})", "", "## Spec and current code", job["code"].rstrip(), "",
             f"## {len(job['cases'])} confirmed wrong output(s)",
             "On each input below the program is wrong: `expected` is the output the witnesses (or the user) agreed on."]
    for i, c in enumerate(job["cases"]):
        lines += [f"[{i}] input    = {c['input_repr'][:600]}", f"    expected = {c['expected']}",
                  f"    program  = {c['got']}", f"    caught by: {', '.join(c['failed_properties'])}"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- checking

def check_cases(job: dict, task: dict, code: str, timeout: float = 120) -> list[dict]:
    data = {"entry_point": job["entry_point"], "code": code, "call_timeout": 2.0, "cases": job["cases"],
            "witnesses": [{"name": w["name"], "code": w["code"]} for w in task["implementations"]]}
    res = run_code(_data(_CASES, data), timeout=timeout)
    out = _last_json(res)
    if not isinstance(out, list) or len(out) != len(job["cases"]):
        detail = (res["stderr"] or res["stdout"]).strip()[-300:] or f"exit {res['exit_code']}"
        return [{"ok": False, "got": "sandbox error: " + detail} for _ in job["cases"]]
    return out


def check(job: dict, task: dict, code: str, examples: int = 100, timeout: float = 120) -> dict:
    """-> {"cases": [...], "cases_ok": n, "props_failed": [...], "ok": all cases pass, "clean": ok and no property fails}"""
    cases = check_cases(job, task, code, timeout)
    rows = harness.check_candidate(code, job["entry_point"], task["propertySets"], task["properties"],
                                   task["implementations"], examples, timeout)
    bad = [r for r in rows if r["status"] == "failed"]
    errs = [r for r in rows if r["status"] == "error"]
    ok = all(c["ok"] for c in cases)
    return {"cases": cases, "cases_ok": sum(c["ok"] for c in cases), "props_failed": bad, "props_error": errs,
            "ok": ok, "clean": ok and not bad and not errs}


def format_check(job: dict, r: dict) -> str:
    lines = [f"confirmed cases fixed: {r['cases_ok']}/{len(r['cases'])}"]
    for i, c in enumerate(r["cases"]):
        if not c["ok"]:
            lines.append(f"  [{i}] still wrong: program returns {c.get('got')}, expected {c.get('want', job['cases'][i]['expected'])}")
    lines.append(f"shared properties failing on the fix: {len(r['props_failed'])}")
    for p in r["props_failed"][:6]:
        lines.append(f"  {p['propset']}/{p['property']} [{p['failure']}] input={p['input'][:200]} program={p['candidate_out']} "
                     f"({p['witnesses_agree']}/{p['witnesses_total']} witnesses agree with it)")
    if r["props_failed"]:
        lines.append("  (a property can itself be wrong: if the program's output is what the spec says, leave it; "
                     "if it is a new real bug your fix introduced or missed, fix it)")
    for p in r["props_error"][:3]:
        lines.append(f"  error in {p['propset']}/{p['property']}: {p.get('detail')}")
    lines.append("RESULT: " + ("all confirmed cases fixed" + (", no property fails" if r["clean"] else ", but see the property failures") if r["ok"] else "not fixed yet"))
    return "\n".join(lines)


# --------------------------------------------------------------------------- evaluation

def agrees_with_canonical(code: str, reference: str, task: dict, entry_point: str, examples: int = 200, timeout: float = 180) -> dict:
    data = {"entry_point": entry_point, "code": code, "reference": reference, "call_timeout": 2.0, "examples": examples,
            "propsets": [{k: p[k] for k in ("name", "precondition", "generator")} for p in task["propertySets"]]}
    res = run_code(_data(_VS_CANONICAL, data), timeout=timeout)
    out = _last_json(res)
    return out if isinstance(out, dict) else {"checked": 0, "mismatches": [], "error": (res["stderr"] or res["stdout"]).strip()[-300:]}


def evaluate(jobs: dict, repairs: list[dict], mutants: list[dict], workers: int = MAX_CONCURRENT) -> list[dict]:
    canon = private.load_canonical()
    by_id = {j["id"]: j for j in jobs["jobs"]}

    def one(rep):
        job = by_id.get(rep.get("id"))
        if job is None or not rep.get("code"):
            return {"id": rep.get("id"), "verdict": "invalid"}
        task = jobs["tasks"][job["task_id"]]
        r = check(job, task, rep["code"])
        rec = {"id": job["id"], "task_id": job["task_id"], "kind": job["kind"], "cases_fixed": r["cases_ok"],
               "cases_total": len(r["cases"]), "props_failing": sorted({f"{p['propset']}/{p['property']}" for p in r["props_failed"]}),
               "code": rep["code"], "notes": rep.get("notes", "")}
        if job["kind"] == "mutant" and job["task_id"] in canon:
            v = agrees_with_canonical(rep["code"], canon[job["task_id"]], task, job["entry_point"])
            rec["canonical_checked"], rec["canonical_mismatches"] = v["checked"], v["mismatches"]
        rec["verdict"] = ("repaired" if r["ok"] and not rec.get("canonical_mismatches")
                          else "plausible_only" if r["ok"] else "failed")
        return rec

    with ThreadPoolExecutor(max(1, workers)) as ex:
        return list(ex.map(one, repairs))


def summarize(recs: list[dict]) -> str:
    by = defaultdict(list)
    for r in recs:
        by[r["verdict"]].append(r["id"])
    lines = [f"repairs scored: {len(recs)}"]
    names = {"repaired": "fixes every confirmed case and agrees with the canonical solution",
             "plausible_only": "fixes every confirmed case but differs from the canonical solution elsewhere",
             "failed": "still wrong on a confirmed case", "invalid": "unknown id or no code"}
    for k in ("repaired", "plausible_only", "failed", "invalid"):
        if by.get(k):
            lines.append(f"  {k} ({names[k]}): {len(by[k])}: {', '.join(sorted(by[k])[:10])}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- CLI

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("jobs", help="build repair jobs from the vote results")
    p.add_argument("witnesses"); p.add_argument("votes", help="vote records JSONL (harness --votes-out)")
    p.add_argument("--mutants", default="data/humanevalplus_mutants.jsonl"); p.add_argument("--out", default="data/repair_jobs.json")
    p = sub.add_parser("show", help="print what a repair agent works from")
    p.add_argument("jobs"); p.add_argument("id")
    p = sub.add_parser("check", help="check a proposed fix in the sandbox")
    p.add_argument("jobs"); p.add_argument("id"); p.add_argument("code", help="file with the full fixed module")
    p.add_argument("--examples", type=int, default=100)
    p = sub.add_parser("evaluate", help="score finished repairs (also compares with the canonical solution)")
    p.add_argument("jobs"); p.add_argument("repairs", help="JSON: [{id, code}] or {results: [...]}")
    p.add_argument("--mutants", default="data/humanevalplus_mutants.jsonl"); p.add_argument("--out")
    a = ap.parse_args()

    def lines(path):
        return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]

    if a.cmd == "jobs":
        jobs = make_jobs(json.loads(Path(a.witnesses).read_text()), lines(a.votes), lines(a.mutants))
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(jobs, indent=1))
        print(f"{len(jobs['jobs'])} repair jobs ({sum(len(j['cases']) for j in jobs['jobs'])} confirmed cases) -> {a.out}")
        return
    jobs = json.loads(Path(a.jobs).read_text())
    try:
        if a.cmd == "show":
            print(show(_job(jobs, a.id)))
        elif a.cmd == "check":
            job = _job(jobs, a.id)
            r = check(job, jobs["tasks"][job["task_id"]], Path(a.code).read_text(), a.examples)
            print(format_check(job, r))
            sys.exit(0 if r["ok"] else 1)
        else:
            reps = json.loads(Path(a.repairs).read_text())
            reps = reps.get("results", reps) if isinstance(reps, dict) else reps
            recs = evaluate(jobs, reps, lines(a.mutants))
            if a.out:
                Path(a.out).write_text("".join(json.dumps(r) + "\n" for r in recs))
            print(summarize(recs))
    except SandboxUnavailable as e:
        sys.exit(f"sandbox unavailable: {e}")


if __name__ == "__main__":
    main()

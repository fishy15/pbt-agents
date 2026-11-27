"""Repair programs that the vote found to be wrong (their output differs from y*).

`vote.py` labels a failing program "bug" when, on a legitimate input, its output differs from the
intended output y*. This module turns those labels into repair jobs, checks proposed repairs in the
sandbox, and scores finished repairs. The repair itself is done by subagents (the `repair` workflow,
`.claude/workflows/repair.js`), which call `show` and `check` below.

Two kinds of repair, depending on the vote's verdict:

  bug             the program's output differs from y*      -> repair the program (jobs["jobs"])
  property_wrong  the program's output equals y*, yet a     -> repair the property (jobs["property_jobs"])
                  property rejected it

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

# Does the fixed property accept the confirmed correct outputs and still reject the confirmed bugs?
_PROP_CHECK = _PRELUDE + r'''
ns = load(DATA["code"])
fn = ns.get("prop_" + DATA["name"])
if fn is None:
    found = [v for k, v in ns.items() if k.startswith("prop_") and callable(v)]
    fn = found[0] if len(found) == 1 else None
if not callable(fn):
    print(json.dumps({"error": "no function prop_" + DATA["name"]})); sys.exit(0)

def run_prop(cand, args):
    """-> None if the property holds, else a short description of how it failed."""
    def timed(*a):
        signal.setitimer(signal.ITIMER_REAL, T)
        try:
            return cand(*copy.deepcopy(a))
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
    try:
        fn(timed, *copy.deepcopy(args))
        return None
    except AssertionError as e:
        return "assertion: " + short(str(e) or "AssertionError", 200)
    except CallTimeout as e:
        return "timeout: " + str(e)
    except Exception as e:
        return "exception: " + repr(e)[:200]

wits = {}
for w in DATA["witnesses"]:
    try:
        wits[w["name"]] = load(w["code"])[EP]
    except BaseException:
        pass
cases, regress = [], []
for c in DATA["cases"]:
    try:
        args = parse(c["input_repr"])
        msg = run_prop(wits[c["witness"]], args) if c["witness"] in wits else "witness missing"
    except BaseException as e:
        msg = "could not run: %r" % (e,)
    cases.append({"ok": msg is None, "msg": msg})
for m in DATA["must_fail"]:
    try:
        args = parse(m["input_repr"])
        msg = run_prop(load(m["code"])[EP], args)
    except BaseException as e:
        msg = "could not run: %r" % (e,)
    regress.append({"id": m["id"], "caught": msg is not None})
print(json.dumps({"cases": cases, "regress": regress}))
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
    pjobs = _property_jobs(tmap, votes, info)
    used = {j["task_id"] for j in jobs.values()} | {j["task_id"] for j in pjobs}
    keep = ("entry_point", "propertySets", "properties", "implementations")
    return {"tasks": {t: {k: tmap[t].get(k) or [] for k in keep} for t in sorted(used)},
            "jobs": sorted(jobs.values(), key=lambda j: j["id"]),
            "property_jobs": sorted(pjobs, key=lambda j: j["id"])}


def _property_jobs(tmap: dict, votes: list[dict], info: dict) -> list[dict]:
    """One job per property that raised on a run in which the program was right on every call it received."""
    calls = {(v["task_id"], v["input_repr"]): v for v in votes if v.get("type", "call") == "call" and v.get("y_star") is not None}
    cases, bugs = defaultdict(list), defaultdict(list)
    for r in votes:
        if r.get("type") != "row" or r["task_id"] not in tmap or not r.get("input_repr"):
            continue
        key = (r["task_id"], r["property"])
        if r["verdict"] == "bug" and r["candidate"] in info:
            bugs[key].append({"id": r["candidate"], "code": info[r["candidate"]][2], "input_repr": r["input_repr"]})
        elif r["verdict"] == "property_wrong":
            cs, common = [], None
            for c in r["calls"]:
                v = next((calls[k] for k in calls if k[0] == r["task_id"] and k[1][:300] == c["input"]), None)
                if v is None:
                    continue
                members = set(v["witness_outputs"].get(v["y_star"], []))
                common = members if common is None else common & members
                cs.append({"call": v["input_repr"], "expected": v["y_star"],
                           "program_output": v["verdicts"][r["candidate"]]["output"]})
            if cs and common is not None:
                # a witness that gave the agreed output on every call the property made
                cases[key].append({"input_repr": r["input_repr"], "witness": sorted(common)[0] if common else None,
                                   "program": r["candidate"], "propset": r["propset"], "calls": cs})
    out = []
    for (tid, prop), cs in cases.items():
        cs = [c for c in cs if c["witness"]]
        cur = next((p for p in tmap[tid].get("properties") or [] if p["name"] == prop), None)
        if cur is None or not cs:
            continue
        prompt = next((i[0] for k, i in info.items() if k.startswith(tid + "/mut")), "")
        out.append({"id": f"{tid}::{prop}", "target": "property", "task_id": tid, "property": prop,
                    "entry_point": tmap[tid]["entry_point"], "prompt": prompt, "code": cur["code"],
                    "cases": cs, "must_still_fail": bugs.get((tid, prop), [])})
    return out


def _job(jobs: dict, cid: str) -> dict:
    every = jobs["jobs"] + jobs.get("property_jobs", [])
    for j in every:
        if j["id"] == cid:
            return j
    sys.exit(f"no repair job {cid!r}; jobs: {', '.join(j['id'] for j in every)}")


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


# --------------------------------------------------------------------------- property repair

def show_property(job: dict) -> str:
    lines = [f"# property {job['property']} of {job['task_id']} (entry point {job['entry_point']})", "",
             "## Spec", job["prompt"].rstrip(), "", "## Current property (shared by all programs; written from the spec alone)",
             job["code"].rstrip(), "",
             f"## {len(job['cases'])} run(s) on which the property rejects a program that behaved correctly",
             "On each run the property called the program one or more times; every call returned the output the witnesses (or the user) agreed on, yet the property raised."]
    for i, c in enumerate(job["cases"]):
        lines.append(f"[{i}] property arguments = {c['input_repr'][:600]}   (property set {c['propset']})")
        for k in c["calls"]:
            lines.append(f"      the property called the program on {k['call'][:300]}")
            lines.append(f"        correct output (witnesses/user agree): {k['expected']}   program {c['program']} returned: {k['program_output']}")
    lines += ["", f"## {len(job['must_still_fail'])} real bug(s) the fixed property must still reject"]
    for m in job["must_still_fail"]:
        lines.append(f"  {m['id']} on input {m['input_repr'][:300]}")
    return "\n".join(lines)


def check_property(job: dict, task: dict, code: str, examples: int = 100, timeout: float = 120) -> dict:
    """-> {"cases": [...], "regress": [...], "witness_failures": [...], "ok", "clean"}"""
    data = {"entry_point": job["entry_point"], "name": job["property"], "code": code, "call_timeout": 2.0,
            "cases": job["cases"], "must_fail": job["must_still_fail"],
            "witnesses": [{"name": w["name"], "code": w["code"]} for w in task["implementations"]]}
    res = run_code(_data(_PROP_CHECK, data), timeout=timeout)
    out = _last_json(res)
    if not isinstance(out, dict) or "cases" not in out:
        detail = (out or {}).get("error") if isinstance(out, dict) else None
        detail = detail or (res["stderr"] or res["stdout"]).strip()[-300:] or f"exit {res['exit_code']}"
        return {"cases": [{"ok": False, "msg": detail}] * max(1, len(job["cases"])), "regress": [], "witness_failures": [],
                "ok": False, "clean": False}
    fails = []
    for w in task["implementations"]:
        rows = harness.check_candidate(w["code"], job["entry_point"], task["propertySets"],
                                       [{"name": job["property"], "code": code}], [], examples, timeout)
        fails += [{"witness": w["name"], **r} for r in rows if r["status"] in ("failed", "error")]
    ok = all(c["ok"] for c in out["cases"]) and all(m["caught"] for m in out["regress"])
    return {**out, "witness_failures": fails, "ok": ok, "clean": ok and not fails}


def format_property_check(job: dict, r: dict) -> str:
    n = len(r["cases"])
    lines = [f"correct outputs now accepted: {sum(c['ok'] for c in r['cases'])}/{n}"]
    for i, c in enumerate(r["cases"]):
        if not c["ok"]:
            lines.append(f"  [{i}] property still rejects the correct output: {c['msg']}")
    lines.append(f"real bugs still rejected: {sum(m['caught'] for m in r['regress'])}/{len(r['regress'])}")
    for m in r["regress"]:
        if not m["caught"]:
            lines.append(f"  {m['id']}: the property no longer notices this bug")
    lines.append(f"witness implementations failing the property on generated inputs: {len(r['witness_failures'])}")
    for f in r["witness_failures"][:5]:
        lines.append(f"  {f['witness']} {f['propset']} [{f.get('failure', f.get('status'))}] input={str(f.get('input', ''))[:200]} "
                     f"{f.get('message') or f.get('detail', '')}")
    if r["witness_failures"]:
        lines.append("  (witnesses are not oracles; but if several fail, the property may still be too strict)")
    lines.append("RESULT: " + (("property accepts every confirmed correct output and still rejects every confirmed bug"
                               + ("" if r["clean"] else ", but see the witness failures")) if r["ok"] else "not fixed yet"))
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
    by_id = {j["id"]: j for j in jobs["jobs"] + jobs.get("property_jobs", [])}
    by_task = defaultdict(list)
    for m in mutants:
        by_task[m["task_id"]].append(m)

    def one_property(job, task, rep):
        r = check_property(job, task, rep["code"])
        rec = {"id": job["id"], "task_id": job["task_id"], "target": "property", "cases_fixed": sum(c["ok"] for c in r["cases"]),
               "cases_total": len(r["cases"]), "bugs_still_caught": sum(m["caught"] for m in r["regress"]),
               "bugs_total": len(r["regress"]), "witness_failures": len(r["witness_failures"]),
               "code": rep["code"], "notes": rep.get("notes", "")}
        pr = [{"name": job["property"], "code": rep["code"]}]
        old = [{"name": job["property"], "code": job["code"]}]

        def failing(src, props):
            rows = harness.check_candidate(src, job["entry_point"], task["propertySets"], props, [], 100, 120)
            return [x for x in rows if x["status"] == "failed"]
        if job["task_id"] in canon:   # held out: the original solution must satisfy the property
            bad = failing(canon[job["task_id"]], pr)
            rec["canonical_failures"] = [f"{x['propset']}: input={x['input'][:150]} program={x['candidate_out']}" for x in bad[:3]]
        mut = by_task.get(job["task_id"], [])
        rec["mutants_caught_before"] = sum(bool(failing(m["prompt"] + m["mutated_solution"], old)) for m in mut)
        rec["mutants_caught_after"] = sum(bool(failing(m["prompt"] + m["mutated_solution"], pr)) for m in mut)
        rec["mutants_total"] = len(mut)
        rec["verdict"] = ("repaired" if r["ok"] and not rec.get("canonical_failures")
                          else "plausible_only" if r["ok"] else "failed")
        return rec

    def one(rep):
        job = by_id.get(rep.get("id"))
        if job is None or not rep.get("code"):
            return {"id": rep.get("id"), "verdict": "invalid"}
        task = jobs["tasks"][job["task_id"]]
        if job.get("target") == "property":
            return one_property(job, task, rep)
        r = check(job, task, rep["code"])
        rec = {"id": job["id"], "task_id": job["task_id"], "target": "program", "kind": job["kind"], "cases_fixed": r["cases_ok"],
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
    names = {"repaired": "program: fixes every confirmed case and agrees with the canonical solution; "
                         "property: accepts the correct outputs, still rejects the bugs, canonical passes it",
             "plausible_only": "passes the confirmed checks but not the held-out canonical comparison",
             "failed": "fails a confirmed check", "invalid": "unknown id or no code"}
    for k in ("repaired", "plausible_only", "failed", "invalid"):
        if by.get(k):
            lines.append(f"  {k} ({names[k]}): {len(by[k])}: {', '.join(sorted(by[k])[:10])}")
    props = [r for r in recs if r.get("target") == "property"]
    if props:
        lines.append(f"  properties: mutants caught before -> after the repair: "
                     f"{sum(r['mutants_caught_before'] for r in props)} -> {sum(r['mutants_caught_after'] for r in props)} "
                     f"of {sum(r['mutants_total'] for r in props)} (counted per property)")
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
        print(f"{len(jobs['jobs'])} program repair jobs ({sum(len(j['cases']) for j in jobs['jobs'])} confirmed cases), "
              f"{len(jobs['property_jobs'])} property repair jobs -> {a.out}")
        return
    jobs = json.loads(Path(a.jobs).read_text())
    try:
        if a.cmd == "show":
            job = _job(jobs, a.id)
            print(show_property(job) if job.get("target") == "property" else show(job))
        elif a.cmd == "check":
            job = _job(jobs, a.id)
            task, code = jobs["tasks"][job["task_id"]], Path(a.code).read_text()
            if job.get("target") == "property":
                r = check_property(job, task, code, a.examples)
                print(format_property_check(job, r))
            else:
                r = check(job, task, code, a.examples)
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

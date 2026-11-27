"""Vote on whether a failing input is legitimate and what the intended output y* is.

For every distinct failing input found by the harness (per task):

1. Precondition vote. Every property set's precondition is run on the input. If more than half of
   them reject it (False or raise), the input is rejected and the failure is dismissed.
2. Witness vote. Otherwise every witness implementation is run on the input and the outputs are
   grouped by equality (`eq`). Let p1 be the fraction of the largest group and p2 the fraction of
   the runner-up (fractions are over the witnesses that returned a value; witnesses that raise or
   time out do not vote). If p1 - p2 >= tau, the largest group's output is y*.
3. Otherwise the user is shown the task and the input and picks which witness output is right.
   Answers are saved in a labels file keyed by (task, input), so each question is asked once.
   Without a terminal the questions are printed and left "pending" (put answers in the labels file
   and rerun).

The inputs voted on are the *calls* the failing property run made to the program, not the property's
own arguments (a metamorphic property calls the program on inputs derived from them). Every call of a
failing property run is judged, then the run gets one verdict: "bug" if some call returned something
other than y* (the program is wrong), "property_wrong" if every call was valid and returned y* yet the
property raised (the property is wrong), "dismissed" if a call was an invalid input (the property built
one), "unresolved" if some call has no y* (pending a human answer).

    uv run python -m pbt_agents.vote WITNESSES.json HARNESS_ROWS.jsonl [--tau 0.5]
        [--mutants data/humanevalplus_mutants.jsonl] [--labels data/labels.json] [--out votes.jsonl]

HARNESS_ROWS.jsonl is the harness `--out` file (it must contain `input_repr`).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pbt_agents import private
from pbt_agents.sandbox.runner import MAX_CONCURRENT, SandboxUnavailable, run_code

# Runs inside the sandbox. __DATA__ is replaced by a Python string literal holding the job as JSON.
_SCRIPT = r'''
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

args = eval(DATA["args_repr"], {"nan": float("nan"), "inf": float("inf")})

pre = []
for ps in DATA["propsets"]:
    try:
        ns = {"__name__": "props"}
        exec(ps["precondition"], ns)
        pre.append({"name": ps["name"], "accept": bool(ns["precondition"](*copy.deepcopy(args)))})
    except BaseException:
        pre.append({"name": ps["name"], "accept": False})

groups, wits = [], []   # groups: {"repr", "value", "members"}
for w in DATA["witnesses"]:
    try:
        k, v = call(load(w["code"])[EP], args)
    except BaseException as e:
        k, v = "raise", "failed to load: %r" % (e,)
    row = {"name": w["name"], "kind": k, "repr": short(v), "group": None}
    if k == "ok":
        for i, g in enumerate(groups):
            if eq(g["value"], v):
                g["members"].append(w["name"]); row["group"] = i; break
        else:
            groups.append({"repr": short(v), "value": v, "members": [w["name"]]}); row["group"] = len(groups) - 1
    wits.append(row)

cands = []
for c in DATA["candidates"]:
    try:
        k, v = call(load(c["code"])[EP], args)
    except BaseException as e:
        k, v = "raise", "failed to load: %r" % (e,)
    match = None
    if k == "ok":
        match = next((i for i, g in enumerate(groups) if eq(g["value"], v)), None)
    cands.append({"id": c["id"], "kind": k, "repr": short(v), "group": match})

print(json.dumps({"pre": pre, "witnesses": wits, "cand": cands,
                  "groups": [{"repr": g["repr"], "members": g["members"]} for g in groups]}))
'''


def decide(info: dict, tau: float) -> dict:
    """Apply the two votes to one sandbox report -> {"decision", "group", ...}. Pure, no I/O."""
    pre = info["pre"]
    accept = sum(p["accept"] for p in pre)
    base = {"pre_accept": accept, "pre_total": len(pre), "tau": tau}
    if len(pre) - accept > len(pre) / 2:
        return {**base, "decision": "rejected", "group": None}
    groups = info["groups"]
    sizes = sorted((len(g["members"]) for g in groups), reverse=True)
    voters = sum(sizes)
    if voters == 0:
        return {**base, "decision": "ask", "group": None, "margin": None}
    p1 = sizes[0] / voters
    p2 = (sizes[1] / voters) if len(sizes) > 1 else 0.0
    base["margin"] = round(p1 - p2, 4)
    if p1 - p2 >= tau - 1e-9:
        return {**base, "decision": "witnesses", "group": max(range(len(groups)), key=lambda i: len(groups[i]["members"]))}
    return {**base, "decision": "ask", "group": None}


def _key(task_id: str, input_repr: str) -> str:
    return task_id + "|" + input_repr


def _ask(task: dict, prompt: str, info: dict, d: dict) -> dict | None:
    """Show the task, input and witness outputs; return a label or None if the user skipped."""
    print("\n" + "=" * 78)
    print(f"{task['id']}: witnesses are not decisive (margin {d.get('margin')}, tau {d['tau']})")
    print(prompt.rstrip())
    print("-" * 78)
    print("input:", info["args_repr"][:1000])
    print(f"preconditions accepting it: {d['pre_accept']}/{d['pre_total']}")
    for i, g in enumerate(info["groups"]):
        print(f"  [{i}] {g['repr']}    <- {', '.join(g['members'])}")
    for w in info["witnesses"]:
        if w["kind"] != "ok":
            print(f"  (no vote) {w['name']}: {w['kind']} {w['repr']}")
    for c in info["cand"]:
        print(f"  program under test {c['id']}: {c['repr']}")
    while True:
        a = input("which output is correct? number, 'r' = invalid input, 's' = skip: ").strip().lower()
        if a == "r":
            return {"reject": True}
        if a == "s":
            return None
        if a.isdigit() and int(a) < len(info["groups"]):
            return {"y_star": info["groups"][int(a)]["repr"]}


def vote(tasks: list[dict], rows: list[dict], mutants: list[dict], tau: float = 0.5,
         labels: dict | None = None, interactive: bool = True, timeout: float = 120,
         workers: int = MAX_CONCURRENT) -> list[dict]:
    """-> one record per distinct (task, failing input), with the decision, y* and per-candidate verdicts."""
    labels = labels if labels is not None else {}
    tmap = {t["id"]: t for t in tasks}
    src = dict(private.load_canonical())   # trusted code only: agents never get this
    prompt_of = {}
    for m in mutants:
        prompt_of[m["task_id"]] = m["prompt"]
        src[m["id"]] = m["prompt"] + m["mutated_solution"]

    # The unit voted on is a *call*: an input the program was actually run on inside the failing property run
    # (a metamorphic property calls the program on inputs it derives from its own arguments).
    inputs = defaultdict(lambda: {"cands": set(), "props": defaultdict(set)})
    failed = [r for r in rows if r["status"] == "failed" and (r.get("calls") or r.get("input_repr"))]
    for r in failed:
        for rep in _calls(r):
            e = inputs[(r["task_id"], rep)]
            e["cands"].add(r["candidate"])
            e["props"][r["candidate"]].add(f"{r['propset']}/{r['property']}")

    def probe(item):
        (tid, rep), e = item
        t = tmap[tid]
        data = {"entry_point": t["entry_point"], "args_repr": rep, "call_timeout": 2.0,
                "propsets": [{k: p[k] for k in ("name", "precondition")} for p in t["propertySets"]],
                "witnesses": [{"name": w["name"], "code": w["code"]} for w in t.get("implementations") or []],
                "candidates": [{"id": c, "code": src[c]} for c in sorted(e["cands"])]}
        res = run_code(_SCRIPT.replace("__DATA__", repr(json.dumps(data))), timeout=timeout)
        try:
            info = json.loads(res["stdout"].strip().splitlines()[-1])
        except (IndexError, ValueError):
            return item, None
        info["args_repr"] = rep
        return item, info

    items = [(k, v) for k, v in inputs.items() if k[0] in tmap and all(c in src for c in v["cands"])]
    with ThreadPoolExecutor(max(1, workers)) as ex:
        probed = list(ex.map(probe, items))

    out = []
    for (item, info) in probed:
        (tid, rep), e = item
        rec = {"type": "call", "task_id": tid, "input": rep[:300], "input_repr": rep, "tau": tau,
               "candidates": sorted(e["cands"])}
        if info is None:
            out.append({**rec, "decision": "error", "y_star": None}); continue
        d = decide(info, tau)
        rec.update({k: d[k] for k in ("pre_accept", "pre_total") if k in d}, margin=d.get("margin"))
        rec["witness_outputs"] = {g["repr"]: g["members"] for g in info["groups"]}
        y = None
        if d["decision"] == "rejected":
            rec["decision"] = "rejected"
        elif d["decision"] == "witnesses":
            rec["decision"], y = "accepted_by_witnesses", info["groups"][d["group"]]["repr"]
        else:
            lab = labels.get(_key(tid, rep))
            if lab is None and interactive:
                lab = _ask(tmap[tid], prompt_of.get(tid, ""), info, {**d, "pre_accept": d["pre_accept"]})
                if lab is not None:
                    labels[_key(tid, rep)] = lab
            if lab is None:
                rec["decision"] = "pending"
            elif lab.get("reject"):
                rec["decision"] = "rejected_by_user"
            else:
                rec["decision"], y = "accepted_by_user", lab["y_star"]
        rec["y_star"] = y
        verdicts = {}
        for c in info["cand"]:
            if rec["decision"] in ("rejected", "rejected_by_user"):
                v = "dismissed"
            elif y is None:
                v = "unresolved"
            else:
                gi = next((i for i, g in enumerate(info["groups"]) if g["repr"] == y), None)
                v = "agrees" if gi is not None and c["group"] == gi else "bug"
            verdicts[c["id"]] = {"verdict": v, "output": c["repr"], "properties": sorted(e["props"][c["id"]])}
        rec["verdicts"] = verdicts
        out.append(rec)
    # One verdict per failing property run, from the verdicts of the calls it made:
    #   bug             some call returned something other than y*            -> the program is wrong
    #   unresolved      no bug found but some call has no y*
    #   dismissed       no bug found but some call was an invalid input        -> the property built an invalid input
    #   property_wrong  every call was valid and returned y*, yet the property raised -> the property is wrong
    by_call = {(r["task_id"], r["input_repr"]): r for r in out if r.get("verdicts")}
    for r in failed:
        if r["task_id"] not in tmap:
            continue
        calls = []
        for rep in _calls(r):
            v = (by_call.get((r["task_id"], rep)) or {}).get("verdicts", {}).get(r["candidate"])
            calls.append({"input": rep[:300], "verdict": v["verdict"] if v else "unresolved"})
        kinds = {c["verdict"] for c in calls}
        verdict = ("bug" if "bug" in kinds else "unresolved" if "unresolved" in kinds or not kinds
                   else "dismissed" if "dismissed" in kinds else "property_wrong")
        out.append({"type": "row", "task_id": r["task_id"], "candidate": r["candidate"], "propset": r["propset"],
                    "property": r["property"], "input_repr": r.get("input_repr"), "verdict": verdict, "calls": calls})
    return out


def _calls(row: dict) -> list[str]:
    """The program inputs of a failing row (rows from before call logging fall back to the property's arguments)."""
    return list(dict.fromkeys(row.get("calls") or ([row["input_repr"]] if row.get("input_repr") else [])))


def summarize(recs: list[dict]) -> str:
    calls = [r for r in recs if r.get("type", "call") == "call"]
    rows = [r for r in recs if r.get("type") == "row"]
    lines = [f"distinct failing calls (program inputs): {len(calls)}"]
    by = defaultdict(int)
    for r in calls:
        by[r["decision"]] += 1
    lines.append("decisions: " + ", ".join(f"{k}={v}" for k, v in sorted(by.items())))
    lines.append(f"failing property runs: {len(rows)}")
    for k in ("bug", "property_wrong", "dismissed", "unresolved"):
        sel = [r for r in rows if r["verdict"] == k]
        if sel:
            names = sorted({r["candidate"] if k == "bug" else f"{r['task_id']}::{r['property']}" for r in sel})
            what = "programs" if k == "bug" else "properties" if k == "property_wrong" else "candidate/property pairs"
            lines.append(f"  {k}: {len(sel)} runs; {len(names)} {what}, e.g. {', '.join(names[:8])}")
    lines.append(f"candidates with a confirmed bug: {len({r['candidate'] for r in rows if r['verdict'] == 'bug'})}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("witnesses", help="workflow output JSON (list of tasks)")
    ap.add_argument("rows", help="harness --out JSONL")
    ap.add_argument("--mutants", default="data/humanevalplus_mutants.jsonl")
    ap.add_argument("--tau", type=float, default=0.5, help="required lead of the top witness output over the runner-up")
    ap.add_argument("--labels", default="data/labels.json", help="saved human answers (read and updated)")
    ap.add_argument("--out", help="write one JSON record per failing input here")
    ap.add_argument("--no-ask", action="store_true", help="never prompt; undecided inputs stay pending")
    a = ap.parse_args()
    tasks = json.loads(Path(a.witnesses).read_text())
    rows = [json.loads(l) for l in Path(a.rows).read_text().splitlines() if l.strip()]
    mutants = [json.loads(l) for l in Path(a.mutants).read_text().splitlines() if l.strip()]
    lp = Path(a.labels)
    labels = json.loads(lp.read_text()) if lp.exists() else {}
    try:
        recs = vote(tasks, rows, mutants, a.tau, labels, interactive=not a.no_ask and sys.stdin.isatty())
    except SandboxUnavailable as e:
        sys.exit(f"sandbox unavailable: {e}")
    if labels:
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text(json.dumps(labels, indent=1))
    if a.out:
        Path(a.out).write_text("".join(json.dumps(r) + "\n" for r in recs))
    print(summarize(recs))
    pend = [r for r in recs if r["decision"] == "pending"]
    if pend:
        print(f"\n{len(pend)} inputs need a human answer: rerun in a terminal, or add them to {a.labels} as "
              '{"<task_id>|<input_repr>": {"y_star": "<one of the witness output reprs>"} or {"reject": true}}')


if __name__ == "__main__":
    main()

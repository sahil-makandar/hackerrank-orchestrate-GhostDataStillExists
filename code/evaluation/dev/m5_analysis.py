"""Is the agent investigating, or just accepting the candidate list? (reads a run's transcripts; no model calls)

Tuning cases only. Reports:
  * claims beyond the candidate list (records the pre-pass never offered, linked by the agent's own evidence);
  * refusals overturned by evidence (timing-ambiguous candidates the agent claimed with identifier evidence);
  * wrong links among those new claims (record not in the case's answer, or carrying only other people's identifiers);
    ZERO-WRONG GATE on new claims;
  * state calls that differ from the deterministic reader (the agent no longer sees its hints);
  * recall of answer steps, agent vs baseline, on all cases and on agent-answered cases;
  * every disagreement with the baseline and who matches the answer;
  * how many tool calls went to independent reading (search_evidence, read_evidence, activity).

  python code/evaluation/m5_analysis.py --run-id m5_t3
"""
import argparse
import collections
import csv
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("GHOST_CACHE_ONLY", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import baseline                       # noqa: E402
import split                          # noqa: E402
import state                          # noqa: E402
from agent.tools import ToolBox       # noqa: E402
from ingest.common import DATASET     # noqa: E402

RUNS = Path(__file__).resolve().parents[2] / "cache" / "agent"
INDEPENDENT = {"search_evidence", "read_evidence", "activity"}


def ids(steps):
    return {(s["phase"], s["operation"], s["resource_id"]) for s in steps}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    a = ap.parse_args()
    tuning = set(split.tuning_cases())
    ref = {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"]))
           for r in csv.DictReader(open(DATASET / "sample_output.csv", encoding="utf-8", newline="")) if r["case_id"] in tuning}
    ctx = baseline.Context()
    tot = collections.Counter()
    new_claims, overrules, disagreements, rows = [], [], [], []
    recall = {"agent_all": [0, 0], "baseline_all": [0, 0], "agent_answered": [0, 0], "baseline_on_those": [0, 0]}
    for f in sorted((RUNS / a.run_id).glob("case_*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        cid = d["case_id"]
        if cid not in tuning:
            continue
        gold = ids(ref[cid][1])
        for k, steps in (("agent_all", d["steps"]), ("baseline_all", d["baseline"]["steps"])):
            recall[k][0] += len(ids(steps) & gold)
            recall[k][1] += len(gold)
        tools = collections.Counter(t for turn in d["transcript"] for t in turn["tools"])
        tot["tool_calls"] += sum(tools.values())
        tot["independent_calls"] += sum(v for k, v in tools.items() if k in INDEPENDENT)
        if d["source"] != "agent":
            continue
        tot["agent_cases"] += 1
        for k, steps in (("agent_answered", d["steps"]), ("baseline_on_those", d["baseline"]["steps"])):
            recall[k][0] += len(ids(steps) & gold)
            recall[k][1] += len(gold)
        p = d["payload"] or {}
        if d["status"] != d["baseline"]["status"] or ids(d["steps"]) != ids(d["baseline"]["steps"]):
            gs = ref[cid][0]
            disagreements.append((cid, d["status"], d["baseline"]["status"], gs,
                                  len(ids(d["steps"]) & gold), len(ids(d["steps"]) - gold),
                                  len(ids(d["baseline"]["steps"]) & gold), len(ids(d["baseline"]["steps"]) - gold),
                                  (p.get("notes") or "")[:220]))
        if not p.get("authorises_deletion", True):
            continue
        case = ctx.by_id[cid]
        tb = ToolBox(ctx, case)
        user = p.get("subject_user_id")
        cands = tb.candidates_for(user)
        claimable = {r for r, c in cands.items() if c.tier != "timing-ambiguous"}
        ambiguous = {r for r, c in cands.items() if c.tier == "timing-ambiguous"}
        claimed = {r["resource_id"] for r in p.get("records", [])}
        refused = {r.get("resource_id") for r in p.get("refused_candidates") or []}
        gold_res = {s["resource_id"] for s in ref[cid][1]}
        for rid in sorted((claimed - set(cands)) | (claimed & ambiguous)):
            kind = "beyond the candidate list" if rid not in cands else "overturned refusal (was ambiguous)"
            foreign = ctx.owners.foreign(rid, case["_audit_at"], user)
            in_answer = rid in gold_res
            new_claims.append((cid, rid, kind, "in answer" if in_answer else "NOT in answer",
                               "carries other people's identifiers" if foreign else ""))
        for r in p.get("records", []):
            rs = state.readings(ctx.index, r["resource_id"], tb.audit_at, ctx.clock, tb.subject_files(user))
            hint = state.current(rs, None if r.get("data_category") in ("SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD") else tb.requested)
            if hint and hint.kind != r["state_at_audit"] and not (hint.kind == "PRESENT" and r["state_at_audit"] == "IDENTIFIABLE"):
                overrules.append((cid, r["resource_id"], f"reader {hint.kind}", f"agent {r['state_at_audit']}"))
        tot["claimable_offered"] += len(claimable)
        tot["ambiguous_offered"] += len(ambiguous)
        tot["claimed"] += len(claimed)
        tot["claimed_from_claimable"] += len(claimed & claimable)
        tot["claimable_not_claimed"] += len(claimable - claimed)
        tot["refused_ambiguous"] += len(refused & ambiguous)
        tot["refused_claimable"] += len(refused & claimable)
        rows.append((cid, d["turns"], len(claimable), len(claimed & claimable), len(ambiguous), len(refused & ambiguous),
                     len(claimed - set(cands)), dict(tools)))

    wrong = [c for c in new_claims if c[3] == "NOT in answer" or c[4]]
    print(f"run {a.run_id}: {tot['agent_cases']} agent-answered of {len(tuning)} tuning cases")
    print(f"tool calls {tot['tool_calls']}; independent reading (search/read/activity) {tot['independent_calls']}")
    for k in ("claimable_offered", "claimed_from_claimable", "claimable_not_claimed", "refused_claimable",
              "ambiguous_offered", "refused_ambiguous", "claimed"):
        print(f"  {k:28s} {tot[k]}")
    print(f"\nclaims beyond the candidate list: {sum(1 for c in new_claims if c[2].startswith('beyond'))}")
    print(f"refusals overturned by evidence:  {sum(1 for c in new_claims if c[2].startswith('overturned'))}")
    for c in new_claims:
        print("  ", *c)
    print(f"wrong links among new claims:     {len(wrong)}")
    print(f"\nstate calls that differ from the deterministic reader: {len(overrules)}")
    for o in overrules[:12]:
        print("  ", *o)
    print("\nrecall of answer steps (phase, operation, resource):")
    for k, (hit, n) in recall.items():
        print(f"  {k:20s} {hit}/{n} = {hit / n:.3f}" if n else f"  {k:20s} -")
    print(f"\ndisagreements with the baseline (agent-answered): {len(disagreements)}")
    for cid, ast, bst, gs, ah, ax, bh, bx, notes in disagreements:
        who = "agent" if (ast == gs) > (bst == gs) or ((ast == gs) == (bst == gs) and (ah - ax) > (bh - bx)) else \
              "baseline" if (bst == gs) > (ast == gs) or (bh - bx) > (ah - ax) else "tie"
        print(f"  {cid}: answer {gs} | agent {ast} hit {ah} extra {ax} | baseline {bst} hit {bh} extra {bx} | closer: {who}")
        print(f"      agent notes: {notes}")
    print("\nper case: case, turns, claimable, claimed of those, ambiguous, refused ambiguous, beyond list, tools")
    for r in rows:
        print("  ", *r)
    print("\nNEW-CLAIM GATE (zero wrong links among claims beyond the pre-pass):", "PASS" if not wrong else "FAIL")
    return 0 if not wrong else 1


if __name__ == "__main__":
    sys.exit(main())

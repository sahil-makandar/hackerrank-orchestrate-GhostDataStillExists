"""Error analysis on the 20 TUNING cases: every missed and every wrong step vs the answers, grouped by cause.

  python code/evaluation/error_analysis.py --run-id b_t5d

Steps match by identity (phase, operation, system_id, resource_id); a matched step with another evidence_id is an
evidence error. Each unmatched step is attributed, per record, to one cause:
  account link        the answer's account is missing, or another account is used
  evidence choice     right step, other evidence_id (a different file, or none because the state could not be proven)
  state               the record is judged in the other compliance state (e.g. ABSENT where the answer reports it remaining)
  category            the record gets another treatment (e.g. DELETE where the answer redacts)
  missing record      a non-account record of the answer is absent from the row; split by why (no link, ambiguous link
                      refused, linked but left out) and by record type
  extra record        a record that is not in the answer at all
Cost = answer steps missed + wrong steps written (+1 per evidence mismatch). Ranked by cost.
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
from agent.tools import ToolBox       # noqa: E402
from ingest.common import DATASET     # noqa: E402
from policy import prefix_of, treatment_for_category   # noqa: E402

RUNS = Path(__file__).resolve().parents[2] / "cache" / "agent"
TREAT_OF_OP = {"DELETE_RECORD": "DELETE", "VERIFY_RECORD_ABSENT": "DELETE", "REDACT_RECORD": "REDACT",
               "VERIFY_RECORD_REDACTED": "REDACT", "ANONYMIZE_RECORD": "ANONYMIZE", "VERIFY_RECORD_ANONYMIZED": "ANONYMIZE",
               "VERIFY_RECORD_PRESENT": "RETAIN", "DISABLE_DATA_FLOW": "DISABLE", "VERIFY_DATA_FLOW_DISABLED": "DISABLE",
               "BLOCK_RESTORE": "BLOCK", "VERIFY_RESTORE_BLOCKED": "BLOCK"}


def key(s):
    return (s["phase"], s["operation"], s["system_id"], s["resource_id"])


def treatment(steps):
    t = {TREAT_OF_OP[s["operation"]] for s in steps if s["operation"] in TREAT_OF_OP}
    return next(iter(t)) if len(t) == 1 else None


def judged(ctx, d, cid):
    """resource_id -> (treatment, compliant) as the row's author judged it (agent payload, else the baseline)."""
    from baseline import COMPLIANT
    if d["source"] == "agent" and d.get("payload"):
        out = {}
        for r in d["payload"].get("records", []):
            treat = "DISABLE" if r["data_category"] == "DATA_FLOW" else treatment_for_category(r["data_category"])
            out[r["resource_id"]] = (treat, r["state_at_audit"] in COMPLIANT.get(treat, set()))
        return out
    return {i.resource_id: (i.treatment, i.compliant) for i in baseline.assess(ctx, ctx.by_id[cid]).get("items", [])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="b_t5d")
    a = ap.parse_args()
    ctx = baseline.Context()
    gold = {r["case_id"]: r for r in csv.DictReader(open(DATASET / "sample_output.csv", encoding="utf-8", newline=""))}
    cost, items = collections.Counter(), collections.defaultdict(list)
    status_err = []
    for cid in split.tuning_cases():
        d = json.loads((RUNS / a.run_id / f"{cid}.json").read_text(encoding="utf-8"))
        g_steps, p_steps = json.loads(gold[cid]["steps_json"]), d["steps"]
        if d["status"] != gold[cid]["deletion_status"]:
            status_err.append(f"{cid}: {d['status']} vs answer {gold[cid]['deletion_status']} ({d['source']}"
                              f"{', partial' if d.get('partial') else ''})")
        g_by, p_by = collections.defaultdict(list), collections.defaultdict(list)
        for s in g_steps:
            g_by[s["resource_id"]].append(s)
        for s in p_steps:
            p_by[s["resource_id"]].append(s)
        judg = judged(ctx, d, cid)
        cands = ToolBox(ctx, ctx.by_id[cid]).candidates_for(ctx.user[cid])
        for rid in sorted(set(g_by) | set(p_by)):
            gs, ps = g_by.get(rid, []), p_by.get(rid, [])
            gk, pk = {key(s): s for s in gs}, {key(s): s for s in ps}
            missed = [k for k in gk if k not in pk]
            wrong = [k for k in pk if k not in gk]
            ev = [k for k in gk if k in pk and gk[k]["evidence_id"] != pk[k]["evidence_id"]]
            for k in ev:
                c = ("evidence choice: none cited (state not provable, partial row)" if not pk[k]["evidence_id"]
                     else "evidence choice: another file cited")
                cost[c] += 1
                items[c].append(f"{cid} {rid} {k[1]}: {pk[k]['evidence_id'] or '(none)'} vs {gk[k]['evidence_id']}")
            if not missed and not wrong:
                continue
            n = len(missed) + len(wrong)
            tier = cands[rid].tier if rid in cands else "no link"
            if prefix_of(rid) == "account":
                c = ("account link: answer's account not in the row" if gs and not ps else
                     "account link: an account not in the answer" if ps and not gs else None)
                if c:
                    c += f" ({tier})"
                elif treatment(gs) != treatment(ps):
                    c = "category"
                else:
                    c = "state"
            elif gs and not ps:
                if rid in judg:
                    t, comp = judg[rid]
                    c = ("state: judged compliant, answer reports it" if comp else
                         "category" if t != treatment(gs) else "state")
                else:
                    why = {"timing-ambiguous": "ambiguous link refused", "no link": "no link to the person"}.get(
                        tier, "linked but left out")
                    c = f"missing record ({why}): {prefix_of(rid)}"
            elif ps and not gs:
                c = f"extra record not in the answer ({tier})"
            elif treatment(gs) != treatment(ps):
                c = "category"
            else:
                c = "state"
            cost[c] += n
            items[c].append(f"{cid} {rid}: missed {[k[1] for k in missed]} wrong {[k[1] for k in wrong]}")
    total = sum(cost.values())
    print(f"run {a.run_id}, 20 tuning cases: {total} step errors in all\n")
    print("| rank | cause | steps | share |\n|---:|---|---:|---:|")
    for i, (c, v) in enumerate(cost.most_common(), 1):
        print(f"| {i} | {c} | {v} | {v / total:.0%} |")
    print("\nstatus errors:", status_err or "none")
    for c, _ in cost.most_common():
        print(f"\n{c}:")
        for x in items[c]:
            print("   ", x)


if __name__ == "__main__":
    main()

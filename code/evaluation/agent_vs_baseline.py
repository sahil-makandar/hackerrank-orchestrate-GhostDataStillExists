"""Where did the agent change the answer? Tuning cases only, against the answers (no model calls).

  python code/evaluation/agent_vs_baseline.py --run-id b_t5e

For every tuning case whose submitted row (agent) differs from the deterministic baseline's row: status right or
wrong for each, full-tuple step F1 for each, the winner (status first, then F1), and the steps only one of them has,
marked as in the answer or not. The net effect compares the submitted rows with "baseline row everywhere".
"""
import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scoring                        # noqa: E402
import split                          # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "code" / "cache" / "agent"


def f1(steps, ref):
    p, r = {scoring.full_tuple(s) for s in steps}, {scoring.full_tuple(s) for s in ref}
    tp = len(p & r)
    return scoring.prf(tp, len(p), len(r))[2]


def short(k):
    return f"{k[1]} {k[3]}" + (f" [{k[5]}]" if k[5] else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="b_t5e")
    a = ap.parse_args()
    gold = {r["case_id"]: r for r in csv.DictReader(open(ROOT / "dataset" / "sample_output.csv", encoding="utf-8", newline=""))}
    sub, base, ref = {}, {}, {}
    rows = []
    for cid in split.tuning_cases():
        d = json.loads((RUNS / a.run_id / f"{cid}.json").read_text(encoding="utf-8"))
        g_status, g_steps = gold[cid]["deletion_status"], json.loads(gold[cid]["steps_json"])
        sub[cid], base[cid], ref[cid] = (d["status"], d["steps"]), (d["baseline"]["status"], d["baseline"]["steps"]), (g_status, g_steps)
        if (d["status"], d["steps"]) == (d["baseline"]["status"], d["baseline"]["steps"]):
            continue
        fa, fb = f1(d["steps"], g_steps), f1(d["baseline"]["steps"], g_steps)
        sa, sb = d["status"] == g_status, d["baseline"]["status"] == g_status
        winner = ("agent" if (sa, fa) > (sb, fb) else "baseline" if (sb, fb) > (sa, fa) else "tie")
        gk = {scoring.full_tuple(s) for s in g_steps}
        only_a = [scoring.full_tuple(s) for s in d["steps"] if scoring.full_tuple(s) not in {scoring.full_tuple(x) for x in d["baseline"]["steps"]}]
        only_b = [scoring.full_tuple(s) for s in d["baseline"]["steps"] if scoring.full_tuple(s) not in {scoring.full_tuple(x) for x in d["steps"]}]
        why = "; ".join([f"agent adds {short(k)} ({'in' if k in gk else 'NOT in'} answer)" for k in only_a] +
                        [f"baseline has {short(k)} ({'in' if k in gk else 'NOT in'} answer)" for k in only_b])
        rows.append((cid, d["source"], f"{d['status']}{' ok' if sa else ' WRONG'}", f"{d['baseline']['status']}{' ok' if sb else ' WRONG'}",
                     fa, fb, winner, why))
    print("| Case | Row source | Submitted status | Baseline status | Submitted F1 | Baseline F1 | Closer to the answer | What differs |")
    print("|---|---|---|---|---:|---:|---|---|")
    for r in rows:
        print(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]:.2f} | {r[5]:.2f} | {r[6]} | {r[7]} |")
    w = [r[6] for r in rows]
    print(f"\ncases that differ: {len(rows)}; agent closer {w.count('agent')}, baseline closer {w.count('baseline')}, tie {w.count('tie')}")
    print(scoring.format_score("SUBMITTED (agent rows)", scoring.score(sub, ref)))
    print(scoring.format_score("BASELINE everywhere   ", scoring.score(base, ref)))


if __name__ == "__main__":
    main()

"""Remaining errors on the 20 tuning cases by TYPE: wrong deletion_status, missing step, extra step, wrong step state,
wrong citation (no model calls).

  python code/evaluation/dev/error_types.py [--run-id b_t5g]

Steps match by identity (phase, operation, system_id, resource_id). Per case: status right/wrong; steps of the answer the
row lacks (missing); steps of the row the answer lacks (extra); matched steps whose state differs (wrong state); matched
steps whose evidence_id differs (wrong citation).
"""
import argparse
import collections
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import split  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]


def key(s):
    return (s["phase"], s["operation"], s["system_id"], s["resource_id"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default="b_t5g")
    a = ap.parse_args()
    gold = {r["case_id"]: r for r in csv.DictReader(open(ROOT / "dataset" / "sample_output.csv", encoding="utf-8", newline=""))}
    counts, detail = collections.Counter(), collections.defaultdict(list)
    for cid in split.tuning_cases():
        d = json.loads((ROOT / "code" / "cache" / "agent" / a.run_id / f"{cid}.json").read_text(encoding="utf-8"))
        g = {key(s): s for s in json.loads(gold[cid]["steps_json"])}
        p = {key(s): s for s in d["steps"]}
        if d["status"] != gold[cid]["deletion_status"]:
            counts["wrong deletion_status"] += 1
            detail["wrong deletion_status"].append(f"{cid}: {d['status']}{' (partial)' if d.get('partial') else ''} vs {gold[cid]['deletion_status']} [{d['source']}]")
        for k in g:
            if k not in p:
                counts["missing step"] += 1
                detail["missing step"].append(f"{cid}: {k[1]} {k[3]}")
            elif g[k]["state"] != p[k]["state"]:
                counts["wrong step state"] += 1
                detail["wrong step state"].append(f"{cid}: {k[1]} {k[3]}")
            elif g[k]["evidence_id"] != p[k]["evidence_id"]:
                counts["wrong citation"] += 1
                detail["wrong citation"].append(f"{cid}: {k[1]} {k[3]} cites {p[k]['evidence_id'] or '(none)'}, answer {g[k]['evidence_id'] or '(none)'}")
        for k in p:
            if k not in g:
                counts["extra step"] += 1
                detail["extra step"].append(f"{cid}: {k[1]} {k[3]}")
    total_g = sum(len(json.loads(r["steps_json"])) for c, r in gold.items() if c in set(split.tuning_cases()))
    print(f"run {a.run_id}: answer steps on the 20 tuning cases: {total_g}")
    for t in ("wrong deletion_status", "missing step", "extra step", "wrong step state", "wrong citation"):
        print(f"\n{t}: {counts[t]}")
        for x in detail[t]:
            print("   ", x)


if __name__ == "__main__":
    main()

"""Clock-rule evidence for decisions.md and the evaluation report.

1. Citation audit: no evidence_id cited in sample_output.csv may have been captured after its case's audit_at
   (file ids by manifest captured_at, activity ids by event_time, audit ids by event_time).
2. Score the two clock rules on the 20 tuning samples: two-clock (an observation's own time gates knowledge; the file's export
   time gates citation) versus strict (the export time gates knowledge too).

  python code/evaluation/clock_variants.py
"""
import csv
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("GHOST_CACHE_ONLY", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import linkage                       # noqa: E402
import m2_measure                    # noqa: E402
from ingest import build             # noqa: E402
from ingest.common import DATASET    # noqa: E402


def citation_audit():
    manifest = build.load_manifest()
    t_of = {fid: linkage.parse_time(r["captured_at"]) for fid, r in manifest.items()}
    for a in linkage.load_activity():
        t_of[a["activity_id"]] = a["_t"]
    with open(DATASET / "user_audit_log.csv", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            t_of[r["audit_id"]] = linkage.parse_time(r["event_time"])
    audit_at = {r["case_id"]: linkage.parse_time(r["audit_at"])
                for r in csv.DictReader(open(DATASET / "sample_deletion_requests.csv", encoding="utf-8", newline=""))}
    cited, bad, unknown = 0, [], []
    for r in csv.DictReader(open(DATASET / "sample_output.csv", encoding="utf-8", newline="")):
        for s in json.loads(r["steps_json"]):
            ev = s["evidence_id"]
            if not ev:
                continue
            cited += 1
            if ev not in t_of:
                unknown.append((r["case_id"], ev))
            elif t_of[ev] > audit_at[r["case_id"]]:
                bad.append((r["case_id"], ev, t_of[ev].isoformat(), audit_at[r["case_id"]].isoformat()))
    return cited, bad, unknown


def main():
    cited, bad, unknown = citation_audit()
    print(f"CITATION AUDIT: {cited} cited evidence ids in sample_output.csv; "
          f"{len(bad)} captured after audit_at; {len(unknown)} unknown ids")
    for b in bad[:5] + unknown[:5]:
        print("   ", b)
    ok = not bad and not unknown

    rows = {}
    for clock in ("two", "strict"):
        rows[clock] = m2_measure.run(clock, verbose=False)
    keys = ["gold", "gold_by_identifier", "gold_identifier_citable", "gold_by_timing_unique", "claimable",
            "cases_fully_claimable", "gold_by_timing_ambiguous", "gold_unlinked", "identifier_extra",
            "timing_unique_total", "timing_unique_wrong"]
    print("\nCLOCK VARIANTS on the 20 tuning samples (held-out cases are scored only at the end)")
    print(f"  {'metric':28s} {'two-clock':>10s} {'strict':>10s}")
    for k in keys:
        print(f"  {k:28s} {rows['two']['A'].get(k, 0):>10d} {rows['strict']['A'].get(k, 0):>10d}")
    wrong_b = {c: rows[c]["B"].get("timing-unique WRONG (identifier links it to another case)", 0) for c in rows}
    print(f"  {'label-free B: wrong unique':28s} {wrong_b['two']:>10d} {wrong_b['strict']:>10d}")
    print(f"  {'M2 gate':28s} {'PASS' if rows['two']['ok'] else 'FAIL':>10s} {'PASS' if rows['strict']['ok'] else 'FAIL':>10s}")
    ok &= rows["two"]["ok"]
    print("\nCLOCK GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

"""Timing precision at scale: how often does timing pick the right account when the identifier link is hidden?

  python code/evaluation/timing_precision.py

Label-free: the truth is the account an identifier link ties to the case. Cases: every request whose identifier links
tie it to exactly one account-delete event inside its own burst (the leave-one-anchor-out set of forced_gate.py B).
For each case:
  1. hide every identifier link of that case;
  2. refit the timing model WITHOUT that case's anchors (no leakage of its own answer into the learned ranges);
  3. candidates = account-delete events whose offset from the case's reported_at is inside the learned range and whose
     result matches the case's reported_status, known by audit_at, not owned by another case's identifier anchor, and
     whose account does not carry only other people's identifiers;
  4. best pick = the candidate whose offset is closest to the learned median offset.
Reported: coverage (a candidate exists), precision of the best pick, by number of candidates and by the case's row
status (output.csv, or sample_output.csv for samples). For every wrong pick, the step it would write: a DELETE_RECORD
of someone else's account (picked account still present at audit_at) or a VERIFY_RECORD_ABSENT (already gone).
Also the shipped tiers in the same hidden setting: timing-unique and constraint-forced claims, precision and coverage.
(The shipped tiers use the model fitted on all anchors, as forced_gate.py B does; the best pick uses the refit.)
"""
import collections
import csv
import json
import os
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault("GHOST_CACHE_ONLY", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import linkage                         # noqa: E402
import state                           # noqa: E402
from forced_gate import setup          # noqa: E402
from ingest import build               # noqa: E402
from ingest.common import DATASET      # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
ACC = ("DELETE_RECORD", "account_service")


def fit_medians(model, cases, ident, skip):
    """Refit `model` without case `skip`; return median offset per event key (same data the ranges are learned from)."""
    anchored = [(c, list(ident[c["case_id"]])) for c in cases if c["case_id"] != skip]
    model.fit(anchored)
    offs = collections.defaultdict(list)
    for case, rids in anchored:
        for rid in rids:
            for a in model.by_resource.get(rid, []):
                o = model.offset_h(a, case)
                if abs(o) <= model.MAX_OFFSET_H:
                    offs[model.key(a)].append(o)
    return {k: statistics.median(v) for k, v in offs.items()}


def main():
    t0 = time.time()
    cases, subj, ident, owners, model = setup()
    by_id = {c["case_id"]: c for c in cases}
    fm = linkage.ForcedMatcher(cases, model, ident, owners, subj)
    status = {r["case_id"]: r["deletion_status"] for f in (ROOT / "output.csv", DATASET / "sample_output.csv")
              for r in csv.DictReader(open(f, encoding="utf-8", newline=""))}
    idx = build.load()
    events = fm.events
    anchor_of = {e: cid for cid, evs in fm.anchor_events.items() for e in evs}

    study = []
    for cid, evs in fm.anchor_events.items():
        own = {e for e in evs if cid in model.owners(events[e])}
        if len(own) == 1:
            study.append((cid, events[next(iter(own))]["resource_id"]))
    print(f"anchored cases with exactly one own account-delete event: {len(study)}")

    rows = []
    for cid, truth in study:
        c = by_id[cid]
        lo_model = linkage.TimingModel(cases, model.events, subj)    # leave-one-out refit below
        med = fit_medians(lo_model, cases, ident, cid)
        cands = []
        for e, a in events.items():
            if anchor_of.get(e) not in (None, cid) or a["_t"] > c["_audit_at"]:
                continue                                       # owned by another case's identifier anchor / not yet known
            rng = lo_model.ranges.get(lo_model.key(a))
            if not rng or not rng[0] <= lo_model.offset_h(a, c) <= rng[1]:
                continue
            if lo_model.status_result.get(c["reported_status"]) not in (None, a["reported_result"]):
                continue
            if owners.foreign(a["resource_id"], c["_audit_at"], subj[cid]):
                continue
            cands.append((abs(lo_model.offset_h(a, c) - med.get(lo_model.key(a), 0.0)), a["resource_id"]))
        cands.sort()
        pick = cands[0][1] if cands else None
        gap = (cands[1][0] - cands[0][0]) if len(cands) > 1 else None
        harm = None
        if pick and pick != truth:
            rs = state.readings(idx, pick, c["_audit_at"], "two")
            cur = state.current(rs, c["_requested_at"])
            harm = ("DELETE_RECORD of another person's account" if cur and cur.kind in ("PRESENT", "IDENTIFIABLE")
                    else "VERIFY_RECORD_ABSENT of another person's account" if cur and cur.kind == "ABSENT"
                    else "no provable state: no step")
        # shipped tiers with the link hidden
        known = set(ident[cid]) - {truth}
        timed = model.timing_candidates(c, c["_audit_at"], known, owners, subj[cid])
        uniq = [r for r, x in timed.items() if r.startswith("account_") and x.tier == "timing-unique"]
        got, _ = fm.run(unanchor={cid})
        shipped = uniq[0] if len(uniq) == 1 else (got[cid].resource_id if cid in got else None)
        rows.append({"cid": cid, "n": len(cands), "pick": pick, "ok": pick == truth, "gap": gap, "harm": harm,
                     "status": status.get(cid, "?"), "shipped": shipped, "shipped_ok": shipped == truth})

    def line(label, rs):
        n = len(rs)
        cov = [r for r in rs if r["n"]]
        ok = sum(r["ok"] for r in cov)
        sh = [r for r in rs if r["shipped"]]
        return (f"| {label} | {n} | {len(cov)} ({len(cov) / n:.0%}) | {ok}/{len(cov)} ({ok / len(cov):.0%}) | "
                f"{len(sh)} ({len(sh) / n:.0%}) | {sum(r['shipped_ok'] for r in sh)}/{len(sh)} |" if cov and n else
                f"| {label} | {n} | 0 | – | {len(sh)} | {sum(r['shipped_ok'] for r in sh)}/{len(sh)} |")

    head = ("| cases | n | coverage (best pick exists) | best-pick precision | shipped tiers claim | shipped tiers correct |\n"
            "|---|---:|---:|---:|---:|---:|")
    print("\nBy number of competing candidates")
    print(head)
    print(line("all", rows))
    for k in (0, 1, 2, 3):
        print(line(f"{k} candidate(s)", [r for r in rows if r["n"] == k]))
    print(line("4+ candidates", [r for r in rows if r["n"] >= 4]))
    for st in ("COMPLETE", "INCOMPLETE"):
        sub = [r for r in rows if r["status"] == st]
        print(f"\nRow status {st}")
        print(head)
        print(line("all", sub))
        for k in (1, 2):
            print(line(f"{k} candidate(s)", [r for r in sub if r["n"] == k]))
        print(line("3+ candidates", [r for r in sub if r["n"] >= 3]))
    print("\nWrong best picks: the step they would write")
    for st in ("COMPLETE", "INCOMPLETE"):
        c = collections.Counter(r["harm"] for r in rows if r["status"] == st and r["n"] and not r["ok"])
        print(f"  {st}: {dict(c)}")
    print("\nWith 2+ candidates: precision by margin between the best and second-best candidate (hours from the median)")
    multi = [r for r in rows if r["n"] >= 2]
    for lo, hi in ((0, 0.05), (0.05, 0.2), (0.2, 0.5), (0.5, 99)):
        b = [r for r in multi if lo <= r["gap"] < hi]
        if b:
            print(f"  margin {lo}-{hi} h: {sum(r['ok'] for r in b)}/{len(b)} correct")
    # what the same rules would reach on the rows that have no account step today (no answer known: coverage only)
    out = {r["case_id"]: json.loads(r["steps_json"]) for r in csv.DictReader(open(ROOT / "output.csv", encoding="utf-8", newline=""))}
    gaps = [cid for cid, steps in out.items() if status.get(cid) != "NOT_APPLICABLE"
            and not any(s["system_id"] == "account_service" for s in steps)]
    med = fit_medians(linkage.TimingModel(cases, model.events, subj), cases, ident, None)
    reach = collections.Counter()
    for cid in gaps:
        c = by_id[cid]
        cands = sorted(
            (abs(model.offset_h(a, c) - med.get(model.key(a), 0.0)), a["resource_id"]) for e, a in events.items()
            if anchor_of.get(e) in (None, cid) and a["_t"] <= c["_audit_at"]
            and (rng := model.ranges.get(model.key(a))) and rng[0] <= model.offset_h(a, c) <= rng[1]
            and model.status_result.get(c["reported_status"]) in (None, a["reported_result"])
            and not owners.foreign(a["resource_id"], c["_audit_at"], subj[cid]))
        if not cands:
            reach["no candidate"] += 1
        elif len(cands) == 1:
            e = next(e for e, a in events.items() if a["resource_id"] == cands[0][1])
            rivals = {o for o in model.owners(events[e]) if o != cid and not fm.anchor_events.get(o)}
            reach["exactly 1 candidate, event no other unanchored case could own" if not rivals else
                  f"exactly 1 candidate, but {'1 other case' if len(rivals) == 1 else 'several cases'} could own that event"] += 1
        elif cands[1][0] - cands[0][0] >= 0.2:
            reach["2+ candidates, margin >= 0.2 h"] += 1
        else:
            reach["2+ candidates, margin < 0.2 h"] += 1
    print(f"\nRows in output.csv with no account step today: {len(gaps)}; what timing could reach (no answer known):")
    for k, v in sorted(reach.items()):
        print(f"  {k}: {v}")
    print(f"\n({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()

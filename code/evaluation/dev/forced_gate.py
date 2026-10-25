"""Gate for the constraint-forced tier (global one-to-one assignment of account-delete events).

A. 20 TUNING cases only (held-out cases are not scored here): every constraint-forced claim must be the account in
   the case's answer. Zero wrong, or the gate fails.
B. Label-free, all 550 requests: for every case anchored by an identifier, hide its anchor and rerun the matcher. A forced
   claim for that case must be the anchored account. Zero wrong, or the gate fails.

  python code/evaluation/forced_gate.py
"""
import collections
import csv
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("GHOST_CACHE_ONLY", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import linkage                         # noqa: E402
import split                           # noqa: E402
from identity import IdentityResolver  # noqa: E402
from ingest import build               # noqa: E402
from ingest.common import DATASET      # noqa: E402


def setup():
    idx, resolver, activity = build.load(), IdentityResolver(), linkage.load_activity()
    cases = linkage.load_requests("sample_deletion_requests.csv") + linkage.load_requests()
    subj = {c["case_id"]: resolver.resolve(c["email"])[0].user_id for c in cases}
    ident = {c["case_id"]: linkage.identifier_candidates(resolver.resolve(c["email"])[0], c["_audit_at"], idx, activity)
             for c in cases}
    owners = linkage.OwnerIndex(resolver, idx, activity)
    model = linkage.TimingModel(cases, activity, subj)
    model.fit([(c, list(ident[c["case_id"]])) for c in cases])
    return cases, subj, ident, owners, model


def main():
    t0 = time.time()
    cases, subj, ident, owners, model = setup()
    fm = linkage.ForcedMatcher(cases, model, ident, owners, subj)
    forced, log = fm.run()
    sizes = collections.Counter(len(cs) for cs, _, _ in log)
    skipped = [x for x in log if x[2].startswith("skipped")]
    print(f"components: {len(log)} (sizes {dict(sorted(sizes.items()))}); skipped for more cases than events: {len(skipped)} "
          f"({sum(len(cs) for cs, _, _ in skipped)} cases); constraint-forced claims: {len(forced)}")
    ok = True

    # A. tuning cases
    gold = {}
    for r in csv.DictReader(open(DATASET / "sample_output.csv", encoding="utf-8", newline="")):
        for s in json.loads(r["steps_json"]):
            if s["resource_id"].startswith("account_"):
                gold[r["case_id"]] = s["resource_id"]
    tuning = split.tuning_cases()
    by_id = {c["case_id"]: c for c in cases}
    already = {}
    for cid in tuning:
        c = by_id[cid]
        timed = model.timing_candidates(c, c["_audit_at"], set(ident[cid]), owners, subj[cid])
        already[cid] = {r for r in ident[cid] if r.startswith("account_")} | \
                       {r for r, x in timed.items() if r.startswith("account_") and x.tier == "timing-unique"}
    claims = {cid: forced[cid].resource_id for cid in tuning if cid in forced}
    wrong = {cid: (rid, gold.get(cid)) for cid, rid in claims.items() if rid != gold.get(cid)}
    new = [cid for cid, rid in claims.items() if rid == gold.get(cid) and rid not in already[cid]]
    before = sum(1 for cid in tuning if gold.get(cid) in already[cid])
    print(f"\nA. tuning ({len(tuning)} cases): forced claims {len(claims)}, wrong {len(wrong)}, new correct accounts {len(new)} {new}")
    print(f"   tuning cases whose answer account is claimable: before {before}/{len(tuning)}, with forced tier "
          f"{before + len(new)}/{len(tuning)}")
    for cid, (rid, g) in wrong.items():
        print(f"   WRONG {cid}: forced {rid}, answer {g}")
    ok &= not wrong

    # B. label-free leave-one-anchor-out
    res = collections.Counter()
    bad = []
    for cid, evs in fm.anchor_events.items():
        own = {e for e in evs if cid in model.owners(fm.events[e])}
        if len(own) != 1:
            continue                       # only cases anchored to exactly one event inside their own burst
        truth = fm.events[next(iter(own))]["resource_id"]
        got, _ = fm.run(unanchor={cid})
        if cid not in got:
            res["no claim"] += 1
        elif got[cid].resource_id == truth:
            res["forced = anchor"] += 1
        else:
            res["WRONG"] += 1
            bad.append((cid, got[cid].resource_id, truth))
    print(f"\nB. label-free leave-one-anchor-out over {sum(res.values())} anchored cases: {dict(res)}")
    for b in bad[:5]:
        print("   WRONG", b)
    ok &= res["WRONG"] == 0

    # C. timing-window tier (tight window + result match, same assignment rules): zero wrong on both tests
    wm = linkage.WindowMatcher(cases, model, ident, owners, subj)
    wclaims, _ = wm.run()
    wwrong = {cid: (c.resource_id, gold.get(cid)) for cid, c in wclaims.items() if cid in tuning and c.resource_id != gold.get(cid)}
    wtun = sum(1 for cid in tuning if cid in wclaims)
    print(f"\nC. timing-window tier, tuning cases: {wtun} claims, wrong {len(wwrong)} {wwrong or ''}")
    ok &= not wwrong
    wres, wbad = collections.Counter(), []
    for cid, evs in wm.anchor_events.items():
        own = {e for e in evs if cid in model.owners(wm.events[e])}
        if len(own) != 1:
            continue
        truth = wm.events[next(iter(own))]["resource_id"]
        got, _ = wm.run(unanchor={cid})
        if cid not in got:
            wres["no claim"] += 1
        elif got[cid].resource_id == truth:
            wres["window = anchor"] += 1
        else:
            wres["WRONG"] += 1
            wbad.append((cid, got[cid].resource_id, truth))
    print(f"   label-free leave-one-anchor-out over {sum(wres.values())} anchored cases: {dict(wres)}")
    for b in wbad[:5]:
        print("   WRONG", b)
    ok &= wres["WRONG"] == 0
    print(f"\n({time.time() - t0:.0f}s)\nFORCED GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

"""M2 measurement of the linkage pre-pass.

A. Against the 32 sample answers (the only labelled data): coverage of each tier and, crucially, how many
   timing-unique links are WRONG (resource not in that case's answer).
B. Self-supervised, on all 550 requests, no labels needed: for resources that carry an identifier link AND have
   deletion-worker events, ask what the timing rule alone would have said. A timing-unique link that points to a
   different case than the identifier link is a wrong link.

  python code/evaluation/m2_measure.py
Exit code 0 only if the gate holds: zero wrong timing-unique links in A and in B.
"""
import collections
import csv
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("GHOST_CACHE_ONLY", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import linkage                      # noqa: E402
from identity import IdentityResolver  # noqa: E402
from ingest import build            # noqa: E402
from ingest.common import DATASET   # noqa: E402

WRONG_B = "timing-unique WRONG (identifier links it to another case)"


def _gold():
    gold = collections.defaultdict(set)
    with open(DATASET / "sample_output.csv", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            for s in json.loads(r["steps_json"]):
                gold[r["case_id"]].add(s["resource_id"])
    return gold


def _measure_samples(samples, gold, ident_cands, model, owners, subj, out, subset="tuning"):
    tot = collections.Counter()
    wrong, missed, extra = [], [], []
    for c in samples:
        g = gold[c["case_id"]]
        ident = ident_cands[c["case_id"]]
        timing = model.timing_candidates(c, c["_audit_at"], set(ident), owners, subj[c["case_id"]])
        uniq = {r for r, x in timing.items() if x.tier == "timing-unique"}
        amb = {r for r, x in timing.items() if x.tier == "timing-ambiguous"}
        gap = g - set(ident) - uniq - amb
        tot["gold"] += len(g)
        tot["gold_by_identifier"] += len(g & set(ident))
        tot["gold_identifier_citable"] += len([r for r in g & set(ident) if ident[r].evidence_ids])
        tot["gold_by_timing_unique"] += len(g & uniq)
        tot["gold_by_timing_ambiguous"] += len(g & amb)
        tot["gold_unlinked"] += len(gap)
        tot["identifier_extra"] += len(set(ident) - g)
        tot["timing_unique_total"] += len(uniq)
        tot["timing_unique_wrong"] += len(uniq - g)
        tot["timing_ambiguous_total"] += len(amb)
        tot["cases_fully_claimable"] += g <= (set(ident) | uniq)
        wrong += [(c["case_id"], r, timing[r].reasons[:2]) for r in sorted(uniq - g)]
        missed += [(c["case_id"], r) for r in sorted(gap)]
        extra += [(c["case_id"], r, ident[r].reasons[:1]) for r in sorted(set(ident) - g)]
    tot["claimable"] = tot["gold_by_identifier"] + tot["gold_by_timing_unique"]
    out(f"\nA. {len(samples)} sample cases ({subset})")
    for k in ("gold", "gold_by_identifier", "gold_identifier_citable", "gold_by_timing_unique",
              "gold_by_timing_ambiguous", "gold_unlinked", "claimable", "cases_fully_claimable",
              "identifier_extra", "timing_unique_total", "timing_unique_wrong", "timing_ambiguous_total"):
        out(f"  {k:28s} {tot[k]}")
    if wrong:
        out("  WRONG timing-unique:", wrong[:8])
    if extra:
        out("  identifier-tier candidates not in the answer (the agent must reject these):", extra)
    if missed:
        out("  gold resources with no link at all:", missed[:14])
    return tot


def _measure_self_supervised(cases, ident_cands, model, owners, subj, out):
    res = collections.Counter()
    wrong = []
    for c in cases:
        ident = ident_cands[c["case_id"]]
        timing = model.timing_candidates(c, c["_audit_at"], set(), owners, subj[c["case_id"]])  # no identifier links assumed
        for rid in ident:
            if rid not in model.by_resource:
                continue
            x = timing.get(rid)
            res["identifier-linked resources with worker events"] += 1
            if x is None:
                res["timing says: no claim"] += 1
            elif x.tier == "timing-ambiguous":
                res["timing says: ambiguous"] += 1
            else:
                res["timing-unique and consistent"] += 1
        for rid, x in timing.items():
            if x.tier != "timing-unique":
                continue
            owner_cases = [k for k, v in ident_cands.items() if rid in v]
            if owner_cases and c["case_id"] not in owner_cases:
                res[WRONG_B] += 1
                wrong.append((c["case_id"], rid, owner_cases[:3]))
    out("\nB. self-supervised over", len(cases), "requests")
    for k, v in res.items():
        out(f"  {k:62s} {v}")
    if wrong:
        out("  examples:", wrong[:6])
    return res


def run(clock="two", verbose=True, subset="tuning"):
    """Measure the pre-pass under one clock rule ('two' = two-clock, 'strict' = file export time also gates knowledge)."""
    out = print if verbose else (lambda *a, **k: None)
    idx = build.load()
    resolver = IdentityResolver()
    activity = linkage.load_activity()
    samples = linkage.load_requests("sample_deletion_requests.csv")
    cases = samples + linkage.load_requests()
    if subset == "tuning":                     # held-out cases are scored only at the end
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import split
        keep = set(split.tuning_cases())
        samples = [c for c in samples if c["case_id"] in keep]
    subj = {c["case_id"]: resolver.resolve(c["email"])[0].user_id for c in cases}

    ident_cands = {c["case_id"]: linkage.identifier_candidates(resolver.resolve(c["email"])[0], c["_audit_at"], idx,
                                                               activity, clock) for c in cases}
    owners = linkage.OwnerIndex(resolver, idx, activity, clock)
    model = linkage.TimingModel(cases, activity, subj)
    model.fit([(c, list(ident_cands[c["case_id"]])) for c in cases])
    out("learned timing ranges (hours from reported_at) [lo, hi] n:")
    for k, (lo, hi, n) in sorted(model.ranges.items()):
        out(f"  {k[0]:20s} {k[1]:20s} {k[2]:8s} [{lo:+.2f}, {hi:+.2f}] n={n}")
    out("status -> account-delete result:", model.status_result)

    tot = _measure_samples(samples, _gold(), ident_cands, model, owners, subj, out, subset)
    res = _measure_self_supervised(cases, ident_cands, model, owners, subj, out)
    ok = tot["timing_unique_wrong"] == 0 and res[WRONG_B] == 0
    out("\nM2 GATE:", "PASS" if ok else "FAIL")
    return {"A": dict(tot), "B": dict(res), "ok": ok}


if __name__ == "__main__":
    sys.exit(0 if run()["ok"] else 1)

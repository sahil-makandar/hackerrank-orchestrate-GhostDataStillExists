"""Account linkage by the tight window, with and without burst anchoring, under a global one-to-one assignment.

  python code/evaluation/burst_linkage.py

Candidate events for a case: account DELETE_RECORD events by the deletion_worker, known by audit_at, whose result
matches the case's reported_status (COMPLETE->SUCCESS, PARTIALLY_COMPLETE->PARTIAL, NOT_STARTED->FAILED) and whose
offset from the case's reported_at is in [-1.4h, +0.2h]. Events of accounts tied by identifier to another case, and
accounts carrying only other people's identifiers, are excluded. No case_id order or range is used anywhere.

Burst anchoring (method B): a candidate event is kept only if the case's identifier-linked downstream deletion_worker
events sit where the label-free learned delta (downstream time - own account-delete time) puts them, within its 5-95%
range.

Assignment over all cases (connected components of the case-event graph):
  strict   a match is claimed only if it is in every maximum matching AND stays so when any one other case of the
           component is removed, and components with more cases than events are skipped (the shipped forced rules)
  greedy   a case whose candidate set is a single event no other remaining case needs, repeated to a fixed point
Scored on the 32 samples (answers; held-out access logged) and on the 247-case hidden-link test (label-free).
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

import linkage                         # noqa: E402
import split                           # noqa: E402
from forced_gate import setup          # noqa: E402
from ingest.common import DATASET      # noqa: E402

RESULT = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}
LO, HI = -1.4, 0.2
FM = linkage.ForcedMatcher


def hours(a, b):
    return (a - b).total_seconds() / 3600


class Graph:
    def __init__(self):
        self.cases, self.subj, self.ident, self.owners, self.model = setup()
        self.by_id = {c["case_id"]: c for c in self.cases}
        self.fm = FM(self.cases, self.model, self.ident, self.owners, self.subj)
        self.events = self.fm.events
        # label-free delta for burst anchoring, from anchored cases with exactly one own event
        deltas = []
        for cid, evs in self.fm.anchor_events.items():
            own = [self.events[e] for e in evs if cid in self.model.owners(self.events[e])]
            if len(own) == 1:
                for a in self._downstream(cid):
                    deltas.append(hours(a["_t"], own[0]["_t"]))
        deltas.sort()
        self.dlo, self.dhi = deltas[int(0.05 * len(deltas))], deltas[int(0.95 * len(deltas))]

    def _downstream(self, cid):
        c = self.by_id[cid]
        return [a for rid in self.ident[cid] if not rid.startswith("account_") for a in self.model.by_resource.get(rid, [])
                if -3 <= hours(a["_t"], c["_reported_at"]) <= 3 and a["_t"] <= c["_audit_at"]]

    def adjacency(self, anchored, burst):
        """case -> candidate events, for every case not in `anchored` (anchored cases keep their identifier event)."""
        taken = set().union(*[self.fm.anchor_events[c] for c in anchored]) if anchored else set()
        adj = {}
        for c in self.cases:
            cid = c["case_id"]
            if cid in anchored or c["reported_status"] not in RESULT:
                continue
            down = self._downstream(cid) if burst else []
            out = set()
            for e, a in self.events.items():
                if e in taken or a["_t"] > c["_audit_at"] or a["reported_result"] != RESULT[c["reported_status"]]:
                    continue
                if not LO <= hours(a["_t"], c["_reported_at"]) <= HI:
                    continue
                if self.owners.foreign(a["resource_id"], c["_audit_at"], self.subj.get(cid)):
                    continue
                if down and not all(self.dlo <= hours(d["_t"], a["_t"]) <= self.dhi for d in down):
                    continue
                out.add(e)
            if out:
                adj[cid] = out
        return adj

    def strict(self, adj):
        claims = {}
        for cs, es in FM.components(adj):
            sub = {c: adj[c] for c in cs}
            if len(cs) > len(es):
                continue
            _, match = FM.max_matching(cs, sub)
            for e, c in match.items():
                if self.fm.is_forced(cs, sub, c, e) and all(
                        self.fm.is_forced(cs - {x}, {k: v for k, v in sub.items() if k != x}, c, e) for x in cs - {c}):
                    claims[c] = e
        return claims

    @staticmethod
    def greedy(adj):
        adj = {c: set(v) for c, v in adj.items()}
        claims = {}
        changed = True
        while changed:
            changed = False
            need = collections.Counter(e for v in adj.values() for e in v)
            for c, v in list(adj.items()):
                if len(v) == 1 and need[next(iter(v))] == 1:
                    e = next(iter(v))
                    claims[c] = e
                    del adj[c]
                    for w in adj.values():
                        w.discard(e)
                    changed = True
        return claims


def main():
    g = Graph()
    print(f"burst-anchoring delta (downstream - own account delete), 5-95%: [{g.dlo:+.2f}, {g.dhi:+.2f}] h")
    anchored_all = {cid for cid, evs in g.fm.anchor_events.items() if evs}
    variants = [("A tight window", False), ("B tight window + burst anchoring", True)]

    # samples: real setting (identifier anchors in place)
    split.heldout_cases("measurement of the tight-window / burst account linkage on all 32 samples")
    gold = {}
    for r in csv.DictReader(open(DATASET / "sample_output.csv", encoding="utf-8", newline="")):
        for s in json.loads(r["steps_json"]):
            if s["resource_id"].startswith("account_"):
                gold[r["case_id"]] = s["resource_id"]
    samples = [c["case_id"] for c in linkage.load_requests("sample_deletion_requests.csv")]
    unanchored = [c for c in samples if not any(r.startswith("account_") for r in g.ident[c])]
    print(f"\nSAMPLES: {len(samples)} cases, {len(samples) - len(unanchored)} with an identifier account; "
          f"{len(unanchored)} without (scored below)")
    print("| method | assignment | claims | correct | wrong | coverage of the unanchored |\n|---|---|---:|---:|---:|---:|")
    for name, burst in variants:
        adj = g.adjacency(anchored_all, burst)
        for how in ("strict", "greedy"):
            cl = getattr(g, how)(adj)
            got = {c: g.events[cl[c]]["resource_id"] for c in unanchored if c in cl}
            ok = sum(got[c] == gold.get(c) for c in got)
            print(f"| {name} | {how} | {len(got)} | {ok} | {len(got) - ok} | {len(got)}/{len(unanchored)} |")
            for c in sorted(got, key=lambda x: g.by_id[x]["_reported_at"]):
                if got[c] != gold.get(c):
                    print(f"|   wrong: | {c} | {got[c]} | answer {gold.get(c)} | | |")

    # the shipped tiers on the same unanchored samples, for comparison
    forced, _ = g.fm.run()
    shipped = {}
    for c in unanchored:
        case = g.by_id[c]
        timed = g.model.timing_candidates(case, case["_audit_at"], set(g.ident[c]), g.owners, g.subj[c])
        uniq = [r for r, x in timed.items() if r.startswith("account_") and x.tier == "timing-unique"]
        if len(uniq) == 1:
            shipped[c] = uniq[0]
        elif c in forced:
            shipped[c] = forced[c].resource_id
    ok = sum(shipped[c] == gold.get(c) for c in shipped)
    print(f"| shipped tiers (timing-unique + constraint-forced) | – | {len(shipped)} | {ok} | {len(shipped) - ok} | "
          f"{len(shipped)}/{len(unanchored)} |")
    a_cl = g.strict(g.adjacency(anchored_all, False))
    a_got = {c: g.events[a_cl[c]]["resource_id"] for c in unanchored if c in a_cl}
    new = sorted(c for c in a_got if c not in shipped)
    lost = sorted(c for c in shipped if c not in a_got)
    print(f"samples A-strict adds over the shipped tiers: {new}; shipped claims A-strict does not make: {lost}")
    union = {**shipped, **a_got}
    ok = sum(union[c] == gold.get(c) for c in union)
    conflicts = sum(1 for c in union if c in shipped and c in a_got and shipped[c] != a_got[c])
    print(f"| A-strict, else shipped tier | – | {len(union)} | {ok} | {len(union) - ok} | {len(union)}/{len(unanchored)} | "
          f"(disagreements between the two: {conflicts})")

    # reach on output.csv rows with no account step (no answers: coverage only)
    rows = {r["case_id"]: r for r in csv.DictReader(open(Path(__file__).resolve().parents[3] / "output.csv",
                                                         encoding="utf-8", newline=""))}
    gaps = [c for c, r in rows.items() if r["deletion_status"] != "NOT_APPLICABLE"
            and not any(s["system_id"] == "account_service" for s in json.loads(r["steps_json"]))]
    cl = g.strict(g.adjacency(anchored_all, False))
    reach = [c for c in gaps if c in cl]
    print(f"\noutput.csv rows with no account step: {len(gaps)}; A-strict would give an account to {len(reach)}")

    # hidden-link test: 247 anchored cases, own link hidden one at a time
    study = []
    for cid, evs in g.fm.anchor_events.items():
        own = {e for e in evs if cid in g.model.owners(g.events[e])}
        if len(own) == 1:
            study.append((cid, g.events[next(iter(own))]["resource_id"]))
    print(f"\nHIDDEN-LINK TEST: {len(study)} cases")
    print("| method | assignment | claims | correct | wrong | precision | coverage |\n|---|---|---:|---:|---:|---:|---:|")
    for name, burst in variants:
        for how in ("strict", "greedy"):
            n = ok = 0
            for cid, truth in study:
                cl = getattr(g, how)(g.adjacency(anchored_all - {cid}, burst))
                if cid in cl:
                    n += 1
                    ok += g.events[cl[cid]]["resource_id"] == truth
            print(f"| {name} | {how} | {n} | {ok} | {n - ok} | {ok / n if n else 0:.1%} | {n}/{len(study)} ({n / len(study):.0%}) |")
    # union: A-strict, else the shipped tiers (timing-unique, constraint-forced), each with the link hidden
    n = ok = clash = 0
    for cid, truth in study:
        c = g.by_id[cid]
        cl = g.strict(g.adjacency(anchored_all - {cid}, False))
        a = g.events[cl[cid]]["resource_id"] if cid in cl else None
        timed = g.model.timing_candidates(c, c["_audit_at"], set(g.ident[cid]) - {truth}, g.owners, g.subj[cid])
        uniq = [r for r, x in timed.items() if r.startswith("account_") and x.tier == "timing-unique"]
        got, _ = g.fm.run(unanchor={cid})
        s = uniq[0] if len(uniq) == 1 else (got[cid].resource_id if cid in got else None)
        pick = a or s
        clash += bool(a and s and a != s)
        if pick:
            n += 1
            ok += pick == truth
    print(f"| A-strict, else shipped tiers | – | {n} | {ok} | {n - ok} | {ok / n if n else 0:.1%} | {n}/{len(study)} "
          f"({n / len(study):.0%}) |  disagreements: {clash}")


if __name__ == "__main__":
    main()

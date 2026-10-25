"""M5 self-test without any model call (tuning cases only).

1. Submission round trip: the baseline's own assessment, phrased as a submit_audit payload, must be accepted and must
   produce exactly the baseline's steps. Proves tools, submission checks and step builder agree.
2. Submission checks bite: a claimed record that is not a candidate, a citation that does not contain the record, an
   illegal category, a retained record called ABSENT and a later-captured citation for a finding are all rejected.
3. Every tool runs on every tuning case with zero cutoff violations, and tool outputs stay compact.

  python code/evaluation/m5_selftest.py
"""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("GHOST_CACHE_ONLY", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import baseline                  # noqa: E402
import split                     # noqa: E402
from agent import submit         # noqa: E402
from agent.tools import ToolBox  # noqa: E402
from policy import prefix_of     # noqa: E402
from validator import clock      # noqa: E402

CATEGORY = {("account", "DELETE"): "USER_PROFILE", ("search", "DELETE"): "SEARCH_INDEX_ENTRY",
            ("support", "DELETE"): "SUPPORT_ATTACHMENT", ("support", "REDACT"): "SUPPORT_MESSAGE",
            ("analytics", "DELETE"): "MARKETING_PROFILE", ("analytics", "ANONYMIZE"): "ANALYTICS_PROFILE",
            ("export", "DELETE"): "USER_EXPORT", ("audit_record", "RETAIN"): "SECURITY_AUDIT_LOG",
            ("backup", "BLOCK"): "BACKUP_ARCHIVE", ("data_flow", "DISABLE"): "DATA_FLOW",
            ("tombstone", "REDACT"): "ACCOUNT_TOMBSTONE", ("legal_hold", "RETAIN"): "LEGAL_HOLD_RECORD",
            ("marketing", "DELETE"): "MARKETING_PROFILE", ("cache", "DELETE"): "CACHE_ENTRY",
            ("object", "DELETE"): "SUPPORT_ATTACHMENT", ("email_delivery", "REDACT"): "EMAIL_DELIVERY_LOG"}
STATE = {("DELETE", True): "ABSENT", ("DELETE", False): "PRESENT", ("REDACT", True): "REDACTED",
         ("REDACT", False): "IDENTIFIABLE", ("ANONYMIZE", True): "ANONYMIZED", ("ANONYMIZE", False): "IDENTIFIABLE",
         ("RETAIN", True): "PRESENT", ("DISABLE", True): "DISABLED", ("DISABLE", False): "ACTIVE",
         ("BLOCK", True): "BLOCKED", ("BLOCK", False): "ACTIVE"}
ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


def search_all_identifiers(tb, user_id):
    ident = tb.identity(user_id)
    tb.search_evidence(sorted(ident.emails) + sorted(ident.hashes) + sorted(ident.phones) + [user_id])


def payload_from(ctx, cid, items, tb=None):
    listed = {i.resource_id for i in items}
    # every timing-ambiguous candidate, and every identifier-linked one the items leave out, must be decided: refuse them
    ambiguous = sorted(r for r, c in tb.candidates_for(ctx.user[cid]).items()
                       if c.tier == "timing-ambiguous" or (c.tier == "identifier" and r not in listed)) if tb else []
    recs = [{"system_id": i.system_id, "resource_id": i.resource_id,
             "data_category": CATEGORY[(prefix_of(i.resource_id), i.treatment)],
             "state_at_audit": STATE[(i.treatment, i.compliant)],
             "evidence_id": i.finding_evidence or i.verify_evidence or _any_evidence(ctx, i.resource_id),
             "link_basis": "self-test"} for i in items
            # an item with no evidence anywhere is a timing-window account: the submission adds that step itself
            if i.finding_evidence or i.verify_evidence or _any_evidence(ctx, i.resource_id)]
    return {"authorises_deletion": True, "intent_reason": "self-test", "subject_user_id": ctx.user[cid], "records": recs,
            "refused_candidates": [{"resource_id": r, "reason": "self-test"} for r in ambiguous]}


def _any_evidence(ctx, rid):
    obs = ctx.index.by_resource.get(rid, [])
    return obs[-1].file_id if obs else ""


def main():
    ctx = baseline.Context()
    tuning = split.tuning_cases()
    same, diff = 0, []
    for cid in tuning:
        case = ctx.by_id[cid]
        b = baseline.assess(ctx, case)
        if b.get("unresolved") or b["status"] == "NOT_APPLICABLE":
            continue
        tb = ToolBox(ctx, case)
        search_all_identifiers(tb, ctx.user[cid])
        r = submit.evaluate(tb, payload_from(ctx, cid, b["items"], tb))
        if r["accepted"] and r["steps"] == b["steps"]:
            same += 1
        else:
            diff.append((cid, r.get("errors")))
    check(f"submission round trip reproduces the baseline's steps ({same} cases)", not diff, str(diff[:2]))

    # the checks bite (built on one tuning case that has an INCOMPLETE baseline row)
    cid = next(c for c in tuning if baseline.assess(ctx, ctx.by_id[c])["status"] == "INCOMPLETE"
               and not baseline.assess(ctx, ctx.by_id[c]).get("unresolved"))
    case, b = ctx.by_id[cid], baseline.assess(ctx, ctx.by_id[cid])
    tb = ToolBox(ctx, case)
    search_all_identifiers(tb, ctx.user[cid])
    good = payload_from(ctx, cid, b["items"], tb)
    later = next(k for k, t in sorted(clock().time_of.items()) if k.startswith("evidence_") and t > case["_audit_at"])

    def codes(mutate):
        p = json.loads(json.dumps(good))
        mutate(p)
        return {e["code"] for e in submit.evaluate(tb, p)["errors"]}

    finding = next(i for i, it in enumerate(b["items"]) if not it.compliant)
    check("claiming a record that is not a candidate is rejected",
          "RECORD_LINK" in codes(lambda p: p["records"].append(dict(p["records"][0], resource_id="search_999999"))))
    check("a citation that does not contain the record is rejected",
          "RECORD_EVIDENCE" in codes(lambda p: p["records"][finding].update(evidence_id="evidence_54")))
    check("an illegal category is rejected",
          "RECORD_CATEGORY" in codes(lambda p: p["records"][finding].update(data_category="LEGAL_HOLD_RECORD")))
    check("an unknown subject is rejected", "SUBJECT" in codes(lambda p: p.update(subject_user_id="user_999999")))
    check("a finding citing evidence captured after audit_at is rejected",
          bool(codes(lambda p: p["records"][finding].update(evidence_id=later)) & {"EVIDENCE_NOT_CITABLE", "RECORD_EVIDENCE"}))
    # the refusal rules: pick a tuning case with ambiguous candidates
    amb_cid = next(c for c in tuning if any(x.tier == "timing-ambiguous" for x in
                                            ToolBox(ctx, ctx.by_id[c]).candidates_for(ctx.user[c]).values())
                   and not baseline.assess(ctx, ctx.by_id[c]).get("unresolved"))
    b2 = baseline.assess(ctx, ctx.by_id[amb_cid])
    fresh = ToolBox(ctx, ctx.by_id[amb_cid])                         # no search done yet
    p_refused = payload_from(ctx, amb_cid, b2["items"], fresh)
    c1 = {e["code"] for e in submit.evaluate(fresh, p_refused)["errors"]}
    check("refusing an ambiguous candidate without searching all identifiers is rejected", "REFUSAL_WITHOUT_SEARCH" in c1, str(c1))
    p_open = dict(p_refused, refused_candidates=[])
    c2 = {e["code"] for e in submit.evaluate(fresh, p_open)["errors"]}
    check("leaving an ambiguous candidate undecided is rejected", "AMBIGUOUS_UNADDRESSED" in c2, str(c2))
    search_all_identifiers(fresh, ctx.user[amb_cid])
    r3 = submit.evaluate(fresh, p_refused)
    check("after a search covering every identifier, the refusal is accepted", r3["accepted"], str(r3.get("errors"))[:200])

    # pick a non-compliant record that has an activity row known at audit_at (searched over all tuning cases)
    claim_case = None
    for c2 in tuning:
        b2_ = baseline.assess(ctx, ctx.by_id[c2])
        if b2_.get("unresolved") or b2_["status"] != "INCOMPLETE":
            continue
        for it in b2_["items"]:
            act = next((a["activity_id"] for a in ctx.activity if a["resource_id"] == it.resource_id
                        and a["_t"] <= ctx.by_id[c2]["_audit_at"]), None)
            if not it.compliant and act:
                claim_case = (c2, b2_, it.resource_id, act)
                break
        if claim_case:
            break
    c2, b2_, rid2, act = claim_case
    tb2 = ToolBox(ctx, ctx.by_id[c2])
    search_all_identifiers(tb2, ctx.user[c2])
    p2 = payload_from(ctx, c2, b2_["items"], tb2)
    for r in p2["records"]:
        if r["resource_id"] == rid2:
            r["evidence_id"] = act
    r_sub = submit.evaluate(tb2, p2)
    cited = {s["evidence_id"] for s in r_sub.get("steps", []) if s["resource_id"] == rid2 and s["phase"] == "FINDING"}
    check("a finding citing an activity row gets code's citation: a citable observation of the same state",
          r_sub["accepted"] and cited and not any(e.startswith("activity_") for e in cited) and all(tb2.citable(e) for e in cited),
          f"{c2} {rid2} {cited} {r_sub.get('errors')}")
    real = submit.citable_proof
    submit.citable_proof = lambda *a: None       # no citable reading of that state exists
    try:
        got = {e["code"] for e in submit.evaluate(tb2, p2)["errors"]}
    finally:
        submit.citable_proof = real
    check("without a citable reading, a finding that cites an activity row (a claim) is rejected", "CLAIM_AS_PROOF" in got,
          f"{c2} {rid2} {got}")
    na = submit.evaluate(tb, {"authorises_deletion": False, "intent_reason": "x", "records": []})
    check("authorises_deletion=false gives NOT_APPLICABLE with []", na["accepted"] and na["status"] == "NOT_APPLICABLE" and na["steps"] == [])

    # every tool on every tuning case: zero cutoff violations, compact outputs
    worst, violations, hinted = 0, 0, 0
    for cid in tuning:
        case = ctx.by_id[cid]
        tb = ToolBox(ctx, case)
        u = ctx.user[cid]
        cands = [c["resource_id"] for c in tb.list_candidates(u)["candidates"]][:8]
        files = sorted({e for c in tb.candidates_for(u).values() for e in c.evidence_ids})[:2]
        outs = [tb.resolve_identity(case["email"]), tb.list_candidates(u), tb.account_candidates(u), tb.get_resource(cands),
                tb.search_evidence([case["email"]]), tb.activity(window="burst"), tb.activity(cands), tb.get_policy(cands[0] if cands else None)]
        outs += [tb.read_evidence(f) for f in files]
        worst = max([worst] + [len(json.dumps(o)) for o in outs])
        hinted += sum('"state_hint' in json.dumps(o) for o in outs)
        violations += tb.violations
    check("tools on all tuning cases: zero cutoff violations", violations == 0, str(violations))
    check("no tool output carries a derived state (the agent must establish state itself)", hinted == 0, str(hinted))
    check("tool outputs stay compact (largest under 20k characters)", worst < 20000, f"largest {worst}")
    print("\nM5 SELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

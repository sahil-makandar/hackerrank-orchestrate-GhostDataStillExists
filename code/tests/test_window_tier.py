"""The timing-window account tier: claims are one-to-one, inside the window, result-matched, and verification only."""
import pytest

import baseline
import linkage
from policy import prefix_of


@pytest.fixture(scope="module")
def ctx():
    return baseline.Context()


def test_claims_are_one_to_one(ctx):
    events = [c.evidence_ids[0] for c in ctx.window.values()]
    assert events and len(events) == len(set(events))


def test_every_claim_is_in_the_window_and_matches_the_reported_status(ctx):
    events = {a["activity_id"]: a for a in ctx.timing.events}
    for cid, claim in ctx.window.items():
        case, a = ctx.by_id[cid], events[claim.evidence_ids[0]]
        assert claim.tier == "timing-window" and a["resource_id"] == claim.resource_id
        assert linkage.WindowMatcher.RESULT[case["reported_status"]] == a["reported_result"]
        offset = ctx.timing.offset_h(a, case)
        assert linkage.WindowMatcher.LO <= offset <= linkage.WindowMatcher.HI
        assert a["_t"] <= case["_audit_at"]


def test_identifier_anchored_cases_are_never_claimed_by_timing(ctx):
    for cid in ctx.window:
        assert not any(prefix_of(r) == "account" for r in ctx.ident[cid])


def test_a_window_account_is_verification_only(ctx):
    """Never a DELETE: the step added for a window account is a VERIFY_RECORD_ABSENT, or nothing."""
    for cid, claim in ctx.window.items():
        case = ctx.by_id[cid]
        if not baseline.authorises_deletion(case["request_text"]):
            continue
        ops = {s["operation"] for s in baseline.assess(ctx, case)["steps"] if s["resource_id"] == claim.resource_id}
        assert ops <= {"VERIFY_RECORD_ABSENT"}, (cid, ops)


def test_a_window_account_replaces_other_timing_accounts_of_its_case(ctx):
    for cid, claim in ctx.window.items():
        accounts = {r: c.tier for r, c in ctx.candidates(ctx.by_id[cid]).items() if r.startswith("account_")}
        assert accounts == {claim.resource_id: "timing-window"}


def test_margin_rule_claims_are_unique_and_clearly_ahead(ctx):
    """R5: a margin-rule claim beats the next candidate by at least MARGIN_H and no event is claimed twice."""
    margin_claims = {cid: c for cid, c in ctx.window.items() if "margin rule" in c.reasons[0]}
    assert margin_claims, "the margin rule makes claims on the evaluation requests"
    events = [c.evidence_ids[0] for c in ctx.window.values()]
    assert len(events) == len(set(events))
    for cid, claim in margin_claims.items():
        gap = float(claim.reasons[0].split("ahead of the next by ")[1].split(" h")[0])
        assert gap >= linkage.WindowMatcher.MARGIN_H

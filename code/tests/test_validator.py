"""The validator accepts every answer row and rejects rows that break the output contract."""
import copy

import pytest

import validator


def codes(errs):
    return {e["code"] for e in errs}


def test_every_sample_answer_row_is_valid(samples, answers):
    for cid, (status, steps) in answers.items():
        assert validator.validate(cid, status, steps, samples[cid]["_audit_at"]) == [], cid


@pytest.fixture
def incomplete_row(samples, answers):
    cid = next(c for c, (st, steps) in answers.items() if st == "INCOMPLETE" and len(steps) >= 3)
    return cid, copy.deepcopy(answers[cid][1]), samples[cid]["_audit_at"]


def test_gap_in_step_numbers_is_rejected(incomplete_row):
    cid, steps, audit_at = incomplete_row
    steps[1]["step_no"] = 7
    assert validator.validate(cid, "INCOMPLETE", steps, audit_at)


def test_phases_out_of_order_are_rejected(incomplete_row):
    cid, steps, audit_at = incomplete_row
    steps.reverse()
    for n, s in enumerate(steps, 1):
        s["step_no"] = n
    assert validator.validate(cid, "INCOMPLETE", steps, audit_at)


def test_remediation_must_not_cite_evidence(incomplete_row):
    cid, steps, audit_at = incomplete_row
    rem = next(s for s in steps if s["phase"] == "REMEDIATION")
    rem["evidence_id"] = next(s["evidence_id"] for s in steps if s["evidence_id"])
    assert validator.validate(cid, "INCOMPLETE", steps, audit_at)


def test_unknown_operation_is_rejected(incomplete_row):
    cid, steps, audit_at = incomplete_row
    steps[0]["operation"] = "PURGE_EVERYTHING"
    assert validator.validate(cid, "INCOMPLETE", steps, audit_at)


def test_not_applicable_must_have_no_steps(incomplete_row):
    cid, steps, audit_at = incomplete_row
    assert validator.validate(cid, "NOT_APPLICABLE", steps, audit_at)
    assert validator.validate(cid, "NOT_APPLICABLE", [], audit_at) == []


def test_complete_without_cited_verification_is_rejected(samples, answers):
    cid = next(c for c, (st, _) in answers.items() if st == "COMPLETE")
    steps = copy.deepcopy(answers[cid][1])
    steps[0]["evidence_id"] = ""
    assert validator.validate(cid, "COMPLETE", steps, samples[cid]["_audit_at"])

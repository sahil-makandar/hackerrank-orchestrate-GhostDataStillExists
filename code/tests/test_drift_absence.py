"""R1: absence from an account-table drift capture, with the guard (no reading of the account present after the request)."""
import pytest

import state
from ingest import build
from linkage import parse_time


@pytest.fixture(scope="module")
def index():
    return build.load()


def test_account_omitted_from_both_copies_is_absent_and_citable(index, samples):
    case = samples["case_01"]
    r = state.drift_absence(index, "account_001", case["_requested_at"], case["_audit_at"], [])
    assert r is not None and r.kind == "ABSENT" and r.file_id == "evidence_54" and r.citable
    assert r.time > case["_requested_at"] and r.time <= case["_audit_at"]


def test_guard_blocks_a_present_reading_after_the_request(index, samples):
    case = samples["case_01"]
    seen_present = state.Reading("PRESENT", case["_requested_at"], "evidence_x", True, "other")
    assert state.drift_absence(index, "account_001", case["_requested_at"], case["_audit_at"], [seen_present]) is None
    before = state.Reading("PRESENT", case["_requested_at"].replace(year=2020), "evidence_x", True, "other")
    assert state.drift_absence(index, "account_001", case["_requested_at"], case["_audit_at"], [before]) is not None


def test_an_account_listed_in_the_capture_is_not_absent(index, samples):
    case = samples["case_05"]
    assert state.drift_absence(index, "account_005", parse_time("2026-08-01T00:00:00Z"), case["_audit_at"], []) is None


def test_other_systems_and_captures_before_the_request_are_ignored(index, samples):
    case = samples["case_01"]
    assert state.drift_absence(index, "search_123", case["_requested_at"], case["_audit_at"], []) is None
    assert state.drift_absence(index, "account_001", case["_audit_at"], case["_audit_at"], []) is None

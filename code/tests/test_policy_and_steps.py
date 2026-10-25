"""Policy mapping (deletion_rules.csv -> treatment) and the step builder's order and numbering."""
import pytest

from policy import RULES, treatment_for_category
from steps import PHASES, Item, build_steps

EXPECTED = {
    "USER_PROFILE": "DELETE", "SUPPORT_MESSAGE": "REDACT", "SUPPORT_ATTACHMENT": "DELETE",
    "ANALYTICS_PROFILE": "ANONYMIZE", "MARKETING_PROFILE": "DELETE", "EMAIL_DELIVERY_LOG": "REDACT",
    "SECURITY_AUDIT_LOG": "RETAIN", "LEGAL_HOLD_RECORD": "RETAIN", "BACKUP_ARCHIVE": "BLOCK",
    "ACCOUNT_TOMBSTONE": "REDACT",
}


@pytest.mark.parametrize("category,treatment", sorted(EXPECTED.items()))
def test_category_maps_to_its_treatment(category, treatment):
    assert treatment_for_category(category) == treatment


def test_every_rule_category_has_a_known_treatment():
    assert {treatment_for_category(c) for c in RULES} <= {"DELETE", "REDACT", "ANONYMIZE", "RETAIN", "BLOCK"}


def _incomplete_items():
    return [
        Item("account_service", "account_1", "DELETE", False, finding_evidence="evidence_01", always_verify=True),
        Item("integration_service", "data_flow_1", "DISABLE", False, finding_evidence="evidence_02"),
        Item("backup_service", "backup_1", "BLOCK", False, finding_evidence="evidence_03"),
        Item("support_platform", "support_1", "REDACT", False, finding_evidence="evidence_04"),
        Item("security_service", "audit_record_1", "RETAIN", True, always_verify=True),
    ]


def test_incomplete_steps_are_ordered_by_phase_and_numbered_from_one():
    steps = build_steps("INCOMPLETE", _incomplete_items())
    assert [s["step_no"] for s in steps] == list(range(1, len(steps) + 1))
    phases = [PHASES.index(s["phase"]) for s in steps]
    assert phases == sorted(phases)
    assert all(not s["evidence_id"] for s in steps if s["phase"] != "FINDING")
    assert all(s["evidence_id"] for s in steps if s["phase"] == "FINDING")


def test_flows_and_restores_are_stopped_before_records_change():
    rem = [s["operation"] for s in build_steps("INCOMPLETE", _incomplete_items()) if s["phase"] == "REMEDIATION"]
    assert rem.index("DISABLE_DATA_FLOW") < rem.index("DELETE_RECORD")
    assert rem.index("BLOCK_RESTORE") < rem.index("DELETE_RECORD")
    assert rem.index("DELETE_RECORD") < rem.index("REDACT_RECORD")


def test_retained_record_is_verified_present_never_deleted():
    steps = build_steps("INCOMPLETE", _incomplete_items())
    ops = {s["operation"] for s in steps if s["resource_id"] == "audit_record_1"}
    assert ops == {"VERIFY_RECORD_PRESENT"}


def test_complete_case_has_only_cited_verification_steps():
    items = [Item("account_service", "account_1", "DELETE", True, verify_evidence="evidence_05", always_verify=True),
             Item("analytics_platform", "analytics_1", "ANONYMIZE", True, verify_evidence="evidence_06")]
    steps = build_steps("COMPLETE", items)
    assert steps and all(s["phase"] == "VERIFICATION" and s["evidence_id"] for s in steps)

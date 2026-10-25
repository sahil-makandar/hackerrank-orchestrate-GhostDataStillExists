"""Deletion policy and the output contract's operation tables.
It reads deletion_rules.csv and maps each category to its treatment and operations.

Everything here is derived from `dataset/deletion_rules.csv` and problem_statement.md; the per-record *choice* of
data category stays with the agent (or the baseline heuristics), this module only says what each category requires
and which operations are legal for which kind of resource.
"""
import csv

from ingest.common import DATASET

PHASES = ["FINDING", "REMEDIATION", "VERIFICATION"]

# Operation order inside each phase (problem statement table order).
OP_ORDER = {
    "FINDING": ["REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK"],
    "REMEDIATION": ["DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"],
    "VERIFICATION": ["VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED",
                     "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED"],
}
PHASE_OF = {op: ph for ph, ops in OP_ORDER.items() for op in ops}
OP_RANK = {op: i for ops in OP_ORDER.values() for i, op in enumerate(ops)}

STATE_OF = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT",
    "REPORT_ACTIVE_DATA_FLOW": "ACTIVE", "REPORT_RESTORE_RISK": "ACTIVE",
    "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED", "DELETE_RECORD": "ABSENT",
    "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT", "VERIFY_RECORD_REDACTED": "REDACTED",
    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
    "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}

# A treatment is what a record must receive; each maps to one finding / remediation / verification operation.
FINDING_FOR = {"DELETE": "REPORT_REMAINING_RECORD", "REDACT": "REPORT_EXPOSED_PII", "ANONYMIZE": "REPORT_EXPOSED_PII",
               "DISABLE": "REPORT_ACTIVE_DATA_FLOW", "BLOCK": "REPORT_RESTORE_RISK"}
REMEDIATION_FOR = {"DELETE": "DELETE_RECORD", "REDACT": "REDACT_RECORD", "ANONYMIZE": "ANONYMIZE_RECORD",
                   "DISABLE": "DISABLE_DATA_FLOW", "BLOCK": "BLOCK_RESTORE"}
VERIFICATION_FOR = {"DELETE": "VERIFY_RECORD_ABSENT", "RETAIN": "VERIFY_RECORD_PRESENT",
                    "REDACT": "VERIFY_RECORD_REDACTED", "ANONYMIZE": "VERIFY_RECORD_ANONYMIZED",
                    "DISABLE": "VERIFY_DATA_FLOW_DISABLED", "BLOCK": "VERIFY_RESTORE_BLOCKED"}
TREATMENT_OF_FINDING = {v: k for k, v in FINDING_FOR.items() if k not in ("ANONYMIZE",)}

# Which data categories can live behind each resource-id prefix (structure of the platform, not of any case).
PREFIX_CATEGORIES = {
    "account": ["USER_PROFILE"], "search": ["SEARCH_INDEX_ENTRY"], "cache": ["CACHE_ENTRY"],
    "support": ["SUPPORT_MESSAGE", "SUPPORT_ATTACHMENT"], "object": ["SUPPORT_ATTACHMENT"],
    "analytics": ["ANALYTICS_PROFILE", "PRODUCT_TELEMETRY", "MARKETING_PROFILE"],
    "export": ["USER_EXPORT"], "marketing": ["MARKETING_PROFILE"], "email_delivery": ["EMAIL_DELIVERY_LOG"],
    "audit_record": ["SECURITY_AUDIT_LOG"], "legal_hold": ["LEGAL_HOLD_RECORD"],
    "backup": ["BACKUP_ARCHIVE"], "tombstone": ["ACCOUNT_TOMBSTONE"],
}
# Data flows (integration connectors) are not a data category: the only treatment is to disable them.
FLOW_PREFIX = "data_flow"

_ACTION_TO_TREATMENT = {"DELETE": "DELETE", "REDACT": "REDACT", "ANONYMIZE": "ANONYMIZE", "RETAIN": "RETAIN"}


def load_rules():
    with open(DATASET / "deletion_rules.csv", encoding="utf-8", newline="") as f:
        return {r["data_category"]: r for r in csv.DictReader(f)}


RULES = load_rules()


def treatment_for_category(category):
    """Treatment required by deletion_rules.csv. BACKUP_ARCHIVE is retained *only if restore access can be blocked*."""
    action = RULES[category]["required_action"]
    if category == "BACKUP_ARCHIVE":
        return "BLOCK"
    return _ACTION_TO_TREATMENT[action]


def prefix_of(resource_id):
    for p in sorted(list(PREFIX_CATEGORIES) + [FLOW_PREFIX], key=len, reverse=True):
        if resource_id.startswith(p + "_"):
            return p
    return None


def legal_treatments(resource_id):
    """Treatments that may legally be applied to this kind of resource."""
    p = prefix_of(resource_id)
    if p == FLOW_PREFIX:
        return {"DISABLE"}
    return {treatment_for_category(c) for c in PREFIX_CATEGORIES.get(p, [])}

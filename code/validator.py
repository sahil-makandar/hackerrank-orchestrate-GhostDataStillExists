"""Validator: every rule of the output contract (problem_statement.md / AGENTS.md section 6) as coded errors.
It also checks the citation clock: nothing captured after audit_at is ever cited.

An error is {"code", "step_no", "message"}; the agent receives them as tool feedback and may retry.
HARD RULE enforced here: a cited evidence id must not have been captured / recorded after the case's audit_at.
"""
import csv
import json

from datetime import datetime

from ingest.common import DATASET, RESOURCE_RE, system_of
from policy import (OP_RANK, PHASE_OF, PHASES, STATE_OF, legal_treatments)

STATUSES = {"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"}
FIELDS = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")
# operation -> the treatment it implements (REPORT_EXPOSED_PII may mean REDACT or ANONYMIZE)
TREATMENT_OF_OP = {
    "REPORT_REMAINING_RECORD": {"DELETE"}, "REPORT_EXPOSED_PII": {"REDACT", "ANONYMIZE"},
    "REPORT_ACTIVE_DATA_FLOW": {"DISABLE"}, "REPORT_RESTORE_RISK": {"BLOCK"},
    "DISABLE_DATA_FLOW": {"DISABLE"}, "BLOCK_RESTORE": {"BLOCK"}, "DELETE_RECORD": {"DELETE"},
    "REDACT_RECORD": {"REDACT"}, "ANONYMIZE_RECORD": {"ANONYMIZE"},
    "VERIFY_RECORD_ABSENT": {"DELETE"}, "VERIFY_RECORD_PRESENT": {"RETAIN"}, "VERIFY_RECORD_REDACTED": {"REDACT"},
    "VERIFY_RECORD_ANONYMIZED": {"ANONYMIZE"}, "VERIFY_DATA_FLOW_DISABLED": {"DISABLE"},
    "VERIFY_RESTORE_BLOCKED": {"BLOCK"},
}
FINDING_TO_REMEDIATION = {"REPORT_REMAINING_RECORD": {"DELETE_RECORD"},
                          "REPORT_EXPOSED_PII": {"REDACT_RECORD", "ANONYMIZE_RECORD"},
                          "REPORT_ACTIVE_DATA_FLOW": {"DISABLE_DATA_FLOW"},
                          "REPORT_RESTORE_RISK": {"BLOCK_RESTORE"}}
REMEDIATION_TO_VERIFY = {"DELETE_RECORD": "VERIFY_RECORD_ABSENT", "REDACT_RECORD": "VERIFY_RECORD_REDACTED",
                         "ANONYMIZE_RECORD": "VERIFY_RECORD_ANONYMIZED", "DISABLE_DATA_FLOW": "VERIFY_DATA_FLOW_DISABLED",
                         "BLOCK_RESTORE": "VERIFY_RESTORE_BLOCKED"}


def _t(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class EvidenceClock:
    """id -> when the evidence was captured (file ids) or recorded (activity / audit ids)."""

    def __init__(self):
        self.time_of = {}
        with open(DATASET / "evidence_files.csv", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                self.time_of[r["file_id"]] = _t(r["captured_at"])
        with open(DATASET / "system_activity.csv", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                self.time_of[r["activity_id"]] = _t(r["event_time"])
        with open(DATASET / "user_audit_log.csv", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                self.time_of[r["audit_id"]] = _t(r["event_time"])


_CLOCK = None


def clock():
    global _CLOCK
    if _CLOCK is None:
        _CLOCK = EvidenceClock()
    return _CLOCK


def _err(code, step_no, message):
    return {"code": code, "step_no": step_no, "message": message}


def _parse(steps):
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except ValueError as e:
            return None, [_err("JSON_INVALID", None, f"steps_json is not valid JSON: {e}")]
    if not isinstance(steps, list):
        return None, [_err("NOT_ARRAY", None, "steps_json must be a JSON array")]
    return steps, []


def _check_shape(steps):
    errors = []
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict) or set(s) != set(FIELDS):
            errors.append(_err("STEP_FIELDS", i, f"step must have exactly {list(FIELDS)}"))
        elif not isinstance(s["step_no"], int) or isinstance(s["step_no"], bool) or \
                any(not isinstance(s[k], str) for k in FIELDS[1:]):
            errors.append(_err("STEP_FIELDS", i, "step_no must be an int and every other field a string"))
        elif s["step_no"] != i:
            errors.append(_err("STEP_NUMBER", i, f"step_no must be {i}, got {s['step_no']}"))
    return errors


def _check_step(i, s, audit_at, status):
    """Per-step rules: vocabulary, resource, evidence placement and the hard citation rule."""
    errors = []
    op, phase = s["operation"], s["phase"]
    if phase not in PHASES:
        errors.append(_err("PHASE_INVALID", i, f"unknown phase {phase!r}"))
    if op not in PHASE_OF:
        errors.append(_err("OPERATION_INVALID", i, f"unknown operation {op!r}"))
    elif PHASE_OF[op] != phase:
        errors.append(_err("OP_PHASE_MISMATCH", i, f"{op} belongs to {PHASE_OF[op]}, not {phase}"))
    elif STATE_OF[op] != s["state"]:
        errors.append(_err("STATE_MISMATCH", i, f"{op} must have state {STATE_OF[op]}, got {s['state']!r}"))
    rid = s["resource_id"]
    if not RESOURCE_RE.fullmatch(rid):
        errors.append(_err("RESOURCE_INVALID", i, f"{rid!r} is not a system resource id"))
    elif system_of(rid) != s["system_id"]:
        errors.append(_err("SYSTEM_MISMATCH", i, f"{rid} belongs to {system_of(rid)}, not {s['system_id']!r}"))
    elif op in TREATMENT_OF_OP and not (TREATMENT_OF_OP[op] & legal_treatments(rid)):
        errors.append(_err("ILLEGAL_TREATMENT", i, f"{op} is not allowed for {rid} (policy allows "
                           f"{sorted(legal_treatments(rid))})"))
    errors += _check_evidence(i, s, audit_at, status)
    return errors


def _check_evidence(i, s, audit_at, status):
    ev, phase = s["evidence_id"], s["phase"]
    need = phase == "FINDING" or (phase == "VERIFICATION" and status == "COMPLETE")
    forbid = phase == "REMEDIATION" or (phase == "VERIFICATION" and status == "INCOMPLETE")
    if need and not ev:
        return [_err("EVIDENCE_REQUIRED", i, f"{phase} step must cite an evidence id")]
    if forbid and ev:
        return [_err("EVIDENCE_FORBIDDEN", i, "future work (remediation / post-remediation check) uses evidence_id \"\"")]
    if not ev:
        return []
    t = clock().time_of.get(ev)
    if t is None:
        return [_err("EVIDENCE_UNKNOWN", i, f"{ev!r} is not a file_id, activity_id or audit_id")]
    if t > audit_at:
        return [_err("EVIDENCE_AFTER_AUDIT", i, f"{ev} was captured at {t.isoformat()}, after audit_at {audit_at.isoformat()}")]
    return []


def _check_order(steps):
    keys = [(PHASES.index(s["phase"]), OP_RANK[s["operation"]], s["system_id"], s["resource_id"]) for s in steps]
    errors = []
    for i in range(1, len(keys)):
        if keys[i] < keys[i - 1]:
            errors.append(_err("ORDER", i + 1, "steps must be ordered by phase, operation, system_id, resource_id"))
    if len(set(keys)) != len(keys):
        errors.append(_err("DUPLICATE", None, "the same phase/operation/system/resource appears twice"))
    return errors


def _check_status_rules(status, steps):
    errors = []
    by_phase = {p: [s for s in steps if s["phase"] == p] for p in PHASES}
    if status == "NOT_APPLICABLE":
        return [_err("NOT_APPLICABLE_NOT_EMPTY", None, "NOT_APPLICABLE requires steps_json == []")] if steps else []
    if status == "COMPLETE":
        if not by_phase["VERIFICATION"]:
            errors.append(_err("COMPLETE_EMPTY", None, "COMPLETE needs at least one evidence-backed verification step"))
        if by_phase["FINDING"] or by_phase["REMEDIATION"]:
            errors.append(_err("COMPLETE_HAS_WORK", None, "COMPLETE may contain only VERIFICATION steps"))
        return errors
    # INCOMPLETE: findings, a remediation for each, and a post-remediation check for each remediation
    if not by_phase["FINDING"]:
        errors.append(_err("INCOMPLETE_NO_FINDING", None, "INCOMPLETE needs at least one finding"))
    rem = {(s["system_id"], s["resource_id"]): s["operation"] for s in by_phase["REMEDIATION"]}
    ver = {(s["system_id"], s["resource_id"], s["operation"]) for s in by_phase["VERIFICATION"]}
    for s in by_phase["FINDING"]:
        key = (s["system_id"], s["resource_id"])
        if s["operation"] in FINDING_TO_REMEDIATION and rem.get(key) not in FINDING_TO_REMEDIATION[s["operation"]]:
            errors.append(_err("FINDING_NO_REMEDIATION", s["step_no"], f"finding on {key[1]} has no matching remediation"))
    finding_keys = {(s["system_id"], s["resource_id"]) for s in by_phase["FINDING"]}
    for s in by_phase["REMEDIATION"]:
        key = (s["system_id"], s["resource_id"])
        if key not in finding_keys:
            errors.append(_err("REMEDIATION_NO_FINDING", s["step_no"], f"remediation on {key[1]} has no finding"))
        if s["operation"] in REMEDIATION_TO_VERIFY and (key[0], key[1], REMEDIATION_TO_VERIFY[s["operation"]]) not in ver:
            errors.append(_err("REMEDIATION_NO_VERIFICATION", s["step_no"], f"remediation on {key[1]} has no check afterwards"))
    return errors


def _check_partial(status, steps):
    """A partial row (case not fully resolved): INCOMPLETE, only evidence-free VERIFICATION steps."""
    errors = []
    if status != "INCOMPLETE":
        errors.append(_err("PARTIAL_STATUS", None, "a partial row must have status INCOMPLETE"))
    for i, s in enumerate(steps, 1):
        if s["phase"] != "VERIFICATION":
            errors.append(_err("PARTIAL_PHASE", i, "a partial row may only contain VERIFICATION steps"))
    return errors


def validate(case_id, status, steps_json, audit_at, partial=False):
    """Return a list of errors (empty == valid). `audit_at` is an ISO string or datetime.

    partial=True validates a row for a case that could not be fully resolved: every step-level rule (vocabulary,
    resource, ordering, evidence rules, the hard citation rule) still applies, but the finding -> remediation ->
    check chain is not demanded.
    """
    if status not in STATUSES:
        return [_err("STATUS_INVALID", None, f"deletion_status must be one of {sorted(STATUSES)}, got {status!r}")]
    steps, errors = _parse(steps_json)
    if errors:
        return errors
    audit_at = _t(audit_at) if isinstance(audit_at, str) else audit_at
    errors = _check_shape(steps)
    if errors:
        return errors            # later checks assume well-formed steps
    for i, s in enumerate(steps, 1):
        errors += _check_step(i, s, audit_at, status)
    if not any(e["code"] in ("PHASE_INVALID", "OPERATION_INVALID") for e in errors):
        errors += _check_order(steps)
        errors += _check_partial(status, steps) if partial else _check_status_rules(status, steps)
    return errors


def format_errors(errors):
    return "; ".join((f"step {e['step_no']}: " if e["step_no"] else "") + f"[{e['code']}] {e['message']}" for e in errors)

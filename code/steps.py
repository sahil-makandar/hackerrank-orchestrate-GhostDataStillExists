"""Canonical step construction: from assessed items to ordered, numbered steps.
Order: phase, then operation table order, then system_id, then resource_id.

The agent (or baseline) decides WHAT is non-compliant and which evidence proves it; this module decides how that is
written down: operations, states, phase order, ordering inside a phase, step numbers and evidence placement.
"""
import json
from dataclasses import dataclass

from policy import FINDING_FOR, OP_RANK, PHASES, PHASE_OF, REMEDIATION_FOR, STATE_OF, VERIFICATION_FOR


@dataclass
class Item:
    """One relevant record / data flow / restore path, assessed at audit_at."""
    system_id: str
    resource_id: str
    treatment: str              # DELETE | REDACT | ANONYMIZE | RETAIN | DISABLE | BLOCK  (what policy requires)
    compliant: bool             # already in the required state at audit_at?
    finding_evidence: str = ""  # citable id proving the non-compliant state (when compliant is False)
    verify_evidence: str = ""   # citable id proving the compliant state (COMPLETE cases)
    always_verify: bool = False  # account / retained audit record: verified in every INCOMPLETE case too


def _step(phase, op, item, evidence):
    return {"phase": phase, "system_id": item.system_id, "resource_id": item.resource_id, "operation": op,
            "state": STATE_OF[op], "evidence_id": evidence}


def build_steps(status, items):
    """Return the ordered, numbered steps for `status` (COMPLETE / INCOMPLETE / NOT_APPLICABLE)."""
    if status == "NOT_APPLICABLE":
        return []
    steps = []
    if status == "COMPLETE":
        for it in items:
            steps.append(_step("VERIFICATION", VERIFICATION_FOR[it.treatment], it, it.verify_evidence))
    else:
        for it in items:
            if not it.compliant:
                steps.append(_step("FINDING", FINDING_FOR[it.treatment], it, it.finding_evidence))
                steps.append(_step("REMEDIATION", REMEDIATION_FOR[it.treatment], it, ""))
                steps.append(_step("VERIFICATION", VERIFICATION_FOR[it.treatment], it, ""))
            elif it.always_verify:
                steps.append(_step("VERIFICATION", VERIFICATION_FOR[it.treatment], it, ""))
    seen, unique = set(), []
    for s in steps:
        key = (s["phase"], s["operation"], s["system_id"], s["resource_id"])
        if key not in seen:
            seen.add(key)
            unique.append(s)
    unique.sort(key=lambda s: (PHASES.index(s["phase"]), OP_RANK[s["operation"]], s["system_id"], s["resource_id"]))
    for n, s in enumerate(unique, 1):
        s["step_no"] = n
    return [{k: s[k] for k in ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")}
            for s in unique]


def build_partial(items):
    """Steps for a case we could not fully resolve: status INCOMPLETE, and only the checks we can state, without evidence.

    Nothing is certified (no COMPLETE, no cited proof), but nothing we know is thrown away either. An empty list is
    returned when there is nothing to state.
    """
    steps = [_step("VERIFICATION", VERIFICATION_FOR[it.treatment], it, "") for it in items]
    seen, unique = set(), []
    for s in steps:
        key = (s["operation"], s["system_id"], s["resource_id"])
        if key not in seen:
            seen.add(key)
            unique.append(s)
    unique.sort(key=lambda s: (OP_RANK[s["operation"]], s["system_id"], s["resource_id"]))
    for n, s in enumerate(unique, 1):
        s["step_no"] = n
    return [{k: s[k] for k in ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")}
            for s in unique]


def to_json(steps):
    return json.dumps(steps, ensure_ascii=False, separators=(",", ":"))


assert all(PHASE_OF[v] == "VERIFICATION" for v in VERIFICATION_FOR.values())

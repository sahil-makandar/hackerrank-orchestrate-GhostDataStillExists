"""M3 gate.

 1. Validator accepts all 32 golden sample rows.
 2. Step builder reproduces every golden row exactly from its underlying items (round trip).
 3. Validator rejects 20+ hand-made broken rows, each with the expected error code.
 4. (baseline) the deterministic baseline runs on the 20 tuning samples; every row it emits is validator-clean or counted
    as unresolved, and its scores are recorded.

  python code/evaluation/m3_gate.py
"""
import copy
import csv
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("GHOST_CACHE_ONLY", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import linkage                         # noqa: E402
import validator                       # noqa: E402
from ingest.common import DATASET      # noqa: E402
from policy import OP_RANK, PHASES     # noqa: E402
from steps import Item, build_steps    # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


def load():
    req = {r["case_id"]: r for r in csv.DictReader(open(DATASET / "sample_deletion_requests.csv", encoding="utf-8", newline=""))}
    rows = list(csv.DictReader(open(DATASET / "sample_output.csv", encoding="utf-8", newline="")))
    return req, rows


def items_from_golden(status, steps):
    """Recover the assessed items a golden row was built from (treatment + compliance + evidence)."""
    rem = {(s["system_id"], s["resource_id"]): s["operation"] for s in steps if s["phase"] == "REMEDIATION"}
    treat_of_rem = {"DELETE_RECORD": "DELETE", "REDACT_RECORD": "REDACT", "ANONYMIZE_RECORD": "ANONYMIZE",
                    "DISABLE_DATA_FLOW": "DISABLE", "BLOCK_RESTORE": "BLOCK"}
    treat_of_ver = {"VERIFY_RECORD_ABSENT": "DELETE", "VERIFY_RECORD_PRESENT": "RETAIN", "VERIFY_RECORD_REDACTED": "REDACT",
                    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZE", "VERIFY_DATA_FLOW_DISABLED": "DISABLE",
                    "VERIFY_RESTORE_BLOCKED": "BLOCK"}
    items, findings = [], {(s["system_id"], s["resource_id"]): s for s in steps if s["phase"] == "FINDING"}
    for s in steps:
        if s["phase"] != "VERIFICATION":
            continue
        key = (s["system_id"], s["resource_id"])
        if status == "COMPLETE":
            items.append(Item(key[0], key[1], treat_of_ver[s["operation"]], True, verify_evidence=s["evidence_id"]))
        elif key in findings:
            items.append(Item(key[0], key[1], treat_of_rem[rem[key]], False, finding_evidence=findings[key]["evidence_id"]))
        else:
            items.append(Item(key[0], key[1], treat_of_ver[s["operation"]], True, always_verify=True))
    return items


def renumber(steps):
    for i, s in enumerate(steps, 1):
        s["step_no"] = i
    return steps


def pick(rows, pred):
    for r in rows:
        steps = json.loads(r["steps_json"])
        if pred(r["deletion_status"], steps):
            return r, steps
    raise LookupError("no golden row matches the mutation's target predicate")


def mutations(req, rows):
    manifest = validator.clock().time_of
    later_ids = lambda audit: [k for k, t in sorted(manifest.items()) if k.startswith("evidence_") and t > audit]

    inc = lambda st, steps: st == "INCOMPLETE" and any(s["operation"] == "DELETE_RECORD" for s in steps)
    inc_audit = lambda st, steps: st == "INCOMPLETE" and any(s["operation"] == "VERIFY_RECORD_PRESENT" for s in steps)
    comp = lambda st, steps: st == "COMPLETE" and len(steps) >= 3
    out = []

    def add(name, expect, pred, fn, status=None, raw=None):
        row, steps = pick(rows, pred)
        audit = linkage.parse_time(req[row["case_id"]]["audit_at"])
        steps = copy.deepcopy(steps)
        new_status = status or row["deletion_status"]
        res = fn(steps, audit)
        out.append((name, expect, row["case_id"], new_status, raw if raw is not None else (res if res is not None else steps),
                    req[row["case_id"]]["audit_at"]))

    def first(steps, phase):
        return next(s for s in steps if s["phase"] == phase)

    add("gap in step numbers", "STEP_NUMBER", inc, lambda st, a: st[2].__setitem__("step_no", 99))
    add("two steps swapped (order)", "ORDER", inc, lambda st, a: renumber(st.__setitem__(slice(None), [st[-1]] + st[1:-1] + [st[0]]) or st))
    add("finding cites evidence captured after audit_at", "EVIDENCE_AFTER_AUDIT", inc,
        lambda st, a: first(st, "FINDING").__setitem__("evidence_id", later_ids(a)[0]))
    add("COMPLETE verification cites evidence captured after audit_at", "EVIDENCE_AFTER_AUDIT", comp,
        lambda st, a: st[0].__setitem__("evidence_id", later_ids(a)[0]))
    add("remediation cites evidence", "EVIDENCE_FORBIDDEN", inc, lambda st, a: first(st, "REMEDIATION").__setitem__("evidence_id", "evidence_01"))
    add("post-remediation check cites evidence (INCOMPLETE)", "EVIDENCE_FORBIDDEN", inc,
        lambda st, a: first(st, "VERIFICATION").__setitem__("evidence_id", "evidence_01"))
    add("unknown operation", "OPERATION_INVALID", inc, lambda st, a: first(st, "REMEDIATION").__setitem__("operation", "WIPE_RECORD"))
    add("wrong state for operation", "STATE_MISMATCH", inc,
        lambda st, a: next(s for s in st if s["operation"] == "DELETE_RECORD").__setitem__("state", "REDACTED"))
    add("NOT_APPLICABLE with steps", "NOT_APPLICABLE_NOT_EMPTY", inc, lambda st, a: None, status="NOT_APPLICABLE")
    add("COMPLETE containing a finding", "COMPLETE_HAS_WORK", comp,
        lambda st, a: renumber(st.insert(0, {"step_no": 0, "phase": "FINDING", "system_id": "search_service",
                                             "resource_id": "search_1", "operation": "REPORT_REMAINING_RECORD",
                                             "state": "PRESENT", "evidence_id": "evidence_01"}) or st))
    add("remediation without a check afterwards", "REMEDIATION_NO_VERIFICATION", inc,
        lambda st, a: renumber(st.remove(next(s for s in st if s["operation"] == "VERIFY_RECORD_ABSENT"
                                              and any(r["resource_id"] == s["resource_id"] for r in st if r["phase"] == "REMEDIATION"))) or st))
    add("retained audit record verified as ABSENT (illegal treatment)", "ILLEGAL_TREATMENT", inc_audit,
        lambda st, a: next(s for s in st if s["operation"] == "VERIFY_RECORD_PRESENT").update(operation="VERIFY_RECORD_ABSENT", state="ABSENT"))
    add("duplicate step", "DUPLICATE", inc, lambda st, a: renumber(st.insert(1, copy.deepcopy(st[0])) or st))
    add("unknown evidence id", "EVIDENCE_UNKNOWN", inc, lambda st, a: first(st, "FINDING").__setitem__("evidence_id", "evidence_999"))
    add("finding without evidence", "EVIDENCE_REQUIRED", inc, lambda st, a: first(st, "FINDING").__setitem__("evidence_id", ""))
    add("COMPLETE verification without evidence", "EVIDENCE_REQUIRED", comp, lambda st, a: st[0].__setitem__("evidence_id", ""))
    add("extra field on a step", "STEP_FIELDS", inc, lambda st, a: st[0].__setitem__("note", "x"))
    add("finding without remediation", "FINDING_NO_REMEDIATION", inc,
        lambda st, a: renumber(st.remove(first(st, "REMEDIATION")) or st))
    add("system_id does not match resource prefix", "SYSTEM_MISMATCH", inc, lambda st, a: st[0].__setitem__("system_id", "billing_service"))
    add("invalid status", "STATUS_INVALID", inc, lambda st, a: None, status="DONE")
    add("steps_json is not JSON", "JSON_INVALID", inc, lambda st, a: None, raw="[{")
    add("COMPLETE with no steps", "COMPLETE_EMPTY", comp, lambda st, a: None, raw=[])
    add("INCOMPLETE with no steps", "INCOMPLETE_NO_FINDING", inc, lambda st, a: None, raw=[])
    add("phase order violated (verification first)", "ORDER", inc,
        lambda st, a: renumber(st.sort(key=lambda s: (-PHASES.index(s["phase"]), OP_RANK[s["operation"]], s["system_id"], s["resource_id"])) or st))
    add("DELETE on a retained audit record", "ILLEGAL_TREATMENT", inc_audit,
        lambda st, a: renumber(st.append({"step_no": 0, "phase": "REMEDIATION", "system_id": "security_service",
                                          "resource_id": next(s for s in st if s["operation"] == "VERIFY_RECORD_PRESENT")["resource_id"],
                                          "operation": "DELETE_RECORD", "state": "ABSENT", "evidence_id": ""}) or st))
    return out


def main():
    req, rows = load()

    bad = [(r["case_id"], validator.format_errors(validator.validate(r["case_id"], r["deletion_status"], r["steps_json"],
                                                                      req[r["case_id"]]["audit_at"])))
           for r in rows]
    bad = [b for b in bad if b[1]]
    check(f"validator accepts all {len(rows)} golden sample rows", not bad, str(bad[:2]))

    mism = []
    for r in rows:
        steps = json.loads(r["steps_json"])
        rebuilt = build_steps(r["deletion_status"], items_from_golden(r["deletion_status"], steps))
        if rebuilt != steps:
            mism.append(r["case_id"])
    check(f"step builder reproduces all {len(rows)} golden rows exactly from their items", not mism, str(mism[:5]))

    muts = mutations(req, rows)
    missed = []
    for name, expect, case_id, status, steps, audit_at in muts:
        errs = validator.validate(case_id, status, steps if isinstance(steps, str) else json.dumps(steps), audit_at)
        codes = {e["code"] for e in errs}
        if expect not in codes:
            missed.append((name, expect, sorted(codes)))
    check(f"validator rejects all {len(muts)} broken rows with the expected error code", not missed, str(missed[:4]))
    check("at least 10 broken rows were tried", len(muts) >= 10, str(len(muts)))

    # partial rows (case not fully resolved): INCOMPLETE + evidence-free verification steps only
    pv = [{"step_no": 1, "phase": "VERIFICATION", "system_id": "account_service", "resource_id": "account_1",
           "operation": "VERIFY_RECORD_ABSENT", "state": "ABSENT", "evidence_id": ""}]
    aud = "2026-09-15T12:00:00Z"
    codes = lambda status, steps, partial=True: {e["code"] for e in validator.validate("x", status, steps, aud, partial=partial)}
    check("partial row (INCOMPLETE, evidence-free verification) is accepted", not codes("INCOMPLETE", pv))
    check("partial row with no steps at all is accepted", not codes("INCOMPLETE", []))
    check("partial row may not claim COMPLETE", "PARTIAL_STATUS" in codes("COMPLETE", pv))
    check("partial row may not contain findings", "PARTIAL_PHASE" in codes("INCOMPLETE", [dict(pv[0], phase="FINDING", operation="REPORT_REMAINING_RECORD", state="PRESENT", evidence_id="evidence_01")]))
    check("partial row may not cite evidence", "EVIDENCE_FORBIDDEN" in codes("INCOMPLETE", [dict(pv[0], evidence_id="evidence_01")]))
    check("a full (non-partial) INCOMPLETE row with only a check is still rejected", "INCOMPLETE_NO_FINDING" in codes("INCOMPLETE", pv, partial=False))

    try:
        import baseline                          # noqa: E402  (built after the validator)
    except ImportError:
        print("SKIP baseline (not built yet)")
    else:
        ok_b = baseline.gate_report(req, rows)
        check("baseline answers all 20 tuning samples; unresolved cases become INCOMPLETE with the validated steps we have", ok_b)

    print("\nM3 GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

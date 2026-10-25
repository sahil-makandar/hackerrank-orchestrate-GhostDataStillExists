"""Scoring shared by the baseline and the agent: case, step, field and sequence accuracy against an answer file.

  case accuracy      deletion_status equals the reference
  step precision / recall / F1
                     over the set of (phase, operation, system_id, resource_id, state, evidence_id) tuples ("full")
                     and over (phase, operation, system_id, resource_id) ("identity": right step, any evidence)
  field accuracy     for reference steps that the prediction also contains (matched on phase/op/system/resource):
                     share with the correct state, and with the correct evidence_id
  sequence accuracy  row-level exact match of the whole ordered steps array ("strict"), and the mean longest-common-
                     subsequence ratio of the ordered (phase, operation, system, resource) lists ("lcs")
"""
import json


def _id(s):
    return (s["phase"], s["operation"], s["system_id"], s["resource_id"])


def full_tuple(s):
    return _id(s) + (s["state"], s["evidence_id"])


def _lcs(a, b):
    if not a and not b:
        return 1.0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b, 1):
            cur.append(prev[j - 1] + 1 if x == y else max(prev[j], cur[-1]))
        prev = cur
    return prev[-1] / max(len(a), len(b))


def prf(tp, pred, ref):
    p = tp / pred if pred else (1.0 if not ref else 0.0)
    r = tp / ref if ref else 1.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def score(pred_rows, ref_rows):
    """pred_rows / ref_rows: {case_id: (status, steps_list)}. Cases missing from pred count as wrong."""
    n = len(ref_rows)
    case_ok = strict = 0
    lcs_sum = 0.0
    tp_full = tp_id = n_pred = n_ref = 0
    matched = state_ok = evid_ok = 0
    for cid, (st, steps) in ref_rows.items():
        pst, psteps = pred_rows.get(cid, (None, []))
        case_ok += pst == st
        strict += pst == st and psteps == steps
        lcs_sum += _lcs([_id(s) for s in psteps], [_id(s) for s in steps])
        ref_full, pred_full = {full_tuple(s) for s in steps}, {full_tuple(s) for s in psteps}
        ref_id, pred_id = {_id(s) for s in steps}, {_id(s) for s in psteps}
        tp_full += len(ref_full & pred_full)
        tp_id += len(ref_id & pred_id)
        n_pred += len(pred_id)
        n_ref += len(ref_id)
        by_id = {_id(s): s for s in psteps}
        for s in steps:
            if _id(s) in by_id:
                matched += 1
                state_ok += by_id[_id(s)]["state"] == s["state"]
                evid_ok += by_id[_id(s)]["evidence_id"] == s["evidence_id"]
    pf, rf, ff = prf(tp_full, sum(len({full_tuple(s) for s in pred_rows.get(c, (None, []))[1]}) for c in ref_rows),
                      sum(len({full_tuple(s) for s in v[1]}) for v in ref_rows.values()))
    pi, ri, fi = prf(tp_id, n_pred, n_ref)
    return {
        "cases": n, "case_accuracy": case_ok / n,
        "step_identity": {"precision": pi, "recall": ri, "f1": fi},
        "step_full": {"precision": pf, "recall": rf, "f1": ff},
        "field_accuracy": {"state": state_ok / matched if matched else 0.0, "evidence_id": evid_ok / matched if matched else 0.0,
                           "matched_steps": matched},
        "sequence": {"strict_row_exact": strict / n, "lcs_mean": lcs_sum / n},
    }


def load_reference(rows):
    """CSV dict rows -> {case_id: (status, steps)}."""
    return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in rows}


def format_score(name, s):
    si, sf, fa, sq = s["step_identity"], s["step_full"], s["field_accuracy"], s["sequence"]
    return (f"{name}: case acc {s['case_accuracy']:.3f} | step(identity) P {si['precision']:.3f} R {si['recall']:.3f} F1 {si['f1']:.3f}"
            f" | step(full) P {sf['precision']:.3f} R {sf['recall']:.3f} F1 {sf['f1']:.3f}"
            f" | field state {fa['state']:.3f} evidence {fa['evidence_id']:.3f} (n={fa['matched_steps']})"
            f" | sequence strict {sq['strict_row_exact']:.3f} lcs {sq['lcs_mean']:.3f}")

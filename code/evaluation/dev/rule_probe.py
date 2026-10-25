"""Measure three candidate general rules (nothing is applied to the code or to output.csv).

  python code/evaluation/dev/rule_probe.py

R1  absence from an account-table drift capture: an account is ABSENT at audit_at if a SQL snapshot of the account table,
    captured after the request and by audit_at, enumerates BOTH a primary and a replica copy and lists the account in
    neither. (Today absence is accepted only for vanished records or snapshots that cover the subject.)
R2  support category from the fields of the record itself: SUPPORT_MESSAGE (redact) when the record has message fields,
    else SUPPORT_ATTACHMENT (delete). A verdict whose category disagrees with the fields is rejected.
R4  two identifier-linked accounts for one subject (two requests of one person): each account goes to the request whose
    burst holds its delete event (result matched to reported_status, inside the tight window), one-to-one.

Label-free where possible (the 247 anchored cases, the identifier link as the truth); the 20 tuning answers otherwise.
"""
import collections
import csv
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("GHOST_CACHE_ONLY", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import baseline                                   # noqa: E402
import linkage                                    # noqa: E402
import split                                      # noqa: E402
import state                                      # noqa: E402
from baseline import NON_COMPLIANT                # noqa: E402
from policy import RULES, prefix_of, treatment_for_category   # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "code" / "cache" / "agent"
WM = linkage.WindowMatcher


def r1_file(ctx, rid, case):
    """The drift-capture file that proves `rid` absent for this case (latest), else None."""
    best = None
    by_file = collections.defaultdict(list)
    for s in ctx.index.scopes:
        if s.system_id == "account_service" and ctx.index.manifest[s.file_id]["file_path"].endswith(".sql"):
            if case["_requested_at"] < linkage.parse_time(s.obs_time) <= case["_audit_at"]:
                by_file[s.file_id].append(s)
    for fid, ss in by_file.items():
        names = " ".join(s.collection for s in ss)
        listed = set().union(*[set(s.resource_ids) for s in ss])
        if "primary" in names and "replica" in names and listed and rid not in listed:
            t = max(linkage.parse_time(s.obs_time) for s in ss)
            if best is None or t > best[0]:
                best = (t, fid)
    return best[1] if best else None


def window_ok(ctx, case, ev):
    return WM.RESULT.get(case["reported_status"]) == ev["reported_result"] and WM.LO <= ctx.timing.offset_h(ev, case) <= WM.HI


def r1(ctx, tuning, gold):
    print("== R1 absence from an account-table drift capture")
    files = sorted({s.file_id for s in ctx.index.scopes if s.system_id == "account_service" and "primary" in s.collection})
    print("   files that enumerate a primary copy of the account table:", files)
    fixed, wrong = [], []
    for cid in tuning:
        accs = [s for s in gold[cid] if s["system_id"] == "account_service"]
        if not accs:
            continue
        a = accs[0]
        f = r1_file(ctx, a["resource_id"], ctx.by_id[cid])
        if f:
            ok = a["operation"] == "VERIFY_RECORD_ABSENT" and a["evidence_id"] == f
            (fixed if ok else wrong).append((cid, a["resource_id"], f, a["operation"], a["evidence_id"]))
    print(f"   tuning: applies to {len(fixed) + len(wrong)} answer accounts; answer = ABSENT citing the same file: "
          f"{len(fixed)} {[x[0] for x in fixed]}; contradicted by the answer: {len(wrong)} {wrong}")
    fm = linkage.ForcedMatcher(ctx.cases, ctx.timing, ctx.ident, ctx.owners, ctx.user)
    agree = contra = unknown = napply = 0
    contra_ex = []
    for cid, evs in fm.anchor_events.items():
        own = [fm.events[e] for e in evs if cid in ctx.timing.owners(fm.events[e])]
        if len(own) != 1:
            continue
        case, rid = ctx.by_id[cid], own[0]["resource_id"]
        f = r1_file(ctx, rid, case)
        if not f:
            continue
        napply += 1
        t_snap = linkage.parse_time(ctx.index.manifest[f]["captured_at"])
        rs = state.readings(ctx.index, rid, case["_audit_at"], "two")
        later = [r for r in rs if r.time >= t_snap and r.kind != "UNKNOWN"]
        cur = state.current(rs, case["_requested_at"])
        if any(r.kind in NON_COMPLIANT["DELETE"] for r in later):
            contra += 1
            contra_ex.append((cid, rid))
        elif cur is not None and cur.kind == "ABSENT":
            agree += 1
        else:
            unknown += 1
    print(f"   hidden-link set (247 anchored): applies to {napply}; agrees with the identifier-based reading {agree}; "
          f"contradicted by a PRESENT reading at or after the snapshot {contra} {contra_ex[:3]}; no reading to compare {unknown}")
    rows = {r["case_id"]: r for r in csv.DictReader(open(ROOT / "output.csv", encoding="utf-8", newline=""))}
    eff = collections.Counter()
    for cid, r in rows.items():
        if r["deletion_status"] == "NOT_APPLICABLE":
            continue
        steps = json.loads(r["steps_json"])
        w = ctx.window.get(cid)
        accs = [s["resource_id"] for s in steps if s["system_id"] == "account_service"] or ([w.resource_id] if w else [])
        for rid in accs[:1]:
            if r1_file(ctx, rid, ctx.by_id[cid]):
                has = any(s["resource_id"] == rid for s in steps)
                cited = any(s["resource_id"] == rid and s["evidence_id"] for s in steps)
                eff[(r["deletion_status"], "step present" if has else "no step yet", "cited" if cited else "uncited")] += 1
    partial = [c for c, r in rows.items() if r["deletion_status"] == "INCOMPLETE" and r["steps_json"] != "[]"
               and all(s["phase"] == "VERIFICATION" for s in json.loads(r["steps_json"]))]
    print("   output.csv rows whose account the rule would prove absent:", dict(eff), "| partial rows now:", partial)


def r2(ctx, tuning, gold):
    print("\n== R2 support category from the fields of the record")
    dis_t, n_t = [], 0
    for cid in tuning:
        d = json.loads((RUNS / "b_t5" / f"{cid}.json").read_text(encoding="utf-8"))
        for rec in (d.get("payload") or {}).get("records", []):
            if prefix_of(rec["resource_id"]) == "support" and rec["data_category"] in RULES:
                n_t += 1
                want = baseline.treatment_for(rec["resource_id"], ctx.index.by_resource.get(rec["resource_id"], []))
                if want != treatment_for_category(rec["data_category"]):
                    ans = ",".join(s["operation"] for s in gold[cid] if s["resource_id"] == rec["resource_id"])
                    dis_t.append((cid, rec["resource_id"], rec["data_category"], "fields say " + str(want), "answer ops " + ans))
    print(f"   tuning agent verdicts (b_t5): {n_t} support records; disagree with the fields rule: {len(dis_t)} {dis_t}")
    n_f, dis_f, ex = 0, 0, []
    for p in (RUNS / "final_v1d").glob("case_*.json"):
        d = json.loads(p.read_text(encoding="utf-8"))
        if d["source"] != "agent":
            continue
        for rec in (d.get("payload") or {}).get("records", []):
            if prefix_of(rec["resource_id"]) == "support" and rec["data_category"] in ("SUPPORT_MESSAGE", "SUPPORT_ATTACHMENT"):
                n_f += 1
                want = baseline.treatment_for(rec["resource_id"], ctx.index.by_resource.get(rec["resource_id"], []))
                if want != treatment_for_category(rec["data_category"]):
                    dis_f += 1
                    ex.append((d["case_id"], rec["resource_id"], rec["data_category"], want))
    print(f"   full run (final_v1d agent rows): {n_f} support records; disagree with the fields rule: {dis_f} {ex[:5]}")
    # how often does the fields rule itself match the answers (all 32 samples, in-sample): support steps
    ok = tot = 0
    for cid, steps in gold.items():
        for rid in {s["resource_id"] for s in steps if s["resource_id"].startswith("support_")}:
            ops = {s["operation"] for s in steps if s["resource_id"] == rid}
            truth = "REDACT" if ops & {"REDACT_RECORD", "VERIFY_RECORD_REDACTED"} else "DELETE" if ops & {"DELETE_RECORD", "VERIFY_RECORD_ABSENT"} else None
            if truth:
                tot += 1
                ok += baseline.treatment_for(rid, ctx.index.by_resource.get(rid, [])) == truth
    print(f"   the fields rule against the 32 answers (in-sample): {ok}/{tot} support records right")


def r4(ctx, tuning, gold):
    print("\n== R4 two identifier-linked accounts for one subject")
    multi = {cid: sorted(r for r in ctx.ident[cid] if r.startswith("account_")) for cid in ctx.ident}
    multi = {c: a for c, a in multi.items() if len(a) >= 2}
    ev = {a["resource_id"]: a for a in ctx.timing.events if (a["event_type"], a["target_system"]) == ("DELETE_RECORD", "account_service")}
    print("   cases with 2+ identifier accounts:", len(multi))
    picked = amb = none = 0
    owner = collections.Counter()
    for cid, accs in multi.items():
        ok = [a for a in accs if a in ev and window_ok(ctx, ctx.by_id[cid], ev[a])]
        picked += len(ok) == 1
        amb += len(ok) > 1
        none += not ok
        if len(ok) == 1:
            owner[ok[0]] += 1
    print(f"   the rule picks exactly one account in the window: {picked}; several: {amb}; none: {none}; "
          f"accounts picked by 2+ cases: {[a for a, n in owner.items() if n > 1]}")
    for cid in tuning:
        if cid in multi:
            g = [s["resource_id"] for s in gold[cid] if s["system_id"] == "account_service"]
            ok = [a for a in multi[cid] if a in ev and window_ok(ctx, ctx.by_id[cid], ev[a])]
            print(f"   tuning {cid}: accounts {multi[cid]}, rule picks {ok}, answer {g}")
    # label-free check of the picking power: anchored one-account cases whose OTHER request's account is added as a decoy
    fm = linkage.ForcedMatcher(ctx.cases, ctx.timing, ctx.ident, ctx.owners, ctx.user)
    right = wrong_ = silent = 0
    for cid, evs in fm.anchor_events.items():
        own = [fm.events[e] for e in evs if cid in ctx.timing.owners(fm.events[e])]
        if len(own) != 1:
            continue
        truth = own[0]["resource_id"]
        decoys = [a["resource_id"] for e, a in fm.events.items()
                  if a["resource_id"] != truth and window_ok(ctx, ctx.by_id[cid], a) and e not in evs][:1]
        if not decoys:
            continue
        ok = [a for a in (truth, decoys[0]) if a in ev and window_ok(ctx, ctx.by_id[cid], ev[a])]
        if len(ok) == 1:
            right += ok[0] == truth
            wrong_ += ok[0] != truth
        else:
            silent += 1
    print(f"   decoy test on the 247 anchored cases (truth + one window decoy): picks the truth {right}, picks the decoy {wrong_}, "
          f"cannot separate {silent}")


def main():
    ctx = baseline.Context()
    tuning = split.tuning_cases()
    gold = {r["case_id"]: json.loads(r["steps_json"]) for r in
            csv.DictReader(open(ROOT / "dataset" / "sample_output.csv", encoding="utf-8", newline=""))}
    r1(ctx, tuning, gold)
    r2(ctx, tuning, gold)
    r4(ctx, tuning, gold)


if __name__ == "__main__":
    main()

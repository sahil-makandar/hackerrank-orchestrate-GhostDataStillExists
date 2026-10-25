"""Why do bounded-agent cases fall back, and where do status errors come from? (reads run transcripts; no model calls)

  python code/evaluation/fallback_status_analysis.py --runs b_t4,b_t5,final_v1 [--confirm b_heldout]

Fallback: for every case, the lane it was assigned and the model(s) that wrote its verdicts. A verdict written by a model
other than the assigned lane means the case was rerouted (rate limit / quota / timeout on the assigned lane). Rejection
rates are given per verdict-writing model, and fallback rates for rerouted vs not rerouted cases.

Status: checks that every agent row's status follows from its steps (INCOMPLETE iff it has findings or is a partial
row; COMPLETE iff verification only, each with evidence), and classifies status errors against the answers (tuning
runs only; --confirm prints aggregate counts for a held-out run without per-case details).
"""
import argparse
import collections
import csv
import json
from pathlib import Path

RUNS = Path(__file__).resolve().parents[2] / "cache" / "agent"
DATASET = Path(__file__).resolve().parents[3] / "dataset"
LITE = ("flash-lite",)


def load(run):
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((RUNS / run).glob("case_*.json"))]


def short(m):
    return m.split("/")[-1]


def fallback_part(rows, label):
    per_model = collections.defaultdict(collections.Counter)
    rerouted = collections.Counter()
    reasons = collections.Counter()
    for d in rows:
        verdicts = [c for c in d["transcript"] if "accepted" in c]
        lane = short(d.get("first_lane", ""))
        writers = {short(c["model"]) for c in verdicts}
        moved = bool(writers) and any(w != lane for w in writers)
        key = "rerouted" if moved else "on assigned lane"
        rerouted[(key, "fallback" if d["source"] == "baseline" else "agent")] += 1
        for c in verdicts:
            per_model[short(c["model"])]["accepted" if c["accepted"] else "rejected"] += 1
        if d["source"] == "baseline":
            reasons[d.get("fallback_reason")] += 1
            last = verdicts[-1] if verdicts else None
            reasons[f"   last verdict by {'a lite model' if last and any(x in last['model'] for x in LITE) else 'a non-lite model' if last else 'no verdict'}"] += 1
    print(f"\n== {label}: {len(rows)} cases, fallback {sum(1 for d in rows if d['source'] == 'baseline')}")
    for k, v in sorted(reasons.items()):
        print(f"   {k}: {v}")
    for key in ("on assigned lane", "rerouted"):
        a, f = rerouted[(key, "agent")], rerouted[(key, "fallback")]
        if a + f:
            print(f"   {key:17s}: {a + f:3d} cases, fallback {f} ({f / (a + f):.0%})")
    print("   verdict acceptance by writing model:")
    for m, c in sorted(per_model.items(), key=lambda kv: -(kv[1]['accepted'] + kv[1]['rejected'])):
        n = c["accepted"] + c["rejected"]
        print(f"     {m:30s} {c['accepted']:3d}/{n:<3d} accepted ({c['accepted'] / n:.0%})")


def status_consistency(rows):
    bad = []
    for d in rows:
        if d["source"] != "agent":
            continue
        has_finding = any(s["phase"] == "FINDING" for s in d["steps"])
        partial = bool(d.get("partial"))
        if d["status"] == "NOT_APPLICABLE":
            ok = not d["steps"]
        elif d["status"] == "INCOMPLETE":
            ok = has_finding or partial
        else:
            ok = not has_finding and d["steps"] and all(s["evidence_id"] for s in d["steps"])
        if not ok:
            bad.append(d["case_id"])
    return bad


def status_errors(rows, answers):
    cause = collections.Counter()
    detail = []
    for d in rows:
        if d["case_id"] not in answers or d["status"] == answers[d["case_id"]]:
            continue
        gold = answers[d["case_id"]]
        if d["source"] == "baseline":
            c = f"fallback row (baseline said {d['status']})"
        elif d.get("partial"):
            c = "agent could not prove the states -> partial INCOMPLETE (never certified)"
        elif d["status"] == "INCOMPLETE" and gold == "COMPLETE":
            fnd = [s["resource_id"] for s in d["steps"] if s["phase"] == "FINDING"]
            c = "agent reported a finding the answer does not have"
            detail.append((d["case_id"], f"findings {fnd}"))
        elif d["status"] == "COMPLETE" and gold == "INCOMPLETE":
            c = "agent missed a finding (judged a record compliant)"
            detail.append((d["case_id"], "no findings"))
        else:
            c = f"intent: agent {d['status']} vs answer {gold}"
        cause[c] += 1
    return cause, detail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="b_t4,b_t5,final_v1")
    ap.add_argument("--confirm", default="")
    a = ap.parse_args()
    answers = {r["case_id"]: r["deletion_status"]
               for r in csv.DictReader(open(DATASET / "sample_output.csv", encoding="utf-8", newline=""))}
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import split
    tuning = set(split.tuning_cases())
    for run in [r for r in a.runs.split(",") if r]:
        rows = load(run)
        fallback_part(rows, run)
        bad = status_consistency(rows)
        print(f"   status consistent with the steps on every agent row: {'yes' if not bad else 'NO ' + str(bad)}")
        tun = [d for d in rows if d["case_id"] in tuning]
        if tun:
            cause, detail = status_errors(tun, answers)
            print(f"   status errors on tuning cases: {sum(cause.values())}")
            for k, v in cause.items():
                print(f"     {v} x {k}")
            for x in detail:
                print("       ", *x)
    if a.confirm:
        rows = load(a.confirm)
        fallback_part(rows, a.confirm + " (held-out: aggregate only, not used for tuning)")
        cause, _ = status_errors(rows, answers)
        print(f"   status errors: {sum(cause.values())}: " + "; ".join(f"{v} x {k}" for k, v in cause.items()))
        print(f"   status consistent with the steps: {'yes' if not status_consistency(rows) else 'NO'}")


if __name__ == "__main__":
    main()

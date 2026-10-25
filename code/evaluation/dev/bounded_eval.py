"""Run the bounded agent on the 20 TUNING cases and compare it with an earlier open-loop run.

  python code/evaluation/bounded_eval.py --run-id b_t1 --compare m5_t3 [--workers 8]

Reports: accuracy vs answers for agent / baseline / the compared run (same cases), fallback with reasons, calls and tokens
per case (this run vs the compared run), lanes used, every unfinished case and why (rate limit, call budget, bad verdict,
crash), disagreements with the baseline, and a full-run time estimate from measured throughput and the daily quotas.
"""
import argparse
import collections
import csv
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import baseline                       # noqa: E402
import scoring                        # noqa: E402
import split                          # noqa: E402
import validator                      # noqa: E402
from agent import bounded             # noqa: E402
from agent.bounded import disagreement   # noqa: E402
from ingest.common import DATASET     # noqa: E402

RUNS = Path(__file__).resolve().parents[2] / "cache" / "agent"
USAGE = Path(__file__).resolve().parents[2] / "cache" / "usage.jsonl"
FULL_CASES = 518
DAILY_CAP = {"groq": 1000, "gemini_per_model": 20}


def load_run(run_id, cases):
    out = {}
    for c in cases:
        p = RUNS / run_id / f"{c}.json"
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            d.setdefault("calls", d.get("turns", 0))
            out[c] = d
    return out


def per_case(res):
    calls = [r["calls"] for r in res.values()]
    toks = [r["input_tokens"] + r["output_tokens"] for r in res.values()]
    return {"cases": len(res), "calls_mean": statistics.mean(calls), "calls_max": max(calls),
            "tokens_mean": statistics.mean(toks), "tokens_median": statistics.median(toks), "tokens_max": max(toks)}


def usage_rates(run_id):
    rows = [json.loads(l) for l in USAGE.read_text(encoding="utf-8").splitlines() if '"run": "%s"' % run_id in l]
    rows = [r for r in rows if r.get("run") == run_id]
    if not rows:
        return {}, 0, collections.Counter()
    span = max(rows[-1]["ts"] - rows[0]["ts"], 60) / 60
    ok = collections.Counter(r["provider"] for r in rows if r["ok"] and not r["cached"])
    fails = collections.Counter((r["provider"], (r.get("error") or "")[:40]) for r in rows if not r["ok"])
    return {p: n / span for p, n in ok.items()}, span, fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--compare", default="")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--cases", default="")
    ap.add_argument("--analyse-only", action="store_true")
    ap.add_argument("--heldout", action="store_true", help="FINAL scoring on the 12 held-out cases (logged; run once)")
    a = ap.parse_args()
    os.environ["GHOST_RUN_ID"] = a.run_id
    tuning = split.tuning_cases()
    if a.heldout:
        wanted = split.heldout_cases(f"final held-out scoring, run {a.run_id}")
        tuning = wanted
    else:
        wanted = [c for c in a.cases.split(",") if c] or tuning
    if [c for c in wanted if c not in tuning]:
        sys.exit("refusing to run held-out / unknown cases during tuning")
    ctx = baseline.Context()
    t0 = time.time()
    if not a.analyse_only:
        print(f"bounded run {a.run_id}: {len(wanted)} tuning cases, workers={a.workers}")
        bounded.run_many(ctx, [ctx.by_id[c] for c in wanted], a.run_id, workers=a.workers)
    wall = time.time() - t0
    res = load_run(a.run_id, wanted)
    ref = scoring.load_reference([r for r in csv.DictReader(open(DATASET / "sample_output.csv", encoding="utf-8", newline=""))
                                  if r["case_id"] in wanted])
    print()
    print(scoring.format_score("BOUNDED ", scoring.score({c: (r["status"], r["steps"]) for c, r in res.items()}, ref)))
    print(scoring.format_score("BASELINE", scoring.score({c: (r["baseline"]["status"], r["baseline"]["steps"]) for c, r in res.items()}, ref)))
    old = load_run(a.compare, wanted) if a.compare else {}
    if old:
        print(scoring.format_score(f"{a.compare:8s}", scoring.score({c: (r["status"], r["steps"]) for c, r in old.items()},
                                                                  {c: ref[c] for c in old})) + f"  ({len(old)} cases)")

    n = len(res)
    fb = collections.Counter(r["fallback_reason"] for r in res.values() if r["fallback_reason"])
    print(f"\nfallback: {sum(fb.values())}/{n} = {sum(fb.values()) / n:.0%} {dict(fb)}   (target < 10%)")
    for c, r in sorted(res.items()):
        if r["fallback_reason"]:
            print(f"   {c}: {r['fallback_reason']}: {(r['fallback_detail'] or '')[:160]}")
    pc = per_case(res)
    print(f"calls per case: mean {pc['calls_mean']:.2f}, max {pc['calls_max']} (hard cap {bounded.MAX_CALLS}); "
          f"tokens per case: mean {pc['tokens_mean']:,.0f}, median {pc['tokens_median']:,.0f}, max {pc['tokens_max']:,.0f}")
    if old:
        po = per_case(old)
        print(f"   compared run {a.compare}: calls (turns) per case mean {po['calls_mean']:.2f}, max {po['calls_max']}; "
              f"tokens per case mean {po['tokens_mean']:,.0f}, median {po['tokens_median']:,.0f}, max {po['tokens_max']:,.0f}")
    rounds = collections.Counter(r.get("action_rounds", 0) for r in res.values())
    print(f"action rounds used: {dict(sorted(rounds.items()))}")
    print(f"first lane per case: {dict(collections.Counter(r.get('first_lane') for r in res.values()))}")
    print(f"models that answered: {dict(collections.Counter(m for r in res.values() for m in r['models']))}")
    invalid = [c for c, r in res.items() if validator.validate(c, r["status"], r["steps"], ctx.by_id[c]["_audit_at"],
                                                               partial=bool(r.get("partial")) or (r["source"] == "baseline" and r["baseline"]["partial"]))]
    print(f"invalid rows: {invalid or 'none'}; cutoff violations: {sum(r['cutoff_violations'] for r in res.values())}")
    dis = [disagreement(r) for r in res.values()]
    dis = [d for d in dis if d]
    print(f"agent/baseline disagreements: {len(dis)}")
    for d in dis:
        g = ref[d["case_id"]]
        gi = {(s["phase"], s["operation"], s["resource_id"]) for s in g[1]}
        ai = {(s["phase"], s["operation"], s["resource_id"]) for s in d["agent"]["steps"]}
        bi = {(s["phase"], s["operation"], s["resource_id"]) for s in d["baseline"]["steps"]}
        print(f"   {d['case_id']}: answer {g[0]} | agent {d['agent']['status']} hit {len(ai & gi)} extra {len(ai - gi)} | "
              f"baseline {d['baseline']['status']} hit {len(bi & gi)} extra {len(bi - gi)}")
        print(f"      only agent: {d['only_agent'][:4]} | only baseline: {d['only_baseline'][:4]}")
        print(f"      agent notes: {(d['agent_reasons'].get('notes') or '')[:200]}")

    rates, span, fails = usage_rates(a.run_id)
    if rates:
        print(f"\nmeasured successful calls per minute over {span:.0f} min: " + ", ".join(f"{p} {v:.1f}" for p, v in rates.items()))
        print("failures by provider: " + ", ".join(f"{p} {e!r}: {k}" for (p, e), k in fails.most_common(6)))
        need = pc["calls_mean"] * FULL_CASES
        day_caps = {"groq": DAILY_CAP["groq"], "gemini": DAILY_CAP["gemini_per_model"] * 4}
        per_min = sum(rates.values())
        minutes = need / per_min if per_min else float("inf")
        print(f"full-run estimate: {FULL_CASES} cases x {pc['calls_mean']:.2f} calls = {need:,.0f} calls; at the measured "
              f"{per_min:.1f} calls/min -> {minutes:.0f} min ({minutes / 60:.1f} h). Daily caps: Groq {day_caps['groq']} "
              f"requests, Gemini {day_caps['gemini']} (4 models x 20); NVIDIA has no daily cap.")
    if not a.analyse_only:
        print(f"wall time {wall:.0f}s for {n} cases")


if __name__ == "__main__":
    main()

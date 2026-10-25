"""Section 4 of evaluation_report.md: what the full run did, from its per-case files (no model calls, no answers).

  python code/evaluation/full_run_stats.py --run-id final_v1c

Source mix and fallback reasons, statuses, partial rows, citations picked by code, intent tie-breaks, calls and tokens
per case, models that wrote accepted verdicts, and cases replayed from a stored run.
"""
import argparse
import collections
import json
import statistics
from pathlib import Path

RUNS = Path(__file__).resolve().parents[1] / "cache" / "agent"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    a = ap.parse_args()
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((RUNS / a.run_id).glob("case_*.json"))]
    n = len(rows)
    src = collections.Counter(d["source"] for d in rows)
    print(f"cases {n}: agent {src['agent']} ({src['agent'] / n:.1%}), baseline fallback {src['baseline']} "
          f"({src['baseline'] / n:.1%})")
    print("fallback reasons:", dict(collections.Counter(d["fallback_reason"] for d in rows if d["source"] == "baseline")))
    print("status:", dict(collections.Counter(d["status"] for d in rows)))
    print("status by source:", {s: dict(collections.Counter(d["status"] for d in rows if d["source"] == s)) for s in src})
    print("partial rows (never certified):", sum(1 for d in rows if d.get("partial")),
          "of which agent", sum(1 for d in rows if d.get("partial") and d["source"] == "agent"))
    subs = [w for d in rows if d["source"] == "agent" for w in d.get("warnings") or [] if "instead of" in w]
    print("citations picked by code:", len(subs), "in", sum(1 for d in rows if d["source"] == "agent" and
                                                             any("instead of" in w for w in d.get("warnings") or [])), "cases")
    ic = [d["intent_check"] for d in rows if d.get("intent_check")]
    print("intent disagreements (agent vs rules):", len(ic), dict(collections.Counter(e["action"] for e in ic)))
    print("   tie-breaker answered:", sum(1 for e in ic if e["tiebreak"].get("ok")))
    agree = sum(1 for d in rows if d["source"] == "agent" and d["status"] == d["baseline"]["status"])
    print(f"agent rows whose status equals the baseline's: {agree}/{src['agent']}")
    same_steps = sum(1 for d in rows if d["source"] == "agent" and d["steps"] == d["baseline"]["steps"])
    print(f"agent rows identical to the baseline row: {same_steps}/{src['agent']}")
    calls = [d["calls"] for d in rows]
    toks = [d["input_tokens"] + d["output_tokens"] for d in rows]
    print(f"calls/case mean {statistics.mean(calls):.2f} max {max(calls)}; tokens/case mean {statistics.mean(toks):,.0f} "
          f"median {statistics.median(toks):,.0f} max {max(toks):,}")
    print("action rounds:", dict(sorted(collections.Counter(d["action_rounds"] for d in rows).items())))
    writer = collections.Counter()
    for d in rows:
        acc = [c for c in d.get("transcript", []) if c.get("accepted")]
        if d["source"] == "agent" and acc:
            writer[acc[-1]["model"].split("/")[-1]] += 1
    print("accepted verdict written by:", dict(writer.most_common()))
    lanes = collections.Counter(d.get("first_lane", "").split("/")[-1] for d in rows)
    print("assigned lane:", dict(lanes.most_common()))
    def origin(d):
        if d.get("replay"):
            return d["replay"]
        t = d.get("transcript", [])
        return "re-decided from cached replies" if t and all(c.get("cached") for c in t) else "live"
    print("origin:", dict(collections.Counter(origin(d) for d in rows)))
    print("cutoff violations in tools:", sum(d.get("cutoff_violations", 0) for d in rows))


if __name__ == "__main__":
    main()

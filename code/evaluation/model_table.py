"""Per-model table for evaluation_report.md, from stored run data (no model calls).

  python code/evaluation/model_table.py --cases final_v1e --usage final_v1,final_v1c,final_v1d

Per model:
  - verdicts written and accepted (from the per-case transcripts of --cases, every verdict attempt counted);
  - cases assigned to the model as their lane, and how many of those ended as a baseline fallback;
  - how often the model's first reply asked for actions (1 or 2 action rounds) instead of a direct verdict;
  - live calls, failed attempts and tokens (from code/cache/usage.jsonl rows of the --usage run ids; cache hits excluded).
"""
import argparse
import collections
import json
from pathlib import Path

CACHE = Path(__file__).resolve().parents[1] / "cache"


def short(m):
    return m.split("/")[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", required=True, help="run id whose per-case files produced output.csv")
    ap.add_argument("--usage", required=True, help="comma-separated run ids whose live calls are counted")
    a = ap.parse_args()
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((CACHE / "agent" / a.cases).glob("case_*.json"))]
    m = collections.defaultdict(collections.Counter)
    for d in rows:
        lane = short(d.get("first_lane") or "")
        transcript = d.get("rerun_transcript") or d.get("transcript") or []
        if d.get("rerun") and "accepted on" in d["rerun"]:
            lane = short(transcript[0]["model"]) if transcript else lane
        if lane:
            m[lane]["assigned"] += 1
            m[lane]["assigned_fallback"] += d["source"] == "baseline"
        first = transcript[0] if transcript else None
        if first and not first.get("enforced"):
            k = short(first["model"])
            m[k]["first_replies"] += 1
            m[k]["first_reply_actions"] += bool([x for x in first.get("actions", []) if x != "submit_verdict"])
        for c in transcript:
            if "accepted" in c:
                k = short(c["model"])
                m[k]["verdicts"] += 1
                m[k]["accepted"] += bool(c["accepted"])
                m[k]["enforced_verdicts"] += bool(c.get("enforced"))
    ids = set(a.usage.split(","))
    for line in (CACHE / "usage.jsonl").read_text(encoding="utf-8").splitlines():
        if not any(i in line for i in ids):
            continue
        r = json.loads(line)
        if r.get("run") not in ids or r.get("cached"):
            continue
        k = short(r["model"])
        if r.get("ok"):
            m[k]["calls"] += 1
            m[k]["tokens"] += r.get("input_tokens", 0) + r.get("output_tokens", 0)
        else:
            m[k]["failed"] += 1
    print("| Model | Verdicts accepted | Cases assigned | Fallback of assigned | First reply asked for actions | "
          "Live calls | Failed attempts | Tokens | Tokens / call |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for k, c in sorted(m.items(), key=lambda kv: -kv[1]["calls"]):
        acc = f"{c['accepted']}/{c['verdicts']} ({c['accepted'] / c['verdicts']:.0%})" if c["verdicts"] else "–"
        fb = f"{c['assigned_fallback']} ({c['assigned_fallback'] / c['assigned']:.0%})" if c["assigned"] else "–"
        act = f"{c['first_reply_actions']}/{c['first_replies']} ({c['first_reply_actions'] / c['first_replies']:.0%})" \
            if c["first_replies"] else "–"
        tpc = f"{c['tokens'] / c['calls']:,.0f}" if c["calls"] else "–"
        print(f"| {k} | {acc} | {c['assigned']} | {fb} | {act} | {c['calls']:,} | {c['failed']:,} | {c['tokens']:,} | {tpc} |")
    rounds = collections.Counter(d.get("action_rounds", 0) for d in rows)
    print(f"\naction rounds per case ({len(rows)} cases): " + ", ".join(f"{k}: {v}" for k, v in sorted(rounds.items())))


if __name__ == "__main__":
    main()

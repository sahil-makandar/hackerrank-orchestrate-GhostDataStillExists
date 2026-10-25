"""Token and call usage of one run, from code/cache/usage.jsonl (rows tagged with that run id), for usage_report.md.

  python code/evaluation/usage_summary.py --run-id final_v1 [--cases 518]
  python code/evaluation/usage_summary.py --run-id final_v1,final_v1c     # several runs together

Counts live calls (tokens as reported by the provider), failed attempts and cache hits (0 tokens) per provider/model,
plus the PDF / image extraction calls made while building the evidence index (tagged pdfstruct / vision / ocr).
Cost: every provider used is a free tier, so the actual cost is 0; a list-price estimate is NOT made because these
free-tier models have no published per-token price on the plans used.
"""
import argparse
import collections
import json
from pathlib import Path

USAGE = Path(__file__).resolve().parents[1] / "cache" / "usage.jsonl"

# HYPOTHETICAL list prices, USD per 1M tokens (input, output, source), looked up 2026-10-25. The run used free tiers and
# cost $0; these only say what the same tokens would cost on a paid plan.
LIST_PRICE = {
    "gemini-3.5-flash-lite": (0.30, 2.50, "Google, ai.google.dev/gemini-api/docs/pricing (paid tier, standard)"),
    "gemini-3.1-flash-lite": (0.25, 1.50, "Google, ai.google.dev/gemini-api/docs/pricing (paid tier, standard)"),
    "gemini-3.6-flash": (0.75, 3.75, "Google, ai.google.dev/gemini-api/docs/pricing (through 2026-12-31)"),
    "minimaxai/minimax-m3.1-flash": (0.15, 0.60, "MiniMax list price as reported by several resellers (e.g. OpenRouter, Novita)"),
    "openai/gpt-oss-120b": (0.15, 0.60, "Groq on-demand, as reported by third-party pricing guides (Groq's page did not list it)"),
    "nvidia/nemotron-3-ultra-550b-a55b": (0.50, 2.20, "OpenRouter listing; NVIDIA's own hosted endpoint is free, so this "
                                                      "is a reference third-party price"),
}


def summarise(run_id, cases):
    """run_id: one id or several, comma-separated (a run continued under a new id after a cache-only replay)."""
    ids = set(run_id.split(","))
    rows = [json.loads(line) for line in USAGE.read_text(encoding="utf-8").splitlines() if any(i in line for i in ids)]
    rows = [r for r in rows if r.get("run") in ids]
    per = collections.defaultdict(lambda: collections.Counter())
    for r in rows:
        k = f"{r['provider']}/{r['model']}"
        c = per[k]
        if r.get("cached"):
            c["cached_calls"] += 1
        elif r.get("ok"):
            c["calls"] += 1
            c["input_tokens"] += r.get("input_tokens", 0)
            c["output_tokens"] += r.get("output_tokens", 0)
        else:
            c["failed_attempts"] += 1
    tot = collections.Counter()
    for c in per.values():
        tot.update(c)
    per_case = {k: (tot[k] / cases if cases else 0) for k in ("calls", "input_tokens", "output_tokens")}
    return per, tot, per_case, len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--cases", type=int, default=518)
    a = ap.parse_args()
    per, tot, per_case, n = summarise(a.run_id, a.cases)
    print("| provider / model | calls | failed attempts | cache hits | input tokens | output tokens | total tokens |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for k, c in sorted(per.items(), key=lambda kv: -kv[1]["calls"]):
        print(f"| {k} | {c['calls']:,} | {c['failed_attempts']:,} | {c['cached_calls']:,} | {c['input_tokens']:,} | "
              f"{c['output_tokens']:,} | {c['input_tokens'] + c['output_tokens']:,} |")
    print(f"| **all** | **{tot['calls']:,}** | {tot['failed_attempts']:,} | {tot['cached_calls']:,} | {tot['input_tokens']:,} | "
          f"{tot['output_tokens']:,} | **{tot['input_tokens'] + tot['output_tokens']:,}** |")
    print(f"\nper request ({a.cases}): {per_case['calls']:.2f} calls, {per_case['input_tokens']:,.0f} input + "
          f"{per_case['output_tokens']:,.0f} output tokens ({per_case['input_tokens'] + per_case['output_tokens']:,.0f} total)")
    print("\nHYPOTHETICAL cost at list prices (the run used free tiers: actual cost $0.00)")
    print("| provider / model | $ / 1M input | $ / 1M output | hypothetical cost | price source |")
    print("|---|---:|---:|---:|---|")
    total = 0.0
    for k, c in sorted(per.items(), key=lambda kv: -kv[1]["calls"]):
        model = k.split("/", 1)[1]
        if model not in LIST_PRICE:
            print(f"| {k} | ? | ? | not priced | no list price found |")
            continue
        pin, pout, src = LIST_PRICE[model]
        cost = c["input_tokens"] / 1e6 * pin + c["output_tokens"] / 1e6 * pout
        total += cost
        print(f"| {k} | {pin:.2f} | {pout:.2f} | ${cost:,.2f} | {src} |")
    print(f"| **all** | | | **${total:,.2f}** (${total / a.cases:.4f} per request) | |")


if __name__ == "__main__":
    main()

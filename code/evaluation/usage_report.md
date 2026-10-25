# Usage report: final full-dataset run

This report covers the run that produced `output.csv`: 518 requests from `dataset/deletion_requests.csv`, final run id
**`final_v1e`**. Its live model calls happened in three run ids, and the last step made none:

| Run id | What it did | Live calls |
|---|---|---|
| `final_v1` | first 318 cases | yes |
| `final_v1c` | the 318 re-decided from cached replies under the citation rule, then the other 200 cases | cache hits for the 318, live for the 200 |
| `final_v1d` | the 78 fallback cases re-run on minimax-m3.1-flash only, plus two retries of the cases where it was unavailable | yes |
| `final_v1e` | every stored verdict re-checked under the final checks | none |

The numbers come from:

```text
python code/evaluation/usage_summary.py --run-id final_v1,final_v1c,final_v1d --cases 518
```

That script reads the per-call log `code/cache/usage.jsonl` and counts only rows tagged with these run ids. Development
and tuning calls carry other run ids and are excluded. Calls whose verdict a later step replaced (a rejected first
attempt, a fallback re-run) are still counted, because they are tokens this run spent.

## Providers and models (all free tiers, no paid models)

| Provider | Model | Role in the run |
|---|---|---|
| NVIDIA | minimaxai/minimax-m3.1-flash | lane #2 (weight 2) for the first 318 cases; lane #1 (weight 3) for the last 200; the only model of the fallback re-run; last in the schema-enforced chain (it ignores response schemas) |
| NVIDIA | nvidia/nemotron-3-ultra-550b-a55b | lane #1 for the first 318 cases, then lane #2; first in the schema-enforced chain |
| Groq | openai/gpt-oss-120b | lane for small requests only (8,000 tokens a minute) |
| Google | gemini-3.5-flash-lite, gemini-3.1-flash-lite | lanes, last-resort fallbacks (20 requests a day per model) |
| Google | gemini-3.6-flash, gemini-3.5-flash | lanes, but no calls in this run: their daily quota was already used up |
| any of the above | | intent tie-break, a model from another provider: never needed, since the agent and the rules agreed on all 518 requests |

## Model calls and tokens, per model

- **Calls:** live calls that returned a reply.
- **Failed attempts:** rate limits, timeouts and errors, with no tokens.
- **Cache hits:** replies served from the disk cache, with 0 tokens.
- **Tokens:** as reported by each provider.

| Provider / model | Calls | Failed attempts | Cache hits | Input tokens | Output tokens | Total tokens |
|---|---:|---:|---:|---:|---:|---:|
| gemini/gemini-3.5-flash-lite | 371 | 0 | 442 | 1,627,431 | 115,395 | 1,742,826 |
| nvidia/minimaxai/minimax-m3.1-flash | 289 | 128 | 105 | 1,357,022 | 537,237 | 1,894,259 |
| nvidia/nvidia/nemotron-3-ultra-550b-a55b | 226 | 67 | 298 | 1,039,632 | 803,544 | 1,843,176 |
| gemini/gemini-3.1-flash-lite | 133 | 0 | 168 | 571,190 | 31,358 | 602,548 |
| groq/openai/gpt-oss-120b | 13 | 24 | 20 | 46,695 | 18,025 | 64,720 |
| **all** | **1,032** | 219 | 1,033 | 4,641,970 | 1,505,559 | **6,147,529** |

## Totals and per request

| | Value |
|---|---:|
| Requests | 518 |
| Model calls (live) | 1,032 |
| Input tokens | 4,641,970 |
| Output tokens | 1,505,559 |
| Total tokens | 6,147,529 |
| Average calls per request | 1.99 |
| **Average tokens per request** | **11,868** (8,961 input + 2,906 output) |

The evaluation report uses the same 11,868 tokens-per-request figure.

## Cost

All providers were used on their free tiers: NVIDIA API catalogue free credits, the Groq free plan and the Gemini API
free tier. **The actual cost of the run is $0.00 in total and $0.00 per request.**

### Hypothetical cost at list prices

The table below is **hypothetical**. It shows what the same tokens would cost on paid plans, at list prices looked up
on 2026-10-25, without caching or batch discounts. Nothing was billed.

| Provider / model | $ / 1M input | $ / 1M output | Hypothetical cost | Price source |
|---|---:|---:|---:|---|
| gemini/gemini-3.5-flash-lite | 0.30 | 2.50 | $0.78 | Google, ai.google.dev/gemini-api/docs/pricing (paid tier, standard) |
| nvidia/minimaxai/minimax-m3.1-flash | 0.15 | 0.60 | $0.53 | MiniMax list price as reported by resellers (OpenRouter, Novita) |
| nvidia/nvidia/nemotron-3-ultra-550b-a55b | 0.50 | 2.20 | $2.29 | OpenRouter listing; NVIDIA's own hosted endpoint is free, so this is a reference third-party price |
| gemini/gemini-3.1-flash-lite | 0.25 | 1.50 | $0.19 | Google, ai.google.dev/gemini-api/docs/pricing (paid tier, standard) |
| groq/openai/gpt-oss-120b | 0.15 | 0.60 | $0.02 | Groq on-demand, as reported by third-party pricing guides (Groq's page did not list it) |
| **all** | | | **$3.80** ($0.0073 per request) | |

The prices are in `LIST_PRICE` in `usage_summary.py`, which prints this table.

## Evidence extraction (not part of the run)

These calls were made once, while the evidence index was built. Their replies are cached, and a rebuild reuses them.

| Purpose | Model | Live calls | Tokens |
|---|---|---:|---:|
| PDF structuring (one call per PDF, after pypdf text extraction) | nemotron-3-ultra, minimax-m3.1-flash | 5 | 11,555 |
| Screenshot vision probes; the index uses local OCR (rapidocr, no model) for every screenshot | gemini-3.8-flash | 10 | 15,318 |

## For comparison (20 tuning cases, same method)

| Design | Calls / case | Tokens / case |
|---|---:|---:|
| Open-loop agent (m5_t3) | 8.85 | 71,824 |
| Bounded decision agent (b_t5, final design) | 2.05 | 14,162 |

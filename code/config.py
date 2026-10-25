"""Every tunable of the run in one place: model lanes, the enforced-verdict order, token caps, wait limits and retries.
The agent and the connector read lanes, limits and wait times from here.

Order = fallback order; a failing provider (error, rate limit, exhausted quota) falls to the next. Mistral is
intentionally absent: the key has a zero request quota and paid billing will not be enabled. Model ids were checked
against the live provider model lists (see decisions.md). Free tiers only.
"""

# Bounded agent lanes: cases are spread over these in parallel (weighted round-robin); the other usable lanes are the
# fallbacks. `max_request_tokens`: a lane is only used when the estimated request fits (Groq free tier: 8,000 tokens per
# minute, so one request must stay well below that). Gemini free quota is 20 requests/day per model; four models
# answered a live tool-calling probe on 2026-10-25.
LANES = [
    # rank = fallback order (1 strongest); the lite models answered worst in run b_t2 and are tried last
    # minimax first for the last 200 cases and the fallback re-run: 21/21 verdicts accepted vs 61% for nemotron (decisions.md)
    {"provider": "nvidia", "model": "minimaxai/minimax-m3.1-flash", "weight": 3, "rank": 1, "max_request_tokens": 60000},
    {"provider": "nvidia", "model": "nvidia/nemotron-3-ultra-550b-a55b", "weight": 2, "rank": 2, "max_request_tokens": 60000},
    {"provider": "gemini", "model": "gemini-3.6-flash", "weight": 1, "rank": 3, "max_request_tokens": 60000},
    {"provider": "gemini", "model": "gemini-3.5-flash", "weight": 1, "rank": 4, "max_request_tokens": 60000},
    {"provider": "groq", "model": "openai/gpt-oss-120b", "weight": 2, "rank": 5, "max_request_tokens": 5500},
    {"provider": "gemini", "model": "gemini-3.5-flash-lite", "weight": 1, "rank": 8, "max_request_tokens": 60000},
    {"provider": "gemini", "model": "gemini-3.1-flash-lite", "weight": 1, "rank": 9, "max_request_tokens": 60000},
]



def ranked_chain():
    """All lanes in fallback (rank) order: used for one-off structured calls such as PDF structuring."""
    return [(ln["provider"], ln["model"]) for ln in sorted(LANES, key=lambda ln: ln["rank"])]


# Schema-enforced verdicts (the forced verdict and the one retry): schema enforcement verified live on 2026-10-25 for
# nemotron (json_schema) and the Gemini lite models (responseSchema); Groq documents strict mode; minimax ignores the
# schema, so it is the last resort here
ENFORCED_ORDER = ["nvidia/nemotron-3-ultra-550b-a55b", "openai/gpt-oss-120b", "gemini-3.6-flash", "gemini-3.5-flash",
                  "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "minimaxai/minimax-m3.1-flash"]

# Bounded agent limits
MAX_ACTION_ROUNDS = 2          # action rounds before the verdict is forced
MAX_ACTIONS = 4                # actions per round
AGENT_MAX_TOKENS = 8000        # verdict JSON (~2k tokens) plus hidden reasoning; 4,000 was cut off (finish_reason=length)
AGENT_MAX_WAIT = 240           # seconds to wait for a cooled-down provider before a verdict call fails
TIEBREAK_MAX_TOKENS = 800      # intent tie-break reply
TIEBREAK_MAX_WAIT = 120

# Connector
GEMINI_MIN_OUTPUT = 16000      # Gemini's maxOutputTokens also counts hidden thinking tokens; small caps truncate answers
RETRY_DELAYS = (0, 15, 40)     # seconds before each attempt on one provider/model; only transient errors are retried
DEFAULT_COOLDOWN = 30          # seconds, when a 429 does not say how long to wait
PARK_ABOVE = 900               # a retry-after longer than this means the quota is gone for a long while: park the provider

# Images: Gemini vision, then local OCR only (no model). Every vision-derived ID is cross-checked
# against local OCR text (rapidocr) either way.
VISION_CHAIN = [("gemini", "gemini-3.8-flash")]

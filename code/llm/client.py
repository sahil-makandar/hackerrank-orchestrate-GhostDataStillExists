"""One connector for every provider: ordered fallback chain, tool calling, on-disk cache, rate limiting, usage log.
`chat` is the only entry point; keys come from the environment and are never logged.

  chat(chain, messages, tools=[...])  ->  {"text", "tool_calls", "message", "provider", "model", tokens, "cached", ...}

* Conversations use the OpenAI message format (system / user / assistant with tool_calls / tool). A conversation started
  on one provider can continue on another after a fallback; Gemini turns are rebuilt (and its raw parts, which carry
  thought signatures, are reused when the same provider answers again).
* A provider that errors, is rate limited, times out or runs out of quota is skipped and the next one is tried.
  Transient errors (5xx, timeouts) are retried a few times first; 429s put the provider on cooldown; daily quotas park
  it until the quota resets (persisted across runs); a model unknown to the account is dropped for the run.
* Every reply is cached on disk (keyed by provider, model, messages, tools, ...); a repeat call costs nothing.
* Every attempt writes a usage row (provider, model, tokens, ok/failed/cached, case id) for the usage report.
* Keys come from environment variables (or an uncommitted .env) and are never logged.
"""
import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import config
from typing import Any, Callable

CODE_DIR = Path(__file__).resolve().parents[1]
ROOT = CODE_DIR.parent
CACHE_DIR = CODE_DIR / "cache" / "llm"
USAGE_LOG = CODE_DIR / "cache" / "usage.jsonl"
STATE_FILE = CODE_DIR / "cache" / "provider_state.json"

PROVIDERS = {
    "nvidia": {"url": "https://integrate.api.nvidia.com/v1/chat/completions", "key": "NVIDIA_API_KEY",
               "style": "openai", "min_interval": 1.6, "max_in_flight": 3},   # 40 requests per minute
    "groq": {"url": "https://api.groq.com/openai/v1/chat/completions", "key": "GROQ_API_KEY",
             "style": "openai", "min_interval": 2.0},
    "gemini": {"url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
               "key": "GEMINI_API_KEY", "style": "gemini", "min_interval": 6.0},
}
GEMINI_FOREIGN_SIGNATURE = "skip_thought_signature_validator"   # for function calls produced by another provider
# tunables live in config.py; module-level names are kept because the connector tests patch them
GEMINI_MIN_OUTPUT = config.GEMINI_MIN_OUTPUT
RETRY_DELAYS = config.RETRY_DELAYS
TRANSIENT = ("HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504", "TimeoutError", "URLError", "ConnectionReset", "timed out")
DEFAULT_COOLDOWN = config.DEFAULT_COOLDOWN
PARK_ABOVE = config.PARK_ABOVE

CACHE_ONLY = os.environ.get("GHOST_CACHE_ONLY") == "1"   # serve cached replies only; never call a provider

# replaceable in tests
_sleep = time.sleep
_now = time.time

_lock = threading.RLock()   # guards the rate-limit slots and the usage log when cases run in parallel
_last_call = {}             # provider -> epoch seconds of its next free request slot
_cooldown = {}       # provider -> epoch seconds until which it is rate limited (memory only)
_parked = {}         # provider -> {"until": epoch, "reason": str}   (persisted: daily quotas survive restarts)
_dead_models = set()  # (provider, model) unknown to this account, dropped for the run


def _load_env():
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()


class LLMError(Exception):
    def __init__(self, message, status=None, retry_after=None, daily=False):
        super().__init__(message)
        self.status, self.retry_after, self.daily = status, retry_after, daily


# ----------------------------------------------------------------------------------------------- provider state
def load_state() -> None:
    _parked.clear()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        _parked.update({k: v for k, v in data.items() if v.get("until", 0) > _now()})
    except (OSError, ValueError):
        pass


def _save_state():
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(_parked), encoding="utf-8")
    except OSError:
        pass


def reset_state() -> None:
    """Forget cooldowns, parked providers and dead models (tests)."""
    _last_call.clear()
    _cooldown.clear()
    _parked.clear()
    _dead_models.clear()


PER_MODEL_QUOTA = {"gemini"}   # providers whose free quotas and rate limits are counted per model


def _park_key(provider, model):
    return f"{provider}:{model}" if provider in PER_MODEL_QUOTA and model else provider


def unavailable(provider: str, model: str | None = None) -> tuple | None:
    """(seconds until usable, reason) if the provider (or, for per-model quotas, this model) is cooling down or parked."""
    now = _now()
    for key in dict.fromkeys([provider, _park_key(provider, model)]):
        if key in _parked and _parked[key]["until"] > now:
            return _parked[key]["until"] - now, "parked: " + _parked[key]["reason"]
        if _cooldown.get(key, 0) > now:
            return _cooldown[key] - now, "rate limited, cooling down"
    return None


def _mark_failure(provider, model, err):
    if err.status == 404 or (err.status == 400 and re.search(r"model\W.{0,60}(not found|does not exist|decommissioned|unknown|not supported)",
                                                               str(err), re.I)):
        _dead_models.add((provider, model))        # this model id does not exist for this account
    elif err.status in (401, 403):
        _parked[provider] = {"until": _now() + 3600, "reason": f"HTTP {err.status} (key rejected)"}
        _save_state()
    elif err.daily or (err.retry_after and err.retry_after > PARK_ABOVE):
        _parked[_park_key(provider, model)] = {"until": _now() + (err.retry_after or 86400), "reason": "daily quota exhausted"}
        _save_state()
    elif err.status == 429:
        _cooldown[_park_key(provider, model)] = _now() + (err.retry_after or DEFAULT_COOLDOWN)


def provider_status() -> dict:
    """Human-readable provider availability, for run logs."""
    out = {}
    for p in PROVIDERS:
        u = unavailable(p)
        out[p] = "ok" if u is None else f"{u[1]} ({u[0]:.0f}s)"
    for key, v in _parked.items():
        if ":" in key and v["until"] > _now():
            out[key] = f"parked: {v['reason']} ({v['until'] - _now():.0f}s)"
    return out


load_state()


# ---------------------------------------------------------------------------------------------------- transport
def _key(provider):
    k = os.environ.get(PROVIDERS[provider]["key"])
    if not k:
        raise LLMError(f"{provider}: API key not set", status=401)
    return k


def _wait(provider):
    """Shared rate limiter: every thread reserves the provider's next free slot under a lock, then sleeps outside it,
    so parallel cases together never exceed the provider's request rate."""
    gap = PROVIDERS[provider]["min_interval"]
    with _lock:
        slot = max(_now(), _last_call.get(provider, 0))
        _last_call[provider] = slot + gap      # next free slot for this provider
    delay = slot - _now()
    if delay > 0:
        _sleep(delay)


def _retry_after(header, body):
    """Seconds the provider asks us to wait, from the Retry-After header or the error body."""
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', body)
    if m:
        return float(m.group(1))
    m = re.search(r"try again in (?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", body, re.I)
    if m and (m.group(1) or m.group(2)):
        return int(m.group(1) or 0) * 60 + float(m.group(2) or 0)
    return None


def use_test_doubles(*, post: Callable, sleep: Callable, now: Callable, cache_dir: Path, usage_log: Path, state_file: Path) -> None:
    """Replace the network, the clock and the on-disk locations. For the offline connector tests only."""
    global _post, _sleep, _now, CACHE_DIR, USAGE_LOG, STATE_FILE, CACHE_ONLY
    _post, _sleep, _now, CACHE_ONLY = post, sleep, now, False
    CACHE_DIR, USAGE_LOG, STATE_FILE = cache_dir, usage_log, state_file


def _post(url, headers, body, timeout=180):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "ghost-audit/0.1", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        rpm = e.headers.get("x-ratelimit-limit-req-minute")
        daily = bool(re.search(r"per ?day|PerDay|daily|\bTPD\b", raw, re.I)) or rpm == "0"
        raise LLMError(f"HTTP {e.code} [rpm_limit={rpm}]: {raw[:200]}", status=e.code,
                       retry_after=_retry_after(e.headers.get("Retry-After"), raw), daily=daily)
    except Exception as e:  # timeouts, connection resets
        raise LLMError(f"{type(e).__name__}: {str(e)[:200]}")


# ------------------------------------------------------------------------------------------------- cache / usage
def _cache_path(parts):
    h = hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    return CACHE_DIR / f"{h}.json"


def _log_usage(row):
    USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with _lock, USAGE_LOG.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"ts": _now(), "run": os.environ.get("GHOST_RUN_ID", "dev"), **row}, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------------------------- message adapters
def tool_result_message(call: dict, content: Any) -> dict:
    """The message that answers one tool call (keeps the name: Gemini needs it)."""
    return {"role": "tool", "tool_call_id": call["id"], "name": call["name"],
            "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)}


def _strip(messages):
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]


def _with_image(messages, image):
    """Attach an image to the last user message in OpenAI-compatible vision format."""
    if not image:
        return messages
    data = "data:%s;base64,%s" % (image[1], base64.b64encode(image[0]).decode())
    last = dict(messages[-1])
    last["content"] = [{"type": "text", "text": last["content"]}, {"type": "image_url", "image_url": data}]
    return messages[:-1] + [last]


def _parse_openai_calls(raw):
    calls = []
    for i, tc in enumerate(raw or []):
        fn = tc.get("function", {})
        args, err = None, None
        try:
            args = json.loads(fn.get("arguments") or "{}")
            if not isinstance(args, dict):
                args, err = None, "arguments must be a JSON object"
        except ValueError as e:
            err = f"arguments are not valid JSON: {e}"
        calls.append({"id": tc.get("id") or f"call_{i}", "name": fn.get("name", ""), "arguments": args,
                      "arguments_error": err})
    return calls


def _openai_call(provider, model, messages, tools, tool_choice, json_mode, max_tokens, image):
    cfg = PROVIDERS[provider]
    body = {"model": model, "messages": _with_image(_strip(messages), image), "max_tokens": max_tokens, "temperature": 0}
    if tools:
        body["tools"] = [{"type": "function", "function": t} for t in tools]
        body["tool_choice"] = tool_choice
    elif isinstance(json_mode, dict):          # schema enforced by the provider (constrained decoding / validation)
        # OpenAI-style structured outputs (Groq strict mode; NVIDIA's hosted API rejects the NIM `nvext.guided_json`
        # field with "unknown field", so the standard json_schema response format is used there too)
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "verdict", "strict": True, "schema": json_mode}}
    elif json_mode:
        body["response_format"] = {"type": "json_object"}
    r = _post(cfg["url"], {"Authorization": "Bearer " + _key(provider)}, body)
    choice = r["choices"][0]
    msg = choice["message"]
    text = msg.get("content") or ""
    raw_calls = msg.get("tool_calls") or []
    if not text.strip() and not raw_calls:
        raise LLMError(f"empty response (finish_reason={choice.get('finish_reason')})")
    u = r.get("usage", {})
    message = {"role": "assistant", "content": text or None}
    if raw_calls:
        message["tool_calls"] = raw_calls
    return {"text": text, "tool_calls": _parse_openai_calls(raw_calls), "message": message,
            "input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)}


def _gemini_schema(node):
    """JSON schema -> the subset Gemini accepts (upper-case types, no additionalProperties / $schema / default)."""
    if isinstance(node, list):
        return [_gemini_schema(n) for n in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for k, v in node.items():
        if k in ("additionalProperties", "$schema", "default", "title"):
            continue
        if k == "type" and isinstance(v, list):
            out["type"] = next((t for t in v if t != "null"), "string").upper()
            out["nullable"] = "null" in v
        else:
            out[k] = v.upper() if k == "type" and isinstance(v, str) else _gemini_schema(v)
    return out


def _to_gemini(messages):
    """OpenAI-format conversation -> (system text, Gemini contents)."""
    system, contents, pending = [], [], []

    def flush():
        if pending:
            contents.append({"role": "user", "parts": list(pending)})
            pending.clear()

    for m in messages:
        role = m["role"]
        if role == "system":
            system.append(m["content"])
        elif role == "tool":
            pending.append({"functionResponse": {"name": m.get("name", ""), "response": {"result": m["content"]}}})
        else:
            flush()
            if role == "user":
                contents.append({"role": "user", "parts": [{"text": m["content"] if isinstance(m["content"], str) else str(m["content"])}]})
            else:
                parts = m.get("_gemini_parts")
                if not parts:
                    parts = [{"text": m["content"]}] if m.get("content") else []
                    for tc in m.get("tool_calls", []):
                        parts.append({"functionCall": {"name": tc["function"]["name"],
                                                       "args": json.loads(tc["function"].get("arguments") or "{}")},
                                      "thoughtSignature": GEMINI_FOREIGN_SIGNATURE})
                contents.append({"role": "model", "parts": parts})
    flush()
    return "\n\n".join(system), contents


def _gemini_call(model, messages, tools, json_mode, max_tokens, image, tool_choice="auto"):
    system, contents = _to_gemini(messages)
    if image:
        contents[-1]["parts"].append({"inline_data": {"mime_type": image[1], "data": base64.b64encode(image[0]).decode()}})
    body = {"contents": contents,
            "generationConfig": {"temperature": 0, "maxOutputTokens": max(max_tokens, GEMINI_MIN_OUTPUT)}}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if tools:
        body["tools"] = [{"functionDeclarations": [{"name": t["name"], "description": t.get("description", ""),
                                                    "parameters": _gemini_schema(t["parameters"])} for t in tools]}]
        if isinstance(tool_choice, dict):     # a forced call: only that function may be used
            body["toolConfig"] = {"functionCallingConfig": {"mode": "ANY",
                                                            "allowedFunctionNames": [tool_choice["function"]["name"]]}}
    elif json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
        if isinstance(json_mode, dict):        # response schema enforced by Gemini
            body["generationConfig"]["responseSchema"] = _gemini_schema(json_mode)
    r = _post(PROVIDERS["gemini"]["url"].format(model=model), {"x-goog-api-key": _key("gemini")}, body)
    cand = r["candidates"][0]
    parts = cand.get("content", {}).get("parts", [])
    if cand.get("finishReason") == "MAX_TOKENS":
        raise LLMError("gemini output truncated (finishReason=MAX_TOKENS)")
    text = "".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought"))
    calls, raw_calls = [], []
    for i, p in enumerate(parts):
        if "functionCall" in p:
            fc = p["functionCall"]
            cid = f"call_{i}"
            calls.append({"id": cid, "name": fc["name"], "arguments": dict(fc.get("args") or {}), "arguments_error": None})
            raw_calls.append({"id": cid, "type": "function",
                              "function": {"name": fc["name"], "arguments": json.dumps(fc.get("args") or {}, ensure_ascii=False)}})
    if not text and not calls:
        raise LLMError(f"gemini returned no text (finishReason={cand.get('finishReason')})")
    u = r.get("usageMetadata", {})
    message = {"role": "assistant", "content": text or None, "_gemini_parts": parts}
    if raw_calls:
        message["tool_calls"] = raw_calls
    return {"text": text, "tool_calls": calls, "message": message, "input_tokens": u.get("promptTokenCount", 0),
            "output_tokens": u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)}


# --------------------------------------------------------------------------------------------------- the chain
_in_flight = {}   # provider -> semaphore limiting concurrent requests


def _slot(provider):
    with _lock:
        if provider not in _in_flight:
            _in_flight[provider] = threading.BoundedSemaphore(PROVIDERS[provider].get("max_in_flight", 2))
        return _in_flight[provider]


def _attempt(provider, model, messages, tools, tool_choice, image, json_mode, max_tokens):
    with _slot(provider):          # at most max_in_flight requests per provider at once, across all case threads
        _wait(provider)
        if PROVIDERS[provider]["style"] == "gemini":
            return _gemini_call(model, messages, tools, json_mode, max_tokens, image, tool_choice)
        return _openai_call(provider, model, messages, tools, tool_choice, json_mode, max_tokens, image)


def _attempt_retrying(provider, model, messages, tools, tool_choice, image, json_mode, max_tokens, tag, case_id):
    """One provider/model: retry only transient failures; quota, key and format errors go straight to the next one."""
    err = LLMError("no attempt made")
    for delay in RETRY_DELAYS:
        _sleep(delay) if delay else None
        try:
            res = _attempt(provider, model, messages, tools, tool_choice, image, json_mode, max_tokens)
            if not res["text"].strip() and not res["tool_calls"]:
                raise LLMError("empty response")
            return res
        except (LLMError, KeyError, IndexError, TypeError, ValueError) as e:   # a malformed reply is a failure too
            err = e if isinstance(e, LLMError) else LLMError(f"bad response shape: {type(e).__name__} {e}")
            _log_usage({"tag": tag, "case_id": case_id, "provider": provider, "model": model, "cached": False,
                        "input_tokens": 0, "output_tokens": 0, "ok": False, "error": str(err)[:120]})
            if err.status is not None or not any(t in str(err) for t in TRANSIENT):
                break
    raise err


def chat(chain: list, messages: list, *, tools: list | None = None, tool_choice: Any = "auto",
         image: tuple | None = None, json_mode: Any = False, max_tokens: int = 2000, tag: str = "",
         case_id: str = "", max_wait: float = 0) -> dict:
    """Ask the first usable (provider, model) of `chain`; fall through on any failure.

    `tools`: [{"name", "description", "parameters": <JSON schema>}]. `max_wait`: if every provider is cooling down but
    one will be back within this many seconds, wait for it instead of failing.
    """
    img_hash = hashlib.sha256(image[0]).hexdigest() if image else ""
    deadline = _now() + (max_wait or 0)
    history = []                          # every failure of every round, for the error message
    while True:
        failures, waits = [], []
        for provider, model in chain:
            key = [provider, model, messages, img_hash, json_mode, max_tokens]   # unchanged when no tools: keeps old entries valid
            cp = _cache_path(key + [tools, tool_choice] if tools else key)
            if cp.exists():
                hit = json.loads(cp.read_text(encoding="utf-8"))
                hit.setdefault("tool_calls", [])
                hit.setdefault("message", {"role": "assistant", "content": hit.get("text")})
                _log_usage({"tag": tag, "case_id": case_id, "provider": provider, "model": model, "cached": True,
                            "input_tokens": 0, "output_tokens": 0, "ok": True})
                return {**hit, "cached": True, "failures": failures}
            if (provider, model) in _dead_models:
                failures.append({"provider": provider, "model": model, "error": "skipped: model unknown to this account"})
                continue
            wait = unavailable(provider, model)
            if wait:
                waits.append(wait[0])
                failures.append({"provider": provider, "model": model, "error": "skipped: " + wait[1]})
                continue
            if CACHE_ONLY:
                failures.append({"provider": provider, "model": model, "error": "cache-only mode: no cached reply"})
                continue
            t0 = _now()
            try:
                res = _attempt_retrying(provider, model, messages, tools, tool_choice, image, json_mode, max_tokens, tag, case_id)
            except LLMError as e:
                failures.append({"provider": provider, "model": model, "error": str(e)})
                _mark_failure(provider, model, e)
                back = unavailable(provider, model)   # a 429 just put it on cooldown: it is worth waiting for
                if back:
                    waits.append(back[0])
                continue
            out = {**res, "provider": provider, "model": model}
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
            _log_usage({"tag": tag, "case_id": case_id, "provider": provider, "model": model, "cached": False,
                        "input_tokens": res["input_tokens"], "output_tokens": res["output_tokens"], "ok": True,
                        "tool_calls": len(res["tool_calls"]), "latency_s": round(_now() - t0, 2)})
            return {**out, "cached": False, "failures": failures}
        history += failures
        # every usable provider failed or is cooling down: wait for the first to come back, while the deadline allows
        if waits and max_wait and _now() + min(waits) <= deadline:
            _sleep(min(waits) + 0.5)
            continue
        break
    raise LLMError("all providers failed: " + json.dumps(history[-12:]))

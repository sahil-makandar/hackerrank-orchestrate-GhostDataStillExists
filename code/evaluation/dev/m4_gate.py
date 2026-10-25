"""M4 gate: the LLM connector.

Part A (offline, fake network, fake clock): every failure path and the bookkeeping around it.
Part B (live, tiny, cached): a real bad model id falls through to a real provider; a real tool-calling round trip on every
provider that currently has quota. Gemini is exercised offline only while its free daily quota is exhausted.

  python code/evaluation/m4_gate.py          # part B calls are cached: reruns cost nothing
  python code/evaluation/m4_gate.py --offline
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config                       # noqa: E402
from llm import client              # noqa: E402

ok = True
REAL = {k: getattr(client, k) for k in ("CACHE_DIR", "USAGE_LOG", "STATE_FILE", "_post", "_sleep", "_now", "CACHE_ONLY")}


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


class Clock:
    def __init__(self):
        self.t = 1_000_000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class FakeNet:
    """Scripted transport: handlers[provider](body) returns a JSON reply or raises LLMError."""

    def __init__(self):
        self.handlers, self.calls = {}, []

    def __call__(self, url, headers, body, timeout=180):
        provider = "nvidia" if "nvidia" in url else "groq" if "groq" in url else "gemini"
        self.calls.append((provider, body))
        return self.handlers[provider](body)

    def count(self, provider):
        return sum(1 for p, _ in self.calls if p == provider)


def oa_ok(text="ok", calls=None, pt=10, ct=5):
    msg = {"content": text}
    if calls:
        msg["tool_calls"] = [{"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": a}}
                             for i, (n, a) in enumerate(calls)]
    return {"choices": [{"message": msg}], "usage": {"prompt_tokens": pt, "completion_tokens": ct}}


def gem_ok(text="ok", calls=None):
    parts = ([{"text": text}] if text else []) + [{"functionCall": {"name": n, "args": a}} for n, a in (calls or [])]
    return {"candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3}}


def raiser(err):
    def h(body):
        raise err
    return h


def usage_rows():
    p = client.USAGE_LOG
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


TOOLS = [{"name": "get_user_id", "description": "Resolve an email to a user id.",
          "parameters": {"type": "object", "properties": {"email": {"type": "string"}}, "required": ["email"],
                         "additionalProperties": False}}]
NV, GQ, GM = ("nvidia", "nv-model"), ("groq", "gq-model"), ("gemini", "gm-model")


def part_a():
    print("--- Part A: offline failure paths (fake network, fake clock) ---")
    tmp = Path(tempfile.mkdtemp(prefix="m4_gate_"))
    clock, net = Clock(), FakeNet()
    client.use_test_doubles(post=net, sleep=clock.sleep, now=clock.now, cache_dir=tmp / "llm",
                            usage_log=tmp / "usage.jsonl", state_file=tmp / "state.json")
    saved_env = {v: os.environ.get(v) for v in ("NVIDIA_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY")}
    for var in saved_env:
        os.environ[var] = "dummy"          # the fake network never sees a real key
    msgs = lambda n: [{"role": "user", "content": f"question {n}"}]
    n = [0]

    def fresh(chain, **kw):
        n[0] += 1                              # unique prompt so earlier cache entries never interfere
        return client.chat(chain, msgs(n[0]), **kw)

    def reset():
        client.reset_state()
        net.calls.clear()
        net.handlers = {"nvidia": lambda b: oa_ok("from nvidia"), "groq": lambda b: oa_ok("from groq"),
                        "gemini": lambda b: gem_ok("from gemini")}

    # 1. bad model id (404) -> next provider; the dead model is not asked again
    reset()
    net.handlers["nvidia"] = raiser(client.LLMError("HTTP 404 Function not found for account", status=404))
    r = fresh([NV, GQ])
    check("bad model id: falls through to the next provider", r["provider"] == "groq" and r["failures"][0]["provider"] == "nvidia")
    c = net.count("nvidia")
    fresh([NV, GQ])
    check("bad model id: the dead model is not asked again", net.count("nvidia") == c)

    # 2. 429 with Retry-After -> next provider, cooldown honoured, then used again
    reset()
    net.handlers["nvidia"] = raiser(client.LLMError("HTTP 429", status=429, retry_after=20))
    r = fresh([NV, GQ])
    check("429: falls through to the next provider", r["provider"] == "groq")
    c = net.count("nvidia")
    clock.t += 5
    fresh([NV, GQ])
    check("429: provider skipped while cooling down (no request made)", net.count("nvidia") == c)
    clock.t += 30
    net.handlers["nvidia"] = lambda b: oa_ok("nvidia is back")
    r = fresh([NV, GQ])
    check("429: provider used again after the cooldown", r["provider"] == "nvidia" and r["text"] == "nvidia is back")

    # 3. daily quota -> parked, persisted across a restart, released when the quota resets
    reset()
    net.handlers["gemini"] = raiser(client.LLMError("HTTP 429 PerDay", status=429, retry_after=49883, daily=True))
    r = fresh([GM, NV])
    check("daily quota: falls through to the next provider", r["provider"] == "nvidia")
    client.reset_state()
    client.load_state()
    check("daily quota: parked state survives a restart", client.unavailable("gemini", "gm-model") is not None)
    check("daily quota on Gemini parks that model only, not the other Gemini models",
          client.unavailable("gemini", "another-model") is None)
    clock.t += 50_000
    client.reset_state()
    client.load_state()
    check("daily quota: provider released once the quota has reset", client.unavailable("gemini", "gm-model") is None)

    # 4. timeout -> transient: retried with the configured delays, then next provider
    reset()
    net.handlers["nvidia"] = raiser(client.LLMError("TimeoutError: The read operation timed out"))
    clock.slept.clear()
    r = fresh([NV, GQ])
    check("timeout: retried then falls through", r["provider"] == "groq" and net.count("nvidia") == len(client.RETRY_DELAYS),
          f"attempts={net.count('nvidia')}")
    check("timeout: back-off delays were applied", [d for d in clock.slept if d >= 15][:2] == [15, 40])

    # 5. malformed reply -> next provider, no pointless retries
    reset()
    net.handlers["nvidia"] = lambda b: {"choices": []}
    r = fresh([NV, GQ])
    check("malformed reply: falls through without retrying", r["provider"] == "groq" and net.count("nvidia") == 1)

    # 6. every provider fails
    reset()
    for p in net.handlers:
        net.handlers[p] = raiser(client.LLMError("HTTP 503 overloaded", status=503))
    try:
        fresh([NV, GQ, GM])
        raised, text = False, ""
    except client.LLMError as e:
        raised, text = True, str(e)
    check("all providers failing raises LLMError naming every provider",
          raised and all(p in text for p in ("nvidia", "groq", "gemini")))

    # 7. cache + usage rows
    reset()
    net.handlers["nvidia"] = lambda b: oa_ok("cached answer", pt=123, ct=45)
    rows0 = len(usage_rows())
    m = [{"role": "user", "content": "the same question"}]
    r1 = client.chat([NV, GQ], m, tag="t", case_id="case_x")
    c = len(net.calls)
    r2 = client.chat([NV, GQ], m, tag="t", case_id="case_x")
    check("repeat call is served from the cache with no request", len(net.calls) == c and r2["cached"] and r2["text"] == r1["text"])
    new = usage_rows()[rows0:]
    check("usage rows: live call carries tokens and case id, repeat call is cached with 0 tokens",
          new[0]["input_tokens"] == 123 and new[0]["output_tokens"] == 45 and new[0]["case_id"] == "case_x" and not new[0]["cached"]
          and new[1]["cached"] and new[1]["input_tokens"] == 0 and new[1]["output_tokens"] == 0,
          str([(r["cached"], r["input_tokens"]) for r in new]))
    reset()
    net.handlers["nvidia"] = raiser(client.LLMError("HTTP 404 nope", status=404))
    rows0 = len(usage_rows())
    fresh([NV, GQ], case_id="case_y")
    new = usage_rows()[rows0:]
    check("usage rows: failed attempts are logged too (provider, model, error)",
          any(not r["ok"] and r["provider"] == "nvidia" and r["error"] for r in new) and any(r["ok"] and r["provider"] == "groq" for r in new))

    # 8. tool calling, OpenAI style
    reset()
    net.handlers["nvidia"] = lambda b: oa_ok(None, calls=[("get_user_id", '{"email": "a@b.com"}'), ("get_user_id", "{not json")])
    r = fresh([NV], tools=TOOLS)
    sent = net.calls[-1][1]
    check("tool call: schema is sent as an OpenAI function tool", sent["tools"][0]["function"]["name"] == "get_user_id" and sent["tool_choice"] == "auto")
    check("tool call: arguments are parsed to a dict", r["tool_calls"][0]["arguments"] == {"email": "a@b.com"} and r["tool_calls"][0]["name"] == "get_user_id")
    check("tool call: invalid JSON arguments are reported, not raised",
          r["tool_calls"][1]["arguments"] is None and "not valid JSON" in r["tool_calls"][1]["arguments_error"])
    check("tool call: the assistant message can be appended to the history as is",
          r["message"]["role"] == "assistant" and len(r["message"]["tool_calls"]) == 2)

    # 9. Gemini adapter: conversation conversion + function calling
    reset()
    net.handlers["gemini"] = lambda b: gem_ok(None, calls=[("get_user_id", {"email": "c@d.com"})])
    history = [{"role": "system", "content": "You audit deletions."}, {"role": "user", "content": "Start."}]
    r = client.chat([GM], history, tools=TOOLS)
    body = net.calls[-1][1]
    check("gemini: system prompt goes to systemInstruction", body["systemInstruction"]["parts"][0]["text"] == "You audit deletions.")
    decl = body["tools"][0]["functionDeclarations"][0]
    check("gemini: tool schema is sanitised (upper-case types, no additionalProperties)",
          decl["parameters"]["type"] == "OBJECT" and "additionalProperties" not in decl["parameters"]
          and decl["parameters"]["properties"]["email"]["type"] == "STRING")
    check("gemini: functionCall parsed into a normalised tool call", r["tool_calls"][0]["arguments"] == {"email": "c@d.com"})
    history += [r["message"], client.tool_result_message(r["tool_calls"][0], {"user_id": "user_9"})]
    client.chat([GM], history, tools=TOOLS)
    contents = net.calls[-1][1]["contents"]
    check("gemini: tool result is sent back as a user functionResponse and the raw model parts are replayed",
          [c["role"] for c in contents] == ["user", "model", "user"] and "functionResponse" in contents[2]["parts"][0]
          and contents[1]["parts"] == r["message"]["_gemini_parts"])

    # 10. a conversation started on an OpenAI-style provider continues on Gemini after a fallback
    reset()
    net.handlers["nvidia"] = lambda b: oa_ok(None, calls=[("get_user_id", '{"email": "e@f.com"}')])
    first = client.chat([NV], [{"role": "user", "content": "Find e@f.com"}], tools=TOOLS)
    hist = [{"role": "user", "content": "Find e@f.com"}, first["message"], client.tool_result_message(first["tool_calls"][0], "user_3")]
    net.handlers["nvidia"] = raiser(client.LLMError("HTTP 429", status=429, retry_after=60))
    r = client.chat([NV, GM], hist, tools=TOOLS)
    contents = net.calls[-1][1]["contents"]
    check("fallback mid-conversation: OpenAI-style history is rebuilt for Gemini",
          r["provider"] == "gemini" and contents[1]["parts"][0]["functionCall"]["args"] == {"email": "e@f.com"}
          and contents[2]["parts"][0]["functionResponse"]["name"] == "get_user_id")

    # 11. every provider cooling down: wait if it is short, fail otherwise
    reset()
    net.handlers["nvidia"] = raiser(client.LLMError("HTTP 429", status=429, retry_after=10))
    net.handlers["groq"] = raiser(client.LLMError("HTTP 429", status=429, retry_after=10))
    try:
        fresh([NV, GQ])
    except client.LLMError:
        pass
    net.handlers["nvidia"] = lambda b: oa_ok("after waiting")
    try:
        fresh([NV, GQ])
        refused = False
    except client.LLMError:
        refused = True
    check("all cooling down, max_wait=0: fails fast", refused)
    r = fresh([NV, GQ], max_wait=30)
    check("all cooling down, max_wait=30: waits for the first provider and succeeds", r["text"] == "after waiting")
    # several cooldowns in a row (other threads keep hitting 429) still succeed while the deadline allows
    reset()
    hits = {"n": 0}

    def flaky(body):
        hits["n"] += 1
        if hits["n"] <= 4:
            raise client.LLMError("HTTP 429", status=429, retry_after=10)
        return oa_ok("finally")
    net.handlers["nvidia"] = flaky
    r = fresh([NV], max_wait=120)
    check("repeated cooldowns: keeps waiting until the deadline and then succeeds", r["text"] == "finally", f"attempts={hits['n']}")
    reset()
    net.handlers["nvidia"] = raiser(client.LLMError("HTTP 429", status=429, retry_after=10))
    try:
        fresh([NV], max_wait=25)
        msg = ""
    except client.LLMError as e:
        msg = str(e)
    check("deadline passed: the error keeps the failure history (not an empty list)", "429" in msg and "[]" not in msg, msg[:80])

    # 12. cache-only mode serves the cache and refuses everything else
    reset()
    net.handlers["nvidia"] = lambda b: oa_ok("only once")
    m = [{"role": "user", "content": "cache only question"}]
    client.chat([NV], m)
    client.CACHE_ONLY = True
    c = len(net.calls)
    cached = client.chat([NV], m)
    try:
        client.chat([NV], [{"role": "user", "content": "never seen"}])
        blocked = False
    except client.LLMError:
        blocked = True
    client.CACHE_ONLY = False
    check("cache-only mode: cached reply served, uncached call refused, no request made", cached["cached"] and blocked and len(net.calls) == c)

    # 13. per-provider spacing (rate limit)
    reset()
    clock.slept.clear()
    fresh([NV])
    t1 = clock.t
    fresh([NV])
    check("rate limit: consecutive calls to one provider are spaced by its minimum interval",
          clock.t - t1 >= client.PROVIDERS["nvidia"]["min_interval"] - 1e-6, f"{clock.t - t1:.2f}s")

    for k, v in REAL.items():
        setattr(client, k, v)
    for var, val in saved_env.items():      # put the real keys back before any live call
        if val is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = val
    client.reset_state()
    client.load_state()


def part_b():
    print("\n--- Part B: live providers (small, cached) ---")
    if client.CACHE_ONLY:
        print("SKIP live part (cache-only mode)")
        return
    # real bad model id falls through to a real provider
    try:
        r = client.chat([("nvidia", "nvidia/this-model-does-not-exist"), config.ranked_chain()[-1]],
                        [{"role": "user", "content": "Reply with the single word: ok"}], max_tokens=200, tag="m4_gate", case_id="m4")
        check("live: a nonexistent model id falls through to the next real provider",
              r["provider"] == config.ranked_chain()[-1][0] and r["failures"] and r["failures"][0]["provider"] == "nvidia",
              f'{r["provider"]}/{r["model"]}')
    except client.LLMError as e:
        check("live: a nonexistent model id falls through to the next real provider", False, str(e)[:160])

    prompt = [{"role": "system", "content": "You are a careful assistant. Always use the provided tool when asked to look something up."},
              {"role": "user", "content": "Look up the user id of the email addr@example.com using the tool, then tell me the id."}]
    print("   provider status:", client.provider_status())
    for provider, model in config.ranked_chain():
        label = f"live tool round trip on {provider}/{model}"
        if client.unavailable(provider, model):
            print(f"SKIP {label}: provider currently unavailable ({client.provider_status()[provider]}); adapter covered offline")
            continue
        try:
            r1 = client.chat([(provider, model)], prompt, tools=TOOLS, max_tokens=800, tag="m4_gate", case_id="m4")
            call = r1["tool_calls"][0] if r1["tool_calls"] else None
            good_call = bool(call) and call["name"] == "get_user_id" and (call["arguments"] or {}).get("email", "").lower() == "addr@example.com"
            r2 = client.chat([(provider, model)], prompt + [r1["message"], client.tool_result_message(call, {"user_id": "user_7"})],
                             tools=TOOLS, max_tokens=800, tag="m4_gate", case_id="m4") if good_call else {"text": ""}
            check(label, good_call and "user_7" in r2["text"], f'call={call and call["arguments"]} answer={r2["text"][:60]!r}')
        except client.LLMError as e:
            if client.unavailable(provider, model):      # the call itself hit a daily quota: not a defect of the connector
                print(f"SKIP {label}: {client.provider_status()[provider]}")
            else:
                check(label, False, str(e)[:200])

    # a repeat of a live call is free
    rows0 = len(usage_rows())
    try:
        client.chat([config.ranked_chain()[0]], prompt, tools=TOOLS, max_tokens=800, tag="m4_gate", case_id="m4")
        new = usage_rows()[rows0:]
        check("live: a repeated call is served from the cache with 0 tokens",
              bool(new) and all(r["cached"] and r["input_tokens"] == 0 for r in new))
    except client.LLMError as e:
        check("live: a repeated call is served from the cache with 0 tokens", False, str(e)[:160])


def main():
    part_a()
    if "--offline" not in sys.argv:
        part_b()
    print("\nM4 GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

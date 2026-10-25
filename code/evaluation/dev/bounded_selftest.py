"""Bounded agent self-test with a scripted fake model (no quota used).

  python code/evaluation/bounded_selftest.py

Checks: a direct verdict is accepted in 1 call; actions then verdict take 2 calls; after 2 action rounds the next call is
a schema-enforced verdict; a rejected verdict gets exactly one (schema-enforced) retry, then the baseline; no case ever
exceeds MAX_CALLS; the enforced schema allows one account only and names only refs / lines of this case file; refusals
made with refuse_lead reach the verdict; the intent safety net (both directions, agreement, no tie-breaker); the case
file carries no derived state.
"""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("GHOST_CACHE_ONLY", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import baseline                       # noqa: E402
import split                          # noqa: E402
from agent import bounded, casefile   # noqa: E402
from agent.tools import ToolBox       # noqa: E402
from llm import client                # noqa: E402
from m5_selftest import payload_from  # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


class Script:
    """Fake client.chat: replays scripted replies; records whether each call was a tool call or a schema call."""

    def __init__(self, steps):
        self.steps, self.seen = list(steps), []

    def __call__(self, chain, msgs, tools=None, tool_choice="auto", json_mode=False, **kw):
        self.seen.append("schema" if isinstance(json_mode, dict) else ("json" if json_mode else "tools"))
        step = self.steps.pop(0) if self.steps else ("text", "thinking")
        calls = [] if step[0] == "text" else [{"id": f"c{len(self.seen)}_{i}", "name": n, "arguments": a, "arguments_error": None}
                                              for i, (n, a) in enumerate(step[1])]
        raw = [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
               for c in calls]
        msg = {"role": "assistant", "content": None if calls else step[1]}
        if raw:
            msg["tool_calls"] = raw
        return {"text": "" if calls else step[1], "tool_calls": calls, "message": msg, "provider": "fake",
                "model": "scripted", "input_tokens": 100, "output_tokens": 10, "cached": False, "failures": []}


def to_decisions(cf, payload):
    """A records-style payload as reference decisions on this case file (test helper)."""
    by_rid = {}
    for ref, (rid, _s, lines) in casefile.resolve(cf).items():
        by_rid.setdefault(rid, (ref, lines))
    decisions = []
    for r in payload["records"]:
        ref, lines = by_rid[r["resource_id"]]
        line = next((k for k, v in lines.items() if v == r["evidence_id"]), next(iter(lines), ""))
        decisions.append({"ref": ref, "decision": "include", "category": r["data_category"], "state": r["state_at_audit"],
                          "evidence": line})
    for r in payload.get("refused_candidates") or []:
        if r["resource_id"] in by_rid:
            decisions.append({"ref": by_rid[r["resource_id"]][0], "decision": "refuse", "reason": r.get("reason", "")})
    return {"authorises_deletion": True, "intent_reason": "self-test", "decisions": decisions}


def to_enforced(cf, verdict):
    """The same verdict in the schema-enforced shape (account / records / ambiguous)."""
    tiers = {r["ref"]: r["tier"] for r in cf["records"]}
    rid_of = {ref: v[0] for ref, v in casefile.resolve(cf).items()}
    out = {"authorises_deletion": True, "intent_reason": "self-test", "notes": "",
           "account": {"ref": "none", "state": "ABSENT", "evidence": "none"}, "records": [], "ambiguous": []}
    for d in verdict["decisions"]:
        if tiers.get(d["ref"]) == "timing-ambiguous":
            out["ambiguous"].append({"ref": d["ref"], "decision": d["decision"], "category": d.get("category", "none"),
                                     "state": d.get("state", "none"), "evidence": d.get("evidence", "none"),
                                     "link_evidence": "none", "reason": d.get("reason", "")})
        elif d["decision"] == "include" and rid_of[d["ref"]].startswith("account_"):
            out["account"] = {"ref": d["ref"], "state": d["state"], "evidence": d["evidence"]}
        elif d["decision"] == "include":
            out["records"].append({"ref": d["ref"], "category": d["category"], "state": d["state"], "evidence": d["evidence"]})
    return json.dumps(out)


def main():
    ctx = baseline.Context()
    tuning = split.tuning_cases()
    cid = next(c for c in tuning if baseline.assess(ctx, ctx.by_id[c])["status"] == "INCOMPLETE"
               and not baseline.assess(ctx, ctx.by_id[c]).get("unresolved")
               and any(x.tier == "timing-ambiguous" for x in ToolBox(ctx, ctx.by_id[c]).candidates_for(ctx.user[c]).values()))
    case = ctx.by_id[cid]
    b = baseline.assess(ctx, case)
    tb = ToolBox(ctx, case)
    cf = casefile.build(ToolBox(ctx, case), ctx.user[cid])
    good = to_decisions(cf, payload_from(ctx, cid, b["items"], tb))
    good_enf = to_enforced(cf, good)
    ambiguous = [d["ref"] for d in good["decisions"] if d["decision"] == "refuse"]
    bad = dict(good, decisions=good["decisions"] + [{"ref": "R999", "decision": "include", "category": "X",
                                                     "state": "ABSENT", "evidence": "R999.e1"}])
    real_chat = client.chat
    run = lambda c=case: bounded.run_case(ctx, c, [("fake", "scripted")], "bounded_selftest")
    try:
        client.chat = Script([("calls", [("submit_verdict", good)])])
        r = run()
        check("direct verdict accepted in 1 call, same steps as the baseline",
              r["source"] == "agent" and r["calls"] == 1 and r["steps"] == b["steps"], f"calls={r['calls']}")

        client.chat = Script([("calls", [("search_identifiers", {"terms": [case["email"]]}), ("read_file", {"file_id": "evidence_01"})]),
                              ("calls", [("submit_verdict", good)])])
        r = run()
        check("one action round then verdict: 2 calls", r["source"] == "agent" and r["calls"] == 2, f"calls={r['calls']}")

        s = Script([("calls", [("get_activity", {"window": "burst"})]), ("calls", [("search_identifiers", {"terms": ["x@y.z"]})]),
                    ("text", good_enf)])
        client.chat = s
        r = run()
        check("after 2 action rounds the next call is a schema-enforced verdict",
              s.seen == ["tools", "tools", "schema"] and r["source"] == "agent", str(s.seen))

        s = Script([("calls", [("submit_verdict", bad)]), ("text", good_enf)])
        client.chat = s
        r = run()
        check("a rejected verdict gets one schema-enforced retry that can succeed",
              s.seen == ["tools", "schema"] and r["source"] == "agent" and r["steps"] == b["steps"], f"{s.seen} {r['source']}")

        s = Script([("calls", [("submit_verdict", bad)]), ("text", "not json at all")])
        client.chat = s
        r = run()
        check("a failed retry ends in the baseline (validator_exhausted)",
              r["source"] == "baseline" and r["fallback_reason"] == "validator_exhausted" and r["calls"] == 2, f"calls={r['calls']}")

        client.chat = Script([("text", "hmm")] * 9)
        r = run()
        check(f"a model that never submits stays within MAX_CALLS ({bounded.MAX_CALLS}) and falls back",
              r["calls"] <= bounded.MAX_CALLS and r["source"] == "baseline", f"calls={r['calls']} reason={r['fallback_reason']}")

        if ambiguous:
            keep = [d for d in good["decisions"] if d["decision"] == "include"] + \
                   [d for d in good["decisions"] if d["decision"] == "refuse"][4:]
            client.chat = Script([("calls", [("refuse_lead", {"resource_id": ref, "reason": "no identifier link"}) for ref in ambiguous[:4]]),
                                  ("calls", [("submit_verdict", dict(good, decisions=keep))])])
            r = run()
            check("refuse_lead refusals reach the verdict", r["source"] == "agent", f"fallback={r['fallback_detail']}")

        # the enforced schema itself
        sch = bounded.verdict_schema(cf)
        acc_enum = sch["properties"]["account"]["properties"]["ref"]["enum"]
        line_enum = set(sch["properties"]["records"]["items"]["properties"]["evidence"]["enum"])
        case_lines = {k for v in casefile.resolve(cf).values() for k in v[2]}
        check("enforced schema: one account object, refs and lines only from this case file",
              sch["properties"]["account"]["type"] == "object" and "none" in acc_enum and line_enum <= case_lines | {"none"},
              f"accounts={acc_enum}")

        # intent safety net
        na = {"authorises_deletion": False, "intent_reason": "looks like an export request", "decisions": []}
        s = Script([("calls", [("submit_verdict", na)]),
                    ("text", '{"authorises_deletion": true, "reason": "asks to delete the account"}'),
                    ("text", good_enf)])
        client.chat = s
        r = run()
        ic = r.get("intent_check") or {}
        check("agent says not applicable, rules + tie-breaker say deletion: agent re-decides (enforced), deletion audited",
              r["status"] != "NOT_APPLICABLE" and r["source"] == "agent" and ic.get("action", "").startswith("agent re-decided"),
              f"{r['status']} {ic.get('action')} {s.seen}")
        nd_case = next(c for c in ctx.cases if not baseline.authorises_deletion(c["request_text"]))
        accepted = {"status": "INCOMPLETE", "steps": [], "warnings": [],
                    "payload": {"authorises_deletion": True, "intent_reason": "misread"}}
        client.chat = Script([("text", '{"authorises_deletion": false, "reason": "only asks for a copy"}')])
        fin, ic = bounded.reconcile_intent(nd_case, ToolBox(ctx, nd_case), {}, "", accepted,
                                           [{"provider": "fake", "model": "scripted"}], "bounded_selftest")
        check("agent says deletion, rules + tie-breaker say not: NOT_APPLICABLE, logged",
              fin["status"] == "NOT_APPLICABLE" and (ic or {}).get("action") == "overruled to NOT_APPLICABLE")
        client.chat = Script([("text", "I cannot decide")])
        fin, ic = bounded.reconcile_intent(nd_case, ToolBox(ctx, nd_case), {}, "", accepted,
                                           [{"provider": "fake", "model": "scripted"}], "bounded_selftest")
        check("no usable tie-breaker: the rules win (agent said deletion, rules say not: NOT_APPLICABLE)",
              fin["status"] == "NOT_APPLICABLE" and (ic or {}).get("decided") is False)
        client.chat = Script([("calls", [("submit_verdict", good)])])
        r = run()
        check("agent and rules agree: no tie-break call", r.get("intent_check") is None and r["calls"] == 1)
    finally:
        client.chat = real_chat

    leaked, sizes = 0, []
    for c in tuning:
        t = casefile.to_text(casefile.build(ToolBox(ctx, ctx.by_id[c]), ctx.user[c]))
        leaked += "state_hint" in t
        sizes.append(casefile.est_tokens(t))
    check("case files carry no derived state", leaked == 0, str(leaked))
    print(f"   case-file size (est. tokens) on the tuning cases: median {sorted(sizes)[len(sizes) // 2]}, max {max(sizes)}")
    print("\nBOUNDED SELFTEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

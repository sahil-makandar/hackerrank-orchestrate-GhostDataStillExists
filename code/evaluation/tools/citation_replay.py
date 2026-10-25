"""Re-decide stored agent runs under the citation rule, with no model calls (cached replies only).

  python code/evaluation/tools/citation_replay.py --src b_t5 --dst b_t5c --tuning      # check on the 20 tuning cases (scored)
  python code/evaluation/tools/citation_replay.py --src final_v1 --dst final_v1c       # apply to a stored full run

Every case is run again through the same loop with GHOST_CACHE_ONLY=1. The models are tried in the order they answered
in the stored run, so each call is served from the reply cache of that run. A verdict rejected before only because its
citation was not citable can now be accepted. If the replayed conversation leaves the cache, that case cannot
be re-decided without a new call, so the stored result is kept and counted.
"""
import argparse
import collections
import csv
import json
import os
import sys
from pathlib import Path

os.environ["GHOST_CACHE_ONLY"] = "1"          # before the client is imported: never call a provider
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))          # code/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))          # code/evaluation/

import baseline                       # noqa: E402
import config                         # noqa: E402
import scoring                        # noqa: E402
import split                          # noqa: E402
from agent import bounded, casefile, submit     # noqa: E402
from agent.tools import ToolBox       # noqa: E402
from ingest.common import DATASET     # noqa: E402

RUNS = bounded.RUNS_DIR


def stored_order(d):
    """(provider, model) in the order they answered the stored case, then every lane by rank."""
    seen, out = set(), []
    lanes = sorted(config.LANES, key=lambda ln: ln["rank"])
    for pm in [tuple(d["first_lane"].split("/", 1))] if d.get("first_lane") else []:
        out.append(pm)
    for c in d.get("transcript", []):
        out.append((c["provider"], c["model"]))
    out += [(ln["provider"], ln["model"]) for ln in lanes]
    return [pm for pm in out if not (pm in seen or seen.add(pm))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    ap.add_argument("--tuning", action="store_true", help="only the 20 tuning cases, scored against the answers")
    ap.add_argument("--heldout", action="store_true",
                    help="only the 12 held-out cases, scored (DISCLOSURE run after the citation rule; logged access)")
    a = ap.parse_args()
    os.environ["GHOST_RUN_ID"] = a.dst
    stored = {json.loads(p.read_text(encoding="utf-8"))["case_id"]: json.loads(p.read_text(encoding="utf-8"))
              for p in sorted((RUNS / a.src).glob("case_*.json"))}
    if a.tuning or a.heldout:
        keep = set(split.tuning_cases()) if a.tuning else \
            set(split.heldout_cases(f"disclosure: held-out scores after the citation rule ({a.src} -> {a.dst})"))
        stored = {c: d for c, d in stored.items() if c in keep}
    ctx = baseline.Context()
    out_dir = RUNS / a.dst
    out_dir.mkdir(parents=True, exist_ok=True)
    real_enforced = bounded.enforced_chain
    tally, new = collections.Counter(), {}
    for cid in sorted(stored, key=lambda c: int(c.split("_")[1])):
        old = stored[cid]
        order = stored_order(old)
        enforced_first = [(c["provider"], c["model"]) for c in old.get("transcript", []) if c.get("enforced")]
        bounded.enforced_chain = lambda est, f=enforced_first: list(dict.fromkeys(f + real_enforced(est)))
        r = bounded.run_case(ctx, ctx.by_id[cid], order, a.dst)
        # with cached replies only, "no provider answered" always means the conversation left the cache
        left_cache = r["source"] == "baseline" and r.get("fallback_reason") == "providers_exhausted"
        fresh = r["baseline"]               # the baseline row under the current code (new tiers included)
        if left_cache and old["source"] == "agent":
            # the stored verdict stands only if it still passes the current submission checks
            again = None
            if old.get("payload"):
                tb = ToolBox(ctx, ctx.by_id[cid])
                casefile.build(tb, ctx.user.get(cid))     # runs the identifier search the run did (refusal rule)
                again = submit.evaluate(tb, old["payload"])
            if again is not None and again["accepted"]:
                kept = dict(old, status=again["status"], steps=again["steps"], partial=bool(again.get("partial")),
                            warnings=again["warnings"], baseline=fresh,
                            replay="kept stored verdict (replay left the reply cache; still passes the checks)")
                tally["kept stored (replay left the cache, still passes)"] += 1
            else:
                kept = dict(old, status=fresh["status"], steps=fresh["steps"], partial=bool(fresh.get("partial")),
                            baseline=fresh, source="baseline", fallback_reason="validator_exhausted",
                            fallback_detail="stored verdict fails the current checks: "
                                            + json.dumps([e["code"] for e in (again or {}).get("errors", [])][:5]),
                            replay="stored verdict rejected by the current checks; baseline row")
                tally["stored verdict now rejected -> baseline"] += 1
            (out_dir / f"{cid}.json").write_text(json.dumps(kept, ensure_ascii=False, default=str), encoding="utf-8")
            r = kept
        elif left_cache:
            # the stored case fell back too; keep its record (its real calls and fallback reason) unchanged
            kept = dict(old, status=fresh["status"], steps=fresh["steps"], partial=bool(fresh.get("partial")), baseline=fresh,
                        replay="kept stored fallback record; its baseline row recomputed under the current code")
            (out_dir / f"{cid}.json").write_text(json.dumps(kept, ensure_ascii=False, default=str), encoding="utf-8")
            r = kept
            tally["fallback, unchanged"] += 1
        else:
            changed = (r["status"], r["steps"]) != (old["status"], old["steps"])
            how = (f"{old['source']}{'/partial' if old.get('partial') else ''} -> "
                   f"{r['source']}{'/partial' if r.get('partial') else ''}")
            tally[f"{how}{' (changed)' if changed else ''}"] += 1
        new[cid] = r
    bounded.enforced_chain = real_enforced
    print(f"replayed {len(new)} cases of {a.src} into {a.dst} (no model calls)")
    for k, v in sorted(tally.items()):
        print(f"   {v:4d}  {k}")
    subs = sum(1 for r in new.values() for w in r.get("warnings") or [] if "instead of" in w)
    print(f"   citations picked by code: {subs}")
    if a.tuning or a.heldout:
        ref = scoring.load_reference([r for r in csv.DictReader(open(DATASET / "sample_output.csv", encoding="utf-8",
                                                                     newline="")) if r["case_id"] in new])
        print(scoring.format_score("STORED  ", scoring.score({c: (d["status"], d["steps"]) for c, d in stored.items()}, ref)))
        print(scoring.format_score("REPLAYED", scoring.score({c: (d["status"], d["steps"]) for c, d in new.items()}, ref)))
        print(scoring.format_score("BASELINE", scoring.score({c: (d["baseline"]["status"], d["baseline"]["steps"])
                                                             for c, d in stored.items()}, ref)))


if __name__ == "__main__":
    main()

"""Re-run the baseline-fallback cases of a finished run on one model, then merge into a new run id.

  python code/evaluation/tools/rerun_fallbacks.py --src final_v1c --dst final_v1d --provider nvidia --model minimaxai/minimax-m3.1-flash

Every verdict call (the action loop and the schema-enforced verdict / retry) goes to the one model; the submission
checks and the validator are unchanged. A case keeps its baseline row unless the model's verdict is accepted.
The intent tie-break, which only runs when the model and the rules disagree, still asks a model of another provider.
The other cases are copied from --src unchanged, so `python code/main.py --run-id <dst>` writes output.csv.
"""
import argparse
import collections
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))          # code/

import baseline                       # noqa: E402
from agent import bounded             # noqa: E402

RUNS = bounded.RUNS_DIR


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    ap.add_argument("--provider", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--retry-unavailable", action="store_true",
                    help="re-run cases where the model was unavailable (providers_exhausted) in an earlier pass")
    a = ap.parse_args()
    os.environ["GHOST_RUN_ID"] = a.dst
    src, dst = RUNS / a.src, RUNS / a.dst
    dst.mkdir(parents=True, exist_ok=True)
    stored = {json.loads(p.read_text(encoding="utf-8"))["case_id"]: p for p in sorted(src.glob("case_*.json"))}
    fallbacks = sorted((c for c, p in stored.items() if json.loads(p.read_text(encoding="utf-8"))["source"] == "baseline"),
                       key=lambda c: int(c.split("_")[1]))
    print(f"{a.src}: {len(stored)} cases, {len(fallbacks)} baseline fallbacks -> re-run on {a.provider}/{a.model}")
    for c, p in stored.items():
        if c not in fallbacks and not (dst / p.name).exists():
            shutil.copyfile(p, dst / p.name)
    def unanswered(c):
        """The model never gave a verdict (it was unavailable): that is not a verdict failure, so it is retried."""
        p = dst / f"{c}.json"
        return a.retry_unavailable and p.exists() and "providers_exhausted" in json.loads(p.read_text(encoding="utf-8")).get("rerun", "")
    todo = [c for c in fallbacks if not (dst / f"{c}.json").exists() or unanswered(c)]     # resumable
    chain = [(a.provider, a.model)]
    bounded.enforced_chain = lambda est: chain                             # the enforced verdict on the same model
    ctx = baseline.Context()
    tmp = f"{a.dst}__rerun"
    res = bounded.run_many(ctx, [ctx.by_id[c] for c in todo], tmp, workers=a.workers, fixed_chain=chain)
    tally = collections.Counter()
    for c in todo:
        r_path = RUNS / tmp / f"{c}.json"
        new = json.loads(r_path.read_text(encoding="utf-8")) if r_path.exists() else None
        if new is not None and new["source"] == "agent":
            new["rerun"] = f"fallback in {a.src}; accepted on {a.model}"
            (dst / f"{c}.json").write_text(json.dumps(new, ensure_ascii=False, default=str), encoding="utf-8")
            tally["accepted: agent row"] += 1
        else:
            old = json.loads(stored[c].read_text(encoding="utf-8"))
            old["rerun"] = (f"re-run on {a.model} also failed: "
                            f"{(new or res.get(c) or {}).get('fallback_reason')}; baseline row kept")
            old["rerun_transcript"] = (new or {}).get("transcript", [])
            (dst / f"{c}.json").write_text(json.dumps(old, ensure_ascii=False, default=str), encoding="utf-8")
            tally[f"still fallback ({(new or res.get(c) or {}).get('fallback_reason')})"] += 1
    for k, v in sorted(tally.items()):
        print(f"   {v:4d}  {k}")
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in dst.glob("case_*.json")]
    fb = sum(1 for d in rows if d["source"] == "baseline")
    print(f"{a.dst}: {len(rows)} cases, fallback {fb} ({fb / len(rows):.1%})")


if __name__ == "__main__":
    main()

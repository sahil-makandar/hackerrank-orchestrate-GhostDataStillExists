"""Ghost Data Still Exists: deletion-audit agent. Writes output.csv at the repository root.
It resumes from the stored per-case results; `--cache-only` never calls a provider.

  python code/main.py                         full run over dataset/deletion_requests.csv (resumes by default)
  python code/main.py --run-id final_v1       name the run (per-case results: code/cache/agent/<run-id>/)
  python code/main.py --workers 8             parallel cases (one shared rate limiter per provider)
  python code/main.py --baseline-only         deterministic baseline only (no model calls)
  python code/main.py --cache-only            agent run served from cached replies only (never calls a provider;
                                              a case whose reply is not cached falls back to the baseline)
  python code/main.py --fresh                 ignore earlier per-case results of this run id

Resume: every finished case is stored as code/cache/agent/<run-id>/<case_id>.json; a rerun with the same run id skips
those cases. output.csv is rewritten from all stored cases after each batch, so an interrupted run still leaves a valid
file (cases not yet run get the baseline row, marked in the run summary).
"""
import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

CODE = Path(__file__).resolve().parent
sys.path.insert(0, str(CODE))

ROOT = CODE.parent
OUTPUT = ROOT / "output.csv"


def ensure_index():
    """Build the evidence index once (parsers, local OCR, cached model extractions) if it does not exist yet."""
    from ingest import build
    if not build.CACHE.exists():
        print("building the evidence index (first run only) ...", flush=True)
        build.save(build.build(verbose=False))


def write_output(ctx, results, path=None):
    import validator
    from steps import to_json
    rows, problems = [], []
    # numeric order of the case number (case_33 ... case_550), the order of deletion_requests.csv (see decisions.md)
    for c in sorted(ctx.evals, key=lambda c: int(c["case_id"].split("_")[1])):
        cid = c["case_id"]
        r = results.get(cid)
        if r is None:
            import baseline
            b = baseline.assess(ctx, c)
            r = {"status": b["status"], "steps": b["steps"], "source": "baseline",
                 "baseline": {"partial": bool(b.get("partial"))}, "fallback_reason": "not_run_yet"}
        partial = r.get("partial") or (r["source"] == "baseline" and r.get("baseline", {}).get("partial"))
        errs = validator.validate(cid, r["status"], r["steps"], c["_audit_at"], partial=bool(partial))
        if errs:
            problems.append((cid, validator.format_errors(errs)[:200]))
        rows.append((cid, r["status"], to_json(r["steps"])))
    path = Path(path) if path else OUTPUT
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["case_id", "deletion_status", "steps_json"])
        w.writerows(rows)
    os.replace(tmp, path)
    return len(rows), problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-id", default="final_v1")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--batch", type=int, default=40, help="cases per batch between output.csv rewrites")
    ap.add_argument("--baseline-only", action="store_true")
    ap.add_argument("--cache-only", action="store_true", help="serve model replies from code/cache/llm only")
    ap.add_argument("--output", default="", help="write the CSV here instead of the repository root output.csv")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="run at most this many new cases (0 = all)")
    a = ap.parse_args()
    os.environ["GHOST_RUN_ID"] = a.run_id
    if a.baseline_only or a.cache_only:
        os.environ["GHOST_CACHE_ONLY"] = "1"      # read by llm/client.py at import, which happens below
    ensure_index()

    import baseline
    import linkage
    from agent import bounded
    ctx = baseline.Context()
    ctx.evals = linkage.load_requests("deletion_requests.csv")
    run_dir = bounded.RUNS_DIR / a.run_id
    results = {}
    if not a.fresh and run_dir.exists():
        for p in run_dir.glob("case_*.json"):
            d = json.loads(p.read_text(encoding="utf-8"))
            results[d["case_id"]] = d
    todo = [c for c in ctx.evals if c["case_id"] not in results]
    if a.limit:
        todo = todo[:a.limit]
    print(f"run {a.run_id}: {len(ctx.evals)} requests, {len(results)} already done, {len(todo)} to run", flush=True)

    t0 = time.time()
    if a.baseline_only:
        for c in todo:
            b = baseline.assess(ctx, c)
            results[c["case_id"]] = {"case_id": c["case_id"], "status": b["status"], "steps": b["steps"], "source": "baseline",
                                     "fallback_reason": "baseline_only", "baseline": {"partial": bool(b.get("partial"))}}
    else:
        for i in range(0, len(todo), a.batch):
            batch = todo[i:i + a.batch]
            results.update(bounded.run_many(ctx, batch, a.run_id, workers=a.workers, progress=False))
            n, problems = write_output(ctx, results, a.output)
            done = len(results)
            rate = (i + len(batch)) / max(time.time() - t0, 1) * 60
            agent = sum(1 for r in results.values() if r.get("source") == "agent")
            print(f"  {done}/{len(ctx.evals)} cases done ({agent} by the agent), {rate:.1f} cases/min, "
                  f"output.csv rewritten ({len(problems)} invalid rows)", flush=True)
    n, problems = write_output(ctx, results, a.output)
    src = {}
    for r in results.values():
        src[r.get("fallback_reason") or "agent"] = src.get(r.get("fallback_reason") or "agent", 0) + 1
    print(f"\nwrote {a.output or OUTPUT} ({n} rows); sources: {src}; invalid rows: {len(problems)}")
    for p in problems[:10]:
        print("  INVALID", p)
    return 0 if not problems and n == len(ctx.evals) else 1


if __name__ == "__main__":
    sys.exit(main())

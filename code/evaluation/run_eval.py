"""Single entry point for the evaluation: every offline gate, then the reports from stored runs. No model calls.

  python code/evaluation/run_eval.py                       # gates + reports for the submitted run
  python code/evaluation/run_eval.py --quick               # gates only, skipping the slow linkage measurements
  python code/evaluation/run_eval.py --run final_v1e --tuning-run b_t5g --usage final_v1,final_v1c,final_v1d

Gates (each script exits non-zero on a failed check):
  pytest             unit tests in code/tests (validator, policy mapping, step order, citation clock, intent)
  m1_gate           ingest coverage and identity resolution
  m2_measure         linkage tiers on the tuning cases: zero wrong timing-unique links
  forced_gate        constraint-forced tier: zero wrong
  clock_variants     citation audit and the two-clock rule
  m3_gate            validator (golden + broken rows), step builder, baseline
  m4_gate --offline  connector failure paths (fake network and clock)
  m5_selftest        tools and submission checks
  bounded_selftest   bounded loop, retry, budget, intent safety net (scripted fake model)
  custom_cases       our custom evaluation cases
  acceptance         the acceptance criteria and adversarial cases, against output.csv
Reports (read stored per-case results and code/cache/usage.jsonl):
  bounded_eval --analyse-only   agent vs baseline on the 20 tuning cases
  full_run_stats, model_table, usage_summary   the full run
The 12 held-out cases are never scored here; `bounded_eval.py --heldout` / `tools/citation_replay.py --heldout` do that,
and every access is logged in heldout_access.log.
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# every run id whose LIVE calls produced output.csv: final_v1 (318 cases), final_v1c (200 cases), final_v1d (the
# MiniMax re-run of the fallbacks and its retries); final_v1e is a cache-only replay (0 live calls)
USAGE_RUNS = "final_v1,final_v1c,final_v1d"


def script(name):
    """Scripts at the top of code/evaluation/ or in code/evaluation/dev/ (the milestone gates and analyses)."""
    return HERE / f"{name}.py" if (HERE / f"{name}.py").exists() else HERE / "dev" / f"{name}.py"


def run(name, *args):
    t0 = time.time()
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    if name != "m4_gate":            # never call a provider from here; m4_gate --offline fakes the network itself
        env["GHOST_CACHE_ONLY"] = "1"
    cmd = ([sys.executable, "-m", "pytest", str(HERE.parent / "tests"), "-q"] if name == "pytest"
           else [sys.executable, str(script(name)), *args])
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = [line for line in (p.stdout + p.stderr).strip().splitlines() if line.strip()][-1:] or [""]
    return p.returncode, time.time() - t0, tail[0][:110], p.stdout


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", default="final_v1e", help="run whose per-case results produced output.csv")
    ap.add_argument("--tuning-run", default="b_t5g", help="agent run on the 20 tuning cases to score")
    ap.add_argument("--usage", default=USAGE_RUNS, help="run ids whose live calls are counted")
    ap.add_argument("--quick", action="store_true", help="skip m2_measure and forced_gate (several minutes each)")
    a = ap.parse_args()
    gates = [("pytest",), ("m1_gate",), ("m2_measure",), ("forced_gate",), ("clock_variants",), ("m3_gate",), ("m4_gate", "--offline"),
             ("m5_selftest",), ("bounded_selftest",), ("custom_cases",), ("acceptance", "--run-id", a.run)]
    if a.quick:
        gates = [g for g in gates if g[0] not in ("m2_measure", "forced_gate")]
    failed = []
    print(f"{'gate':34s} {'result':6s} {'secs':>5s}  last line")
    for g in gates:
        code, secs, tail, _ = run(*g)
        print(f"{' '.join(g):34s} {'PASS' if code == 0 else 'FAIL':6s} {secs:5.0f}  {tail}")
        if code:
            failed.append(g[0])
    print()
    for rep in [("bounded_eval", "--run-id", a.tuning_run, "--analyse-only"), ("full_run_stats", "--run-id", a.run),
                ("model_table", "--cases", a.run, "--usage", a.usage), ("usage_summary", "--run-id", a.usage)]:
        code, _, _, out = run(*rep)
        print(f"== {' '.join(rep)}" + ("" if code == 0 else "  (FAILED)"))
        print(out.rstrip() + "\n")
        if code:
            failed.append(rep[0])
    print("EVALUATION:", "PASS" if not failed else f"FAIL ({', '.join(failed)})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

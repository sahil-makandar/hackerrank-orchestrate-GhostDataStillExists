"""Rebuild the submitted output.csv from the cache, end to end, and check it against the recorded SHA-256.
Run `python code/rebuild_output.py`; it prints MATCH when the file is identical.

  python code/rebuild_output.py              # rebuild into a temporary file, compare, report MATCH / MISMATCH
  python code/rebuild_output.py --install    # also replace output.csv with the rebuilt file (only if it matches)
  python code/rebuild_output.py --record     # record the SHA-256 of the rebuilt file as the new expected value

Steps (no model call is made; replies and verdicts come from code/cache/):
  1. every stored verdict of run final_v1d is re-checked under the current code and written to run final_v1e
     (evaluation/tools/citation_replay.py);
  2. main.py writes the CSV from run final_v1e with --cache-only;
  3. the SHA-256 of the result is compared with code/evaluation/submitted_output.sha256 and with output.csv.
The cache (code/cache/) is not part of code.zip: this command works in the repository where the run was made.
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CODE = Path(__file__).resolve().parent
ROOT = CODE.parent
EXPECTED = CODE / "evaluation" / "submitted_output.sha256"
SRC_RUN, FINAL_RUN = "final_v1d", "final_v1e"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest().upper()


def run(*args):
    env = dict(os.environ, GHOST_CACHE_ONLY="1", PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, *map(str, args)], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode:
        sys.exit(f"step failed: {' '.join(map(str, args))}\n{p.stdout[-800:]}\n{p.stderr[-800:]}")
    return p.stdout


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--install", action="store_true", help="replace output.csv with the rebuilt file if it matches")
    ap.add_argument("--record", action="store_true", help="record the rebuilt file's SHA-256 as the expected value")
    a = ap.parse_args()
    if not (CODE / "cache" / "agent" / SRC_RUN).exists():
        sys.exit(f"code/cache/agent/{SRC_RUN} is missing: the stored verdicts are needed to rebuild")
    print(f"1. re-checking the stored verdicts of {SRC_RUN} -> {FINAL_RUN} ...", flush=True)
    print("   " + run(CODE / "evaluation" / "tools" / "citation_replay.py", "--src", SRC_RUN, "--dst", FINAL_RUN).splitlines()[0])
    tmp = Path(tempfile.mkdtemp(prefix="rebuild_output_")) / "output.csv"
    print(f"2. writing the CSV from {FINAL_RUN} (cache only) ...", flush=True)
    print("   " + run(CODE / "main.py", "--run-id", FINAL_RUN, "--cache-only", "--output", tmp).strip().splitlines()[-1])
    got = sha256(tmp)
    if a.record:
        EXPECTED.write_text(got + "\n", encoding="utf-8")
    expected = EXPECTED.read_text(encoding="utf-8").strip().upper() if EXPECTED.exists() else None
    current = sha256(ROOT / "output.csv") if (ROOT / "output.csv").exists() else None
    print(f"3. SHA-256 rebuilt   {got}\n   recorded          {expected}\n   output.csv        {current}")
    ok = got == expected
    print("REBUILD:", "MATCH" if ok else "MISMATCH", "(the rebuilt file is " + ("identical to" if got == current else "different from") + " output.csv)")
    if a.install and ok:
        shutil.copyfile(tmp, ROOT / "output.csv")
        print("output.csv replaced by the rebuilt file")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

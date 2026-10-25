"""Build code.zip at the repository root: README.md + code/, without caches, logs, run outputs or secrets.
Run `python code/package.py` from the repository root.

  python code/package.py
"""
import os
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {"cache", "__pycache__", ".venv", "venv", ".git"}
SKIP_SUFFIX = (".pyc", ".zip", ".out", ".pkl", ".log", ".DS_Store")
SKIP_NAMES = {".env"}


def files():
    yield ROOT / "README.md"
    for root, dirs, names in os.walk(ROOT / "code"):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for n in sorted(names):
            if n not in SKIP_NAMES and not n.endswith(SKIP_SUFFIX):
                yield Path(root) / n


def main():
    out = ROOT / "code.zip"
    tmp = out.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files():
            z.write(p, p.relative_to(ROOT).as_posix())
    os.replace(tmp, out)
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    print(f"wrote {out} ({len(names)} files, {out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()

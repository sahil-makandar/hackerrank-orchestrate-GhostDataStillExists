"""M1 gate: ingest coverage + identity resolution. Exit code 0 only if every check passes.

  python code/evaluation/m1_gate.py            # uses cached model outputs; builds + saves the evidence index
"""
import csv
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from identity import IdentityResolver                      # noqa: E402
from ingest import build                                    # noqa: E402
from ingest.common import DATASET, RESOURCE_RE  # noqa: E402
from ingest import localocr                                 # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


def main():
    idx = build.build(verbose=False)
    build.save(idx)
    manifest = idx.manifest

    # 1. every manifest file parsed (or a reason is logged)
    failed = [(r.file_id, r.note) for r in idx.reports if not r.ok]
    check(f"all {len(manifest)} manifest files parsed", not failed and len(idx.reports) == len(manifest), str(failed[:3]))
    by_kind = Counter(r.kind for r in idx.reports)
    print("   files by kind:", dict(by_kind))

    # 2. every resource_id that appears in the raw evidence text is present in observations/scopes
    known = {o.resource_id for o in idx.observations}
    for s in idx.scopes:
        known |= set(s.resource_ids)
    for o in idx.observations:  # back-references (e.g. a search document's source_resource_id) stay retrievable in fields
        known |= set(RESOURCE_RE.findall(" ".join(str(v) for v in o.fields.values())))
    missing = Counter()
    examples = {}
    for fid, row in manifest.items():
        path = DATASET / row["file_path"]
        ext = path.suffix.lower()
        if ext == ".png":
            text = localocr.read(path.read_bytes())
        elif ext == ".pdf":
            from ingest.pdf_ import _text
            text = _text(path)
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
        for rid in set(RESOURCE_RE.findall(text)):
            if rid not in known:
                missing[ext] += 1
                examples.setdefault(ext, []).append((fid, rid))
    check("every resource_id found in raw evidence text is indexed", not missing,
          f"missing by kind={dict(missing)} e.g. {[v[:3] for v in examples.values()]}")

    # 3. image-derived facts must appear in the OCR text of the same image
    bad_img = []
    for fid, row in manifest.items():
        if not row["file_path"].endswith(".png"):
            continue
        ocr = localocr.read((DATASET / row["file_path"]).read_bytes()).lower()
        digits = re.sub(r"\D", "", ocr)
        for o in idx.by_resource.values():
            for ob in o:
                if ob.file_id != fid:
                    continue
                toks = {ob.resource_id.lower()} | ob.emails | ob.hashes | {u.lower() for u in ob.user_ids}
                bad_img += [(fid, t) for t in toks if t not in ocr]
                bad_img += [(fid, p) for p in ob.phones if p.lstrip("+") not in digits]
    check("every image-derived identifier appears in that image's OCR text", not bad_img, str(bad_img[:3]))

    # 4. identity resolver maps every request email to exactly one user_id
    resolver = IdentityResolver()
    rows = []
    for name in ("deletion_requests.csv", "sample_deletion_requests.csv"):
        with open(DATASET / name, encoding="utf-8", newline="") as f:
            rows += list(csv.DictReader(f))
    counts = Counter(len(resolver.resolve(r["email"])) for r in rows)
    check(f"identity resolver: all {len(rows)} request emails -> exactly 1 user", counts == Counter({1: len(rows)}), str(dict(counts)))

    print("\nM1 GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

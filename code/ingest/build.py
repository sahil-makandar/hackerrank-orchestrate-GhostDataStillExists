"""Parse every file in evidence_files.csv into one normalised index (observations + scopes) and a parse report.
`build` parses everything once; `save` and `load` keep the index in code/cache.
"""
import csv
import json
import pickle
from collections import defaultdict

from . import images, json_, logs, pdf_, sql_, text_
from .common import DATASET, FileReport

CACHE = DATASET.parent / "code" / "cache" / "evidence_index.pkl"
PARSERS = {".sql": sql_.parse, ".json": json_.parse, ".log": logs.parse, ".txt": text_.parse,
           ".pdf": pdf_.parse, ".png": images.parse}


class EvidenceIndex:
    def __init__(self, manifest, observations, scopes, reports):
        self.manifest = manifest                  # file_id -> {path, origin, captured_at}
        self.observations = observations
        self.scopes = scopes
        self.reports = reports
        self.by_resource = defaultdict(list)
        for o in observations:
            self.by_resource[o.resource_id].append(o)


def load_manifest():
    with open(DATASET / "evidence_files.csv", encoding="utf-8", newline="") as f:
        return {r["file_id"]: r for r in csv.DictReader(f)}


def build(only_kinds=None, verbose=True):
    manifest = load_manifest()
    observations, scopes, reports = [], [], []
    for fid, row in manifest.items():
        path = DATASET / row["file_path"]
        ext = path.suffix.lower()
        if only_kinds and ext not in only_kinds:
            continue
        rep = FileReport(fid, row["file_path"], ext, False)
        try:
            res = PARSERS[ext](fid, path, row["captured_at"])
            obs, sc = res[0], res[1]
            if len(res) > 3:
                rep.note = json.dumps(res[3], ensure_ascii=False)
            observations += obs
            scopes += sc
            rep.ok, rep.observations, rep.scopes = True, len(obs), len(sc)
        except Exception as e:  # reported, never silent
            rep.note = f"{type(e).__name__}: {str(e)[:200]}"
        reports.append(rep)
        if verbose:
            print(f"{fid} {ext:5s} ok={rep.ok} obs={rep.observations} scopes={rep.scopes} {rep.note[:100]}", flush=True)
    return EvidenceIndex(manifest, observations, scopes, reports)


def save(index):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE, "wb") as f:
        pickle.dump(index, f)


def load():
    with open(CACHE, "rb") as f:
        return pickle.load(f)

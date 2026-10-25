"""Tuning / held-out split of the 32 labelled sample cases (decided 2026-10-24, before any agent run).

20 cases are for tuning prompts and tools; 12 are held out and scored only once, at the end. The split is stratified by
the answer's deletion_status and drawn with a fixed seed, so it is reproducible and was not chosen by looking at agent
results. Reading the held-out labels for scoring goes through `heldout_cases(reason)`, which writes an access line to
`heldout_access.log` so every use is auditable.

Disclosure: the pre-pass, state reader and deterministic baseline were developed looking at all 32 samples (before this
split existed), so the baseline is NOT clean on the held-out 12. The split protects the agent's prompts and tools only.
"""
import csv
import random
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATASET = HERE.parents[1] / "dataset"
SEED = 20261024
HELD_OUT_SIZE = 12
ACCESS_LOG = HERE / "heldout_access.log"


def _statuses():
    with open(DATASET / "sample_output.csv", encoding="utf-8", newline="") as f:
        return {r["case_id"]: r["deletion_status"] for r in csv.DictReader(f)}


def compute():
    """Stratified, seeded split -> (tuning_ids, heldout_ids), both sorted."""
    st = _statuses()
    rng = random.Random(SEED)
    groups = {}
    for cid in sorted(st):
        groups.setdefault(st[cid], []).append(cid)
    # largest-remainder allocation so the held-out set has the same status mix as the samples
    quotas = {k: len(v) * HELD_OUT_SIZE / len(st) for k, v in groups.items()}
    take = {k: int(q) for k, q in quotas.items()}
    for k in sorted(quotas, key=lambda k: quotas[k] - take[k], reverse=True)[:HELD_OUT_SIZE - sum(take.values())]:
        take[k] += 1
    held = []
    for k, ids in sorted(groups.items()):
        shuffled = ids[:]
        rng.shuffle(shuffled)
        held += shuffled[:take[k]]
    held = sorted(held)
    return sorted(set(st) - set(held)), held


def tuning_cases():
    return compute()[0]


def heldout_cases(reason):
    """Held-out case ids. Every call is logged: held-out cases are scored only at the end."""
    if not reason or not reason.strip():
        raise ValueError("state why the held-out cases are being read")
    with open(ACCESS_LOG, "a", encoding="utf-8", newline="\n") as f:
        f.write(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {reason.strip()}\n")
    return compute()[1]


if __name__ == "__main__":
    tuning, held = compute()
    st = _statuses()
    print("tuning  (%d):" % len(tuning), ", ".join(tuning))
    print("heldout (%d):" % len(held), ", ".join(held))
    for name, ids in (("tuning", tuning), ("heldout", held)):
        print(name, "status mix:", {s: sum(st[c] == s for c in ids) for s in sorted(set(st.values()))})

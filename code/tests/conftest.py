"""Shared setup for the unit tests: `python -m pytest code/tests -q` from the repository root.

The tests are offline: no model is called (cache-only mode) and only participant-facing files under dataset/ are read.
"""
import csv
import json
import os
import sys
from pathlib import Path

import pytest

os.environ["GHOST_CACHE_ONLY"] = "1"
CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import linkage  # noqa: E402
from ingest.common import DATASET  # noqa: E402


@pytest.fixture(scope="session")
def samples():
    """case_id -> request row (with parsed times), for the 32 public sample requests."""
    return {c["case_id"]: c for c in linkage.load_requests("sample_deletion_requests.csv")}


@pytest.fixture(scope="session")
def answers():
    """case_id -> (status, steps) from dataset/sample_output.csv."""
    with open(DATASET / "sample_output.csv", encoding="utf-8", newline="") as f:
        return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in csv.DictReader(f)}

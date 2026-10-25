"""Shared types and identifier extraction for evidence ingestion.
Used by every parser: Obs, Scope, the resource id patterns and the identifier extractor.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "dataset"

# Resource-id prefix -> owning system. This is the platform's naming convention,
# derived from the dataset (system_activity targets and evidence), not from any case.
PREFIX_SYSTEM = {
    "account": "account_service",
    "search": "search_service",
    "support": "support_platform",
    "analytics": "analytics_platform",
    "export": "export_service",
    "marketing": "marketing_platform",
    "email_delivery": "email_delivery_service",
    "cache": "cache_service",
    "backup": "backup_service",
    "data_flow": "integration_service",
    "audit_record": "security_service",
    "tombstone": "identity_service",
    "object": "object_storage",
    "legal_hold": "legal_service",
}
_PREFIXES = sorted(PREFIX_SYSTEM, key=len, reverse=True)
RESOURCE_RE = re.compile(r"\b(?:%s)_\d+\b" % "|".join(_PREFIXES))
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
HASH_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")
USER_RE = re.compile(r"\buser_\d+\b")
PHONE_RE = re.compile(r"\+\d[\d ()\-]{7,}\d")


def system_of(resource_id):
    for prefix in _PREFIXES:
        if resource_id.startswith(prefix + "_"):
            return PREFIX_SYSTEM[prefix]
    return None


def norm_phone(p):
    return re.sub(r"[^\d+]", "", p)


def extract_ids(text):
    """Identifiers found anywhere in a text blob, normalised for matching."""
    text = text if isinstance(text, str) else str(text)
    return {
        "emails": {m.lower() for m in EMAIL_RE.findall(text)},
        "hashes": {m.lower() for m in HASH_RE.findall(text)},
        "user_ids": set(USER_RE.findall(text)),
        "phones": {norm_phone(m) for m in PHONE_RE.findall(text)},
        "resources": set(RESOURCE_RE.findall(text)),
    }


@dataclass
class Obs:
    """One observation of one resource in one evidence file."""
    file_id: str
    obs_time: str            # when the state was observed (ISO-8601 Z)
    resource_id: str
    system_id: str
    source: str              # table / collection / log service / page
    fields: dict = field(default_factory=dict)
    emails: set = field(default_factory=set)
    hashes: set = field(default_factory=set)
    user_ids: set = field(default_factory=set)
    phones: set = field(default_factory=set)

    def absorb(self, text):
        ids = extract_ids(text)
        self.emails |= ids["emails"]
        self.hashes |= ids["hashes"]
        self.user_ids |= ids["user_ids"]
        self.phones |= ids["phones"]


@dataclass
class Scope:
    """A snapshot that enumerates ALL records of a collection: absence is evidence."""
    file_id: str
    obs_time: str
    system_id: str
    collection: str
    resource_ids: list


@dataclass
class FileReport:
    file_id: str
    path: str
    kind: str
    ok: bool
    observations: int = 0
    scopes: int = 0
    note: str = ""

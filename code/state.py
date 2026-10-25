"""Resource state at audit_at, read from evidence (deterministic reader used by the baseline and offered to the agent).
A reading is what one observation says about one record; the current state is derived per copy.

A *reading* is what one observation says about one resource. Kinds:
  PRESENT       the record exists (nothing said about its identifiers)
  ABSENT        lookup / snapshot shows it does not exist
  IDENTIFIABLE  exists and still exposes the subject's direct identifiers
  REDACTED      exists, direct identifiers removed (redaction of a retained record)
  ANONYMIZED    exists, no longer attributable to a user
  ACTIVE        data flow enabled / backup restorable
  DISABLED      data flow disabled
  BLOCKED       backup restore blocked
  UNKNOWN       the observation does not say (e.g. an OCR row the agent has to read itself)
Facts are only used if their own time is <= audit_at (the information clock); a reading is *citable* only if its file
was captured / its activity recorded <= audit_at.
"""
import re
from dataclasses import dataclass

from linkage import info_time, parse_time

TRUE = {"true", "1", "yes", "y"}
NULLISH = {"", "null", "none", "n/a"}
SEVERITY = {  # when several copies are read at the same time, the one that still exposes data wins
    "IDENTIFIABLE": 6, "ACTIVE": 5, "PRESENT": 4, "REDACTED": 3, "ANONYMIZED": 3, "BLOCKED": 2, "DISABLED": 2,
    "ABSENT": 1, "UNKNOWN": 0}


@dataclass
class Reading:
    kind: str
    time: object
    file_id: str
    citable: bool
    source: str = ""
    detail: str = ""


def _truthy(v):
    return str(v).strip().lower() in TRUE


def _has(v):
    return v is not None and str(v).strip().lower() not in NULLISH


def _any_identifier(f, keys):
    return any(_has(f.get(k)) for k in keys)


def _lookup_result(f):
    return {"FOUND": "PRESENT", "NOT_FOUND": "ABSENT", "IDENTIFIABLE": "IDENTIFIABLE", "ANONYMIZED": "ANONYMIZED",
            "REDACTED": "REDACTED", "ACTIVE": "ACTIVE", "DISABLED": "DISABLED", "BLOCKED": "BLOCKED"}.get(f["result"], "UNKNOWN")


def classify(o):  # noqa: C901  (one flat table of evidence shapes)
    """Kind of state one observation reports for its resource."""
    f, src, ev = o.fields, o.source, o.fields.get("event", "")
    sysid = o.system_id
    if str(ev).endswith("_started"):                              # a lookup was begun: says nothing about state
        return "UNKNOWN"
    if "result" in f and "record_type" in f:                       # handover / ledgers / record_lookup / history
        return _lookup_result(f)
    if src.startswith("image:") or src.startswith("pdf:"):
        # a business artifact that shows the subject's identifiers proves the record exists and exposes them; whether a
        # backup is restorable or a flow is live cannot be read without the page (the agent reads the OCR rows itself)
        if (o.emails or o.phones) and sysid not in ("backup_service", "integration_service"):
            return "IDENTIFIABLE"
        return "UNKNOWN"
    if ev in ("profile_lookup_completed",):
        return "ABSENT" if f.get("rows") == "0" or f.get("http_status") == "404" else "PRESENT"
    if src == "profile_lookups":
        return "ABSENT" if f.get("status_code") == 404 else "PRESENT"
    if ev == "index_query_completed":
        return "PRESENT" if f.get("result") == "hit" or f.get("total_hits", "0") not in ("0", "") else "ABSENT"
    if src == "index_queries":
        return "PRESENT" if f.get("hits.total", 0) else "ABSENT"
    if ev in ("support_ticket_read_completed",):
        return "IDENTIFIABLE" if _any_identifier(f, ("requester_name", "requester_email", "requester_phone")) else "REDACTED"
    if ev in ("message_pii_scan", "attachment_pii_scan", "analytics_identity_scan"):
        return "IDENTIFIABLE"
    if ev == "message_read":
        return "REDACTED" if "[REDACTED]" in str(f.get("message_text", "")) else "IDENTIFIABLE"
    if ev == "contact_query_completed":
        return "PRESENT" if f.get("rows") not in ("0", None) else "ABSENT"
    if ev == "deletion_marker_check":
        return "PRESENT" if f.get("marker_found") == "false" else "ABSENT"
    if src == "tickets" or src == "support_records":
        return "IDENTIFIABLE" if _any_identifier(f, ("requester_name", "requester_email", "requester_phone")) else "REDACTED"
    if src == "support_conversations" or (sysid == "support_platform" and "message_text" in f):
        text = str(f.get("message_text", ""))
        return "REDACTED" if "[REDACTED]" in text else ("IDENTIFIABLE" if o.emails or o.phones else "PRESENT")
    if ev == "analytics_profile_read":
        return "IDENTIFIABLE" if f.get("direct_identifiers", "0") != "0" or _any_identifier(f, ("email", "phone")) else "ANONYMIZED"
    if ev == "analytics_profile_read_completed" or sysid == "analytics_platform":
        if ev == "contact_lookup_completed":
            return "PRESENT" if f.get("rows") != "0" else "ABSENT"
        return "IDENTIFIABLE" if _any_identifier(f, ("email", "phone", "email_hash", "user_id", "customer_reference")) else "ANONYMIZED"
    if ev == "export_download_check_completed":
        return "PRESENT" if _truthy(f.get("available")) else "ABSENT"
    if ev == "export_metadata_read_completed":
        return "PRESENT" if _truthy(f.get("available", "true")) else "ABSENT"
    if src == "customer_export_jobs" or (sysid == "export_service" and "download_available" in f):
        return "PRESENT" if _truthy(f.get("download_available")) else "ABSENT"
    if ev == "restore_candidate_evaluated":
        return "ACTIVE" if _truthy(f.get("eligible")) and not _truthy(f.get("deletion_block")) else "BLOCKED"
    if ev == "restore_authorization_completed":
        return "BLOCKED" if f.get("decision") == "deny" else "ACTIVE"
    if ev == "restore_policy_read_completed" or (sysid == "backup_service" and "restore_allowed" in f):
        return "ACTIVE" if _truthy(f.get("restore_allowed")) else "BLOCKED"
    if ev in ("connector_heartbeat", "connector_config_loaded"):
        live = _truthy(f.get("enabled")) and f.get("scheduler_registered", "true") != "false"
        return "ACTIVE" if live else "DISABLED"
    if ev == "connector_config_read_completed" or (sysid == "integration_service" and "enabled" in f):
        return "ACTIVE" if _truthy(f.get("enabled")) else "DISABLED"
    if sysid == "identity_service":
        return "IDENTIFIABLE" if _any_identifier(f, ("name", "email", "phone", "email_hash")) else "REDACTED"
    if sysid in ("security_service", "legal_service") or sysid == "support_platform":
        return "ABSENT" if f.get("rows") == "0" else "PRESENT"
    if sysid == "account_service" and f.get("account_status") == "DELETED" and not o.emails:
        return "ABSENT"
    return "PRESENT" if sysid else "UNKNOWN"


def readings(index, rid, audit_at, clock="two", subject_files=frozenset(), requested_at=None):
    """All readings of `rid` known at audit_at, oldest first. With `requested_at`, an account may also get an ABSENT
    reading from a drift capture (see drift_absence)."""
    out = []
    for o in index.by_resource.get(rid, []):
        t = info_time(o, index, clock)
        if t > audit_at:
            continue
        citable = parse_time(index.manifest[o.file_id]["captured_at"]) <= audit_at
        out.append(Reading(classify(o), t, o.file_id, citable, o.source))
    out += disappearance_readings(index, rid, audit_at, clock, subject_files)
    if requested_at is not None:
        drift = drift_absence(index, rid, requested_at, audit_at, out)
        if drift is not None:
            out.append(drift)
    out.sort(key=lambda r: r.time)
    return out


def drift_absence(index, rid, requested_at, audit_at, known):
    """ABSENT reading for an account from a drift capture: a SQL snapshot of the account table taken after the request
    and by audit_at that enumerates both a primary and a replica copy and lists the account in neither. Guard: no reading
    in `known` shows the account present at or after the request (a record omitted from a small capture but present
    elsewhere is not proven gone). decisions.md has the measurements."""
    from ingest.common import system_of
    if system_of(rid) != "account_service":
        return None
    if any(r.kind in ("PRESENT", "IDENTIFIABLE") and r.time >= requested_at for r in known):
        return None
    by_file = {}
    for s in index.scopes:
        if s.system_id == "account_service" and index.manifest[s.file_id]["file_path"].endswith(".sql")                 and requested_at < parse_time(s.obs_time) <= audit_at:
            by_file.setdefault(s.file_id, []).append(s)
    best = None
    for fid, scopes in by_file.items():
        names = " ".join(s.collection for s in scopes)
        listed = set().union(*[set(s.resource_ids) for s in scopes])
        if "primary" in names and "replica" in names and listed and rid not in listed:
            t = max(parse_time(s.obs_time) for s in scopes)
            if best is None or t > best[0]:
                best = (t, fid)
    if best is None:
        return None
    citable = parse_time(index.manifest[best[1]]["captured_at"]) <= audit_at
    return Reading("ABSENT", best[0], best[1], citable, "scope:drift_capture")


def disappearance_readings(index, rid, audit_at, clock, subject_files):
    """Absence evidence from enumerating snapshots, in the only two cases where it is sound:
      * the record was listed in an earlier snapshot of the same collection and is missing from a later one;
      * the snapshot demonstrably covers the subject (`subject_files`: it holds the subject's own identifiers) and
        does not list the record.
    An empty or unrelated table says nothing: inventories of other people's records are full of them."""
    sysid = next((o.system_id for o in index.by_resource.get(rid, [])), None)
    if sysid is None:
        from ingest.common import system_of
        sysid = system_of(rid)
    scopes = [s for s in index.scopes if s.system_id == sysid and parse_time(s.obs_time) <= audit_at]
    out, by_coll = [], {}
    for s in sorted(scopes, key=lambda s: s.obs_time):
        by_coll.setdefault(s.collection, []).append(s)
    for coll, seq in by_coll.items():
        seen_before = False
        for s in seq:
            present = rid in s.resource_ids
            full_table = index.manifest[s.file_id]["file_path"].endswith(".sql")   # bundles list a per-case subset
            if not present and ((seen_before and full_table) or s.file_id in subject_files):
                citable = parse_time(index.manifest[s.file_id]["captured_at"]) <= audit_at
                out.append(Reading("ABSENT", parse_time(s.obs_time), s.file_id, citable, f"scope:{coll}"))
            seen_before |= present
    return out


def copy_key(r):
    """Replicas are separate copies of the data; every other view (service lookups, exports, ledgers, the primary
    snapshot) reads the same primary store."""
    src = r.source.replace("scope:", "")
    return src if "replica" in src else "primary"


def current(rs, since=None):
    """State to act on. Each copy contributes its latest reading (at or after `since`, the request time); the copy that
    still exposes the most wins, because a deletion is complete only if EVERY copy is compliant."""
    per_copy = {}
    for r in rs:
        if r.kind == "UNKNOWN" or (since is not None and r.time < since):
            continue
        k = copy_key(r)
        best = per_copy.get(k)
        if best is None or r.time > best.time or (r.time == best.time and SEVERITY[r.kind] > SEVERITY[best.kind]):
            per_copy[k] = r
    if not per_copy:
        return None
    return max(per_copy.values(), key=lambda r: (SEVERITY[r.kind], r.time))


def proving_reading(rs, kind_ok, since=None):
    """Newest citable reading whose kind is acceptable (the evidence to cite for a state)."""
    cands = [r for r in rs if r.citable and r.kind in kind_ok and (since is None or r.time >= since)]
    return max(cands, key=lambda r: (r.time, SEVERITY[r.kind])) if cands else None


_EMAIL = re.compile(r"@")

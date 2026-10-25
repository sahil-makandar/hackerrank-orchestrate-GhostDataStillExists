"""SQLite-compatible snapshot exports: executed in memory, every table row becomes an observation.
Table snapshots also yield scopes, which say which records a capture lists.
"""
import json
import re
import sqlite3

from .common import Obs, Scope, system_of


def _flatten(prefix, value, out):
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(f"{prefix}.{k}" if prefix else k, v, out)
    else:
        out[prefix] = value


# Typed (non-JSON) snapshot tables -> the system whose records they enumerate.
TABLE_SYSTEM = {
    "customer_profiles": "account_service", "customer_profiles_primary": "account_service",
    "customer_profiles_failover_replica": "account_service",
    "customer_search_documents": "search_service", "search_documents_primary": "search_service",
    "customer_analytics_profiles": "analytics_platform", "support_records": "support_platform",
    "customer_exports": "export_service", "connector_subscriptions": "integration_service",
    "backup_restore_entries": "backup_service", "security_audit_records": "security_service",
}


def parse(file_id, path, manifest_time):
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"Captured at:\s*(\S+)", text)
    captured = m.group(1) if m else manifest_time
    con = sqlite3.connect(":memory:")
    con.executescript(text)
    obs, scopes = [], []
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table' order by name")]
    # snapshot_scope lists the systems this capture covers; tables of other systems say nothing
    scoped = {r[0] for r in con.execute("select origin_system from snapshot_scope")} if "snapshot_scope" in tables else None
    for table in tables:
        cur = con.execute(f'select * from "{table}"')
        cols = [c[0] for c in cur.description]
        if "resource_id" not in cols:
            continue
        per_system = {}
        owner = TABLE_SYSTEM.get(table)
        if owner and (scoped is None or owner in scoped):
            per_system[owner] = []  # an empty table in scope is evidence of absence
        for row in cur.fetchall():
            rec = dict(zip(cols, row))
            flat = {}
            for k, v in rec.items():
                if k == "record_json":
                    _flatten("", json.loads(v), flat)
                else:
                    flat[k] = v
            rid = rec["resource_id"]
            system = rec.get("origin_system") or system_of(rid) or ""
            o = Obs(file_id, captured, rid, system, table, flat)   # state time = snapshot time
            o.absorb(json.dumps(flat, ensure_ascii=False))
            obs.append(o)
            per_system.setdefault(system, []).append(rid)
        # Each table is a full enumeration of the records it holds at capture time.
        for system, ids in per_system.items():
            scopes.append(Scope(file_id, captured, system, table, ids))
    return obs, scopes, captured

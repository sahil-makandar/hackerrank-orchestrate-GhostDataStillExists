"""JSON evidence: snapshot bundles, service exports, ledgers and job lists.
Each record becomes an observation with its own time, so the two-clock rule can apply.
"""
import json

from .common import Obs, Scope, system_of

TIME_KEYS = ("observed_at", "recorded_at", "captured_at")   # not updated_at: a snapshot record is true at snapshot time


def _time(rec, inherited):
    for k in TIME_KEYS:
        v = rec.get(k)
        if isinstance(v, str):
            return v
    return inherited


def _flat(rec):
    out = {}

    def go(prefix, v):
        if isinstance(v, dict):
            for k, x in v.items():
                go(f"{prefix}.{k}" if prefix else k, x)
        else:
            out[prefix] = v

    go("", rec)
    return out


def parse(file_id, path, manifest_time):
    doc = json.loads(path.read_text(encoding="utf-8"))
    obs, scopes = [], []
    top_time = doc.get("exported_at") or doc.get("captured_at") or manifest_time

    def add(rec, system, source, t):
        rid = rec["resource_id"]
        fields = _flat(rec)
        o = Obs(file_id, _time(rec, t), rid, rec.get("system") or system or system_of(rid) or "", source, fields)
        o.absorb(json.dumps(fields, ensure_ascii=False))
        obs.append(o)

    def walk(node, system, source, t):
        if isinstance(node, dict):
            if isinstance(node.get("resource_id"), str):
                add(node, system, source, t)
                return
            # snapshot bundle entries: {captured_at, data_store, collection, records}
            if "records" in node and ("collection" in node or "data_store" in node):
                st = node.get("captured_at") or t
                sysid = node.get("data_store") or system
                coll = node.get("collection") or source
                ids = [r["resource_id"] for r in node["records"] if isinstance(r, dict) and "resource_id" in r]
                scopes.append(Scope(file_id, st, sysid or "", coll, ids))
                for r in node["records"]:
                    walk(r, sysid, coll, st)
                return
            for k, v in node.items():
                is_system = k.endswith(("_service", "_platform"))
                walk(v, k if is_system else system, source if is_system else k, t)
        elif isinstance(node, list):
            for v in node:
                walk(v, system, source, t)

    walk(doc, None, "", top_time)
    # a service export lists every record it observed for a service, per collection
    for system, groups in doc.get("service_exports", {}).items():
        for coll, recs in groups.items():
            scopes.append(Scope(file_id, top_time, system, coll,
                                [r["resource_id"] for r in recs if "resource_id" in r]))
    return obs, scopes, top_time

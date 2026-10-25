"""Log evidence in six line dialects. Lines of one trace are linked, so the line that names a
resource_id inherits the lookup keys / correlation keys that appear on other lines of that trace."""
import json
import re
from collections import defaultdict

from .common import Obs, RESOURCE_RE, extract_ids, system_of

TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\s+(.*)$")
KV = re.compile(r'([A-Za-z_][\w.]*)=("(?:[^"\\]|\\.)*"|\S+)')
BRACKET = re.compile(r"^\[(\w+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+([\w.]+):\s*(.*)$")
SYSLOG = re.compile(r"^<\d+>\d+\s+(\S+)\s+(\S+)\s+(\S+)\s+-\s+-\s+(.*)$")


def _kv(text):
    out = {}
    for k, v in KV.findall(text):
        out[k] = v[1:-1] if v.startswith('"') and v.endswith('"') else v
    return out


def _parse_line(rest):
    """Return (fields, service, trace, event) for one log line body."""
    if rest.startswith("{"):
        d = {k: ("" if v is None else str(v)) for k, v in json.loads(rest).items()}
        return d, d.get("service") or d.get("logger"), d.get("trace_id"), d.get("event")
    m = BRACKET.match(rest)
    if m:
        level, service, trace, event, tail = m.groups()
        d = _kv(tail)
        d.update(level=level, service=service, trace_id=trace, event=event)
        return d, service, trace, event
    if rest.startswith("| ") or " | " in rest[:40]:
        parts = [p.strip() for p in rest.split("|")]
        if len(parts) >= 6:
            d = _kv(" | ".join(parts[5:]))
            d.update(level=parts[1], service=parts[2], trace_id=parts[3], event=parts[4])
            return d, parts[2], parts[3], parts[4]
    m = SYSLOG.match(rest)
    if m:
        node, service, trace, tail = m.groups()
        d = _kv(tail)
        d.update(service=service, trace_id=trace, node=node)
        return d, service, trace, d.get("event")
    if 'message="' in rest:  # cloud runtime export: message="k=\"v\" ..."
        head, _, msg = rest.partition('message="')
        d = _kv(head)
        msg = msg.rsplit('"', 1)[0].replace('\\"', '"')
        d.update(_kv(msg))
        d["service"] = d.get("function") or d.get("service")
        d["trace_id"] = d.get("requestId")
        return d, d["service"], d["trace_id"], d.get("event")
    d = _kv(rest)
    return d, d.get("service"), d.get("trace_id"), d.get("event")


def parse(file_id, path, manifest_time):
    lines = []
    for n, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        m = TS.match(raw)
        if not m:
            continue
        ts, rest = m.groups()
        fields, service, trace, event = _parse_line(rest)
        fields["line"] = n
        lines.append((ts, service, trace, fields))

    # identifiers (emails / hashes / users / phones) seen anywhere on a trace
    trace_ids = defaultdict(lambda: {"emails": set(), "hashes": set(), "user_ids": set(), "phones": set()})
    for ts, service, trace, fields in lines:
        if not trace:
            continue
        ids = extract_ids(json.dumps({k: v for k, v in fields.items() if k != "resource_id"}))
        for key in trace_ids[trace]:
            trace_ids[trace][key] |= ids[key]

    obs = []
    for ts, service, trace, fields in lines:
        rid = fields.get("resource_id")
        if not rid or not RESOURCE_RE.fullmatch(rid):
            continue
        o = Obs(file_id, ts, rid, system_of(rid) or service or "", f"log:{service}", dict(fields))
        o.absorb(json.dumps(fields))
        if trace:
            t = trace_ids[trace]
            o.emails |= t["emails"]
            o.hashes |= t["hashes"]
            o.user_ids |= t["user_ids"]
            o.phones |= t["phones"]
        obs.append(o)
    return obs, [], manifest_time

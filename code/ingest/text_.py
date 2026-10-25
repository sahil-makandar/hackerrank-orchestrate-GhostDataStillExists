"""Plain-text shift handover ledger: '- time | system | resource | type | result | subject'.
Each ledger line becomes one observation of the resource it names.
"""
import re

from .common import Obs, system_of

LINE = re.compile(r"^-\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(.*)$")


def parse(file_id, path, manifest_time):
    obs = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = LINE.match(line.strip())
        if not m:
            continue
        t, system, rid, rtype, result, subject = m.groups()
        o = Obs(file_id, t, rid, system or system_of(rid) or "", "handover",
                {"record_type": rtype, "result": result, "subject_reference": subject.strip()})
        o.absorb(subject)
        obs.append(o)
    return obs, [], manifest_time

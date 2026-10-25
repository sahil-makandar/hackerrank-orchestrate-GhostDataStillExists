"""The agent's tools. One ToolBox per case; the case's audit_at is bound here, so no tool can show anything later.
The bounded agent reaches them through four actions; the development tests call them directly.

Information clock: an observation is shown only if its own time is at or before audit_at. Citation clock: every evidence
id is marked `citable` only if its file was captured (or its activity / audit row recorded) at or before audit_at.
Every value a tool returns passes `_guard`; a violation is counted (the M5 gate requires zero) and the item is withheld.
"""
import json

import linkage
import state
from typing import Any
from ingest import localocr
from ingest.common import DATASET, RESOURCE_RE, norm_phone, system_of
from ingest.pdf_ import _text as pdf_text
from policy import PREFIX_CATEGORIES, RULES, FLOW_PREFIX, VERIFICATION_FOR, prefix_of, treatment_for_category
from validator import clock as evidence_clock

NOISE = {"line", "instance", "pod", "region", "trace_id", "level", "service", "requestId", "function", "node", "logger",
         "severity", "latency_ms", "duration_ms", "attempt", "shard", "took_ms", "wait_ms", "pool", "seq"}
TIER_ORDER = {"identifier": 0, "constraint-forced": 1, "timing-unique": 2, "timing-ambiguous": 3, "none": 4}
MAX_ITEMS = 25


def _compact(fields, limit=14):
    out = {}
    for k, v in fields.items():
        if k in NOISE or k.startswith("metrics.") or k.startswith("recent_activity"):
            continue
        s = v if isinstance(v, (int, float, bool)) or v is None else str(v)
        out[k] = s[:120] + "..." if isinstance(s, str) and len(s) > 120 else s
        if len(out) >= limit:
            break
    return out


READ_BUDGET_CHARS = 16000      # one read_file result never exceeds this many characters of evidence lines


def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class ToolBox:
    def __init__(self, ctx, case):
        self.ctx, self.case = ctx, case
        self.cid, self.audit_at, self.requested = case["case_id"], case["_audit_at"], case["_requested_at"]
        self.violations = 0
        self.default_user = ctx.user.get(self.cid)
        self.searches = {}         # (terms, systems) -> number of hits, to stop repeated searches
        self.searched_terms = set()  # every term searched in this case (refusals require the subject's identifiers)
        self.id_hits = {}          # resource_id -> evidence ids where a searched term matched one of its identifiers

    # ------------------------------------------------------------------ helpers
    def _guard(self, t):
        """True if a fact with own time t may be shown. Anything later is withheld and counted."""
        if t > self.audit_at:
            self.violations += 1
            return False
        return True

    def citable(self, evidence_id: str) -> bool:
        t = evidence_clock().time_of.get(evidence_id)
        return t is not None and t <= self.audit_at

    def known_obs(self, rid: str) -> list:
        idx = self.ctx.index
        return [o for o in idx.by_resource.get(rid, []) if linkage.info_time(o, idx, self.ctx.clock) <= self.audit_at]

    def identity(self, user_id: str) -> Any:
        return self.ctx.resolver.users.get(user_id)

    def candidates_for(self, user_id: str) -> dict:
        """resource_id -> Candidate (all tiers, ambiguous included) for subject `user_id` in this case."""
        ident = self.identity(user_id)
        if ident is None:
            return {}
        if user_id == self.default_user:
            out = dict(self.ctx.ident[self.cid])
        else:
            out = linkage.identifier_candidates(ident, self.audit_at, self.ctx.index, self.ctx.activity, self.ctx.clock)
        out.update({r: c for r, c in self.ctx.timing.timing_candidates(self.case, self.audit_at, set(out),
                                                                        self.ctx.owners, user_id).items()})
        f = self.ctx.forced.get(self.cid)
        if f and user_id == self.default_user and (f.resource_id not in out or out[f.resource_id].tier.startswith("timing")):
            out[f.resource_id] = f
        w = self.ctx.window.get(self.cid)
        if w and user_id == self.default_user:      # the timing-window account replaces every other non-identifier account
            out = {r: c for r, c in out.items() if not (r.startswith("account_") and c.tier != "identifier")}
            out[w.resource_id] = w
        return out

    def claimable(self, user_id: str) -> set:
        return {r for r, c in self.candidates_for(user_id).items() if c.tier != "timing-ambiguous"}

    def subject_files(self, user_id: str) -> frozenset:
        return frozenset(f for c in self.candidates_for(user_id).values() if c.tier == "identifier"
                         for f in c.evidence_ids + c.later_files)

    # ------------------------------------------------------------------ tools
    def resolve_identity(self, email: str) -> dict:
        users = self.ctx.resolver.resolve(email or "")
        out = []
        for u in users:
            hist = []
            for a in self.ctx.resolver.audit:
                if a["user_id"] != u.user_id:
                    continue
                t = linkage.parse_time(a["event_time"])
                if self._guard(t):
                    hist.append({"audit_id": a["audit_id"], "time": a["event_time"], "field": a["field_name"],
                                 "old": a["old_value"], "new": a["new_value"]})
            out.append({"user_id": u.user_id, "name": u.name, "emails": sorted(u.emails), "email_hashes": sorted(u.hashes),
                        "phones": sorted(u.phones), "identity_history": hist})
        note = {0: "no user ever held this email", 1: "exactly one user"}.get(len(out), "several users held this email")
        return {"matches": out, "note": note}

    def list_candidates(self, user_id: str | None, systems: list | None = None) -> dict:
        cands = self.candidates_for(user_id)
        rows = []
        for rid, c in cands.items():
            if systems and c.system_id not in systems:
                continue
            rows.append({"resource_id": rid, "system_id": c.system_id, "tier": c.tier, "reasons": c.reasons[:2],
                         "citable_evidence": [e for e in c.evidence_ids if self.citable(e)][:4],
                         "later_exports_not_citable": c.later_files[:2]})
        rows.sort(key=lambda r: (TIER_ORDER.get(r["tier"], 9), r["system_id"], r["resource_id"]))
        return {"subject": user_id, "count": len(rows), "candidates": rows[:60], "truncated": len(rows) > 60,
                "tiers": "identifier = carries the subject's identifiers; constraint-forced / timing-unique = no identifier, "
                         "only one case can own it (reason shown, you may refuse); timing-ambiguous = several cases could "
                         "own it: never claim it without new identifier evidence"}

    def account_candidates(self, user_id: str | None) -> dict:
        T, case = self.ctx.timing, self.case
        cands = self.candidates_for(user_id)
        expect = T.status_result.get(case["reported_status"])
        events = [a for a in T.events if a["target_system"] == "account_service" and a["event_type"] == "DELETE_RECORD"
                  and abs(T.offset_h(a, case)) <= T.span_h and self._guard(a["_t"])]
        rids = {a["resource_id"] for a in events} | {r for r in cands if prefix_of(r) == "account"}
        rows = []
        for rid in sorted(rids):
            ev = next((a for a in events if a["resource_id"] == rid), None)
            comp = []
            if ev:
                for oc in sorted(T.competitors(ev) - {self.cid}):
                    o = self.ctx.by_id[oc]
                    anchored = [r for r in self.ctx.ident.get(oc, {}) if prefix_of(r) == "account"]
                    comp.append({"case_id": oc, "reported_at": o["reported_at"], "reported_status": o["reported_status"],
                                 "has_identifier_linked_account": anchored[:2]})
            c = cands.get(rid)
            rows.append({
                "resource_id": rid, "tier": c.tier if c else "none", "reason": (c.reasons[:2] if c else []),
                "delete_event": ({"activity_id": ev["activity_id"], "result": ev["reported_result"],
                                  "offset_h_from_reported_at": round(T.offset_h(ev, case), 2),
                                  "result_matches_reported_status": ev["reported_result"] == expect} if ev else None),
                "carries_other_peoples_identifiers": self.ctx.owners.foreign(rid, self.audit_at, user_id),
                "competing_cases": comp[:8]})
        rows.sort(key=lambda r: (TIER_ORDER.get(r["tier"], 9), r["resource_id"]))
        return {"reported_status_is_a_claim": case["reported_status"], "expected_delete_result_for_that_claim": expect,
                "burst_span_hours": round(T.span_h, 2), "accounts": rows[:MAX_ITEMS],
                "guide": "Claim at most one account. Prefer identifier, then constraint-forced / timing-unique. If only "
                         "timing-ambiguous accounts exist and the competing cases look the same, do not guess: leave the "
                         "account out and say why in notes."}

    def get_resource(self, resource_ids: list, user_id: str | None = None) -> dict:
        user_id = user_id or self.default_user
        out = []
        for rid in list(dict.fromkeys(resource_ids or []))[:8]:
            if not RESOURCE_RE.fullmatch(str(rid)):
                out.append({"resource_id": rid, "error": "not a resource id"})
                continue
            copies = {}
            for o in self.known_obs(rid):
                t = linkage.info_time(o, self.ctx.index, self.ctx.clock)
                if not self._guard(t):
                    continue
                r = state.Reading("", t, o.file_id, self.citable(o.file_id), o.source)
                copies.setdefault(state.copy_key(r), []).append(
                    {"time": _iso(t), "evidence_id": o.file_id, "citable": r.citable, "source": o.source,
                     "fields": _compact(o.fields)})
            for r in state.disappearance_readings(self.ctx.index, rid, self.audit_at, self.ctx.clock,
                                                   self.subject_files(user_id)):
                if self._guard(r.time):
                    copies.setdefault(state.copy_key(r), []).append(
                        {"time": _iso(r.time), "evidence_id": r.file_id, "citable": r.citable, "source": r.source,
                         "fields": {"observation": "this snapshot covers the person (or listed this record before) and "
                                                   "does not list this record"}})
            for k in copies:
                copies[k] = sorted(copies[k], key=lambda x: x["time"], reverse=True)[:5]
            p = prefix_of(rid)
            out.append({"resource_id": rid, "system_id": system_of(rid),
                        "possible_categories": ["DATA_FLOW"] if p == FLOW_PREFIX else PREFIX_CATEGORIES.get(p, []),
                        "copies": copies, "requested_at": self.case["requested_at"],
                        "note": "establish the state yourself from these fields; readings before requested_at are history"})
        return {"resources": out}

    def search_evidence(self, terms: list, systems: list | None = None) -> dict:
        terms = [str(t).strip().lower() for t in (terms or []) if str(t).strip()]
        key = (frozenset(terms), frozenset(systems or []))
        self.searched_terms |= set(terms)
        if key in self.searches:
            return {"repeat": True, "note": f"same search as before ({self.searches[key]} hits); nothing new. "
                                            "Search other identifiers or finish."}
        digits = [norm_phone(t).lstrip("+") for t in terms if sum(ch.isdigit() for ch in t) >= 8]
        hits, seen = [], set()
        idx = self.ctx.index
        for o in idx.observations:
            if systems and o.system_id not in systems:
                continue
            blob = (o.resource_id + " " + " ".join(str(v) for v in o.fields.values())).lower()
            ids = o.emails | o.hashes | {u.lower() for u in o.user_ids}
            if not (any(t in ids or t in blob for t in terms) or any(d and d in {p.lstrip("+") for p in o.phones} for d in digits)):
                continue
            t = linkage.info_time(o, idx, self.ctx.clock)
            if t > self.audit_at:
                continue                      # unknown at audit_at: not shown (not a violation, simply filtered)
            key = (o.resource_id, o.file_id, o.obs_time)
            if key in seen:
                continue
            seen.add(key)
            matched = sorted({t for t in terms if t in ids or t in blob})
            if any(t in ids for t in matched) or any(d and d in {p.lstrip("+") for p in o.phones} for d in digits):
                self.id_hits.setdefault(o.resource_id, set()).add(o.file_id)
            hits.append({"resource_id": o.resource_id, "system_id": o.system_id, "evidence_id": o.file_id, "time": _iso(t),
                         "citable": self.citable(o.file_id), "matched_terms": matched[:3],
                         "fields": _compact(o.fields, 8)})
        hits.sort(key=lambda h: (h["resource_id"], h["time"]))
        self.searches[key] = len(hits)
        note = ("Generic words match everyone's records; search the person's identifiers instead."
                if terms and all(len(t) < 12 and "@" not in t and not t.startswith("user_") for t in terms) else "")
        return {"count": len(hits), "hits": hits[:MAX_ITEMS], "truncated": len(hits) > MAX_ITEMS, "note": note}

    def read_evidence(self, file_id: str, contains: str | None = None) -> dict:
        row = self.ctx.index.manifest.get(file_id)
        if row is None:
            return {"error": f"unknown file_id {file_id}"}
        path = DATASET / row["file_path"]
        captured = linkage.parse_time(row["captured_at"])
        needle = (contains or "").lower()
        lines, hidden = [], 0
        if path.suffix in (".png", ".pdf"):
            if captured > self.audit_at:
                return {"file_id": file_id, "error": "captured after audit_at: its content is unknown at audit time"}
            text = localocr.read(path.read_bytes()) if path.suffix == ".png" else pdf_text(path)
            lines = [ln for ln in text.splitlines() if ln.strip()]
        elif path.suffix == ".log":
            for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
                head = ln[:20]
                try:
                    t = linkage.parse_time(head)
                except ValueError:
                    continue
                if t > self.audit_at:
                    hidden += 1
                    continue
                lines.append(ln)
        else:   # snapshots / ledgers: their records, each only if its own snapshot time is known at audit_at
            for o in (o for obs in self.ctx.index.by_resource.values() for o in obs if o.file_id == file_id):
                t = linkage.info_time(o, self.ctx.index, self.ctx.clock)
                if t > self.audit_at:
                    hidden += 1
                    continue
                lines.append(f"{_iso(t)} | {o.system_id} | {o.source} | {o.resource_id} | {json.dumps(_compact(o.fields), ensure_ascii=False)}")
            for s in self.ctx.index.scopes:
                if s.file_id == file_id and linkage.parse_time(s.obs_time) <= self.audit_at:
                    lines.append(f"{s.obs_time} | {s.system_id} | collection {s.collection} lists {len(s.resource_ids)} records")
        if needle:
            lines = [ln for ln in lines if needle in ln.lower()]
        shown, size = [], 0
        for ln in lines[:60]:
            if size + min(len(ln), 400) > READ_BUDGET_CHARS:
                break
            shown.append(ln[:400])
            size += len(shown[-1])
        return {"file_id": file_id, "path": row["file_path"], "origin": row["origin"], "captured_at": row["captured_at"],
                "citable": captured <= self.audit_at, "lines": shown,
                "truncated": len(lines) > len(shown), "hidden_after_audit_at": hidden}

    def activity(self, resource_ids: list | None = None, window: str | None = None) -> dict:
        T, case = self.ctx.timing, self.case
        rows = []
        want = set(resource_ids or [])
        for a in self.ctx.activity:
            if a["_t"] > self.audit_at:
                continue
            hit = a["resource_id"] in want or any(r in a["details"] for r in want)
            if window == "burst" and abs(T.offset_h(a, case)) <= T.span_h + 0.5:
                hit = True
            if hit:
                rows.append({"activity_id": a["activity_id"], "time": a["event_time"], "event_type": a["event_type"],
                             "source": a["source_system"], "target": a["target_system"], "resource_id": a["resource_id"],
                             "reported_result_is_a_claim": a["reported_result"], "details": a["details"][:100],
                             "offset_h_from_reported_at": round(T.offset_h(a, case), 2)})
        rows.sort(key=lambda r: r["time"])
        return {"count": len(rows), "rows": rows[:30], "truncated": len(rows) > 30}

    def get_policy(self, resource_id: str | None = None) -> dict:
        rules = [{"data_category": c, "condition": r["condition"], "required_action": r["required_action"],
                  "treatment": treatment_for_category(c), "final_state_checked": VERIFICATION_FOR[treatment_for_category(c)]}
                 for c, r in RULES.items()]
        rules.append({"data_category": "DATA_FLOW", "condition": "an active process can copy or recreate the data",
                      "required_action": "DISABLE", "treatment": "DISABLE", "final_state_checked": VERIFICATION_FOR["DISABLE"]})
        out = {"rules": rules}
        if resource_id:
            p = prefix_of(resource_id)
            out["resource"] = {"resource_id": resource_id, "system_id": system_of(resource_id),
                               "legal_categories": ["DATA_FLOW"] if p == FLOW_PREFIX else PREFIX_CATEGORIES.get(p, [])}
        return out


TOOLS = [
    {"name": "resolve_identity", "description": "Every user who ever held this email, with all current and historical identifiers (emails, email hashes, phones) and the audit_id of each change known at audit_at.",
     "parameters": {"type": "object", "properties": {"email": {"type": "string"}}, "required": ["email"]}},
    {"name": "list_candidates", "description": "Pre-pass leads for a subject in this case: resource, system, confidence tier and reasons, supporting evidence ids. Leads, not answers.",
     "parameters": {"type": "object", "properties": {"user_id": {"type": "string"}, "systems": {"type": "array", "items": {"type": "string"}}}, "required": ["user_id"]}},
    {"name": "account_candidates", "description": "Every account-delete event in this case's burst window plus identifier-linked accounts: event result vs the reported status, offset, tier and reason, other-owner flag, competing cases, state hint.",
     "parameters": {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]}},
    {"name": "get_resource", "description": "All readings of up to 8 resources known at audit_at, per copy (primary, each replica): time, state hint, evidence id, citable flag, source and policy-relevant fields.",
     "parameters": {"type": "object", "properties": {"resource_ids": {"type": "array", "items": {"type": "string"}}, "user_id": {"type": "string"}}, "required": ["resource_ids"]}},
    {"name": "search_evidence", "description": "Find observations known at audit_at whose identifiers or fields contain any of the terms (emails, hashes, phones, user ids, names, resource ids).",
     "parameters": {"type": "object", "properties": {"terms": {"type": "array", "items": {"type": "string"}}, "systems": {"type": "array", "items": {"type": "string"}}}, "required": ["terms"]}},
    {"name": "read_evidence", "description": "Raw content of one evidence file known at audit_at: log lines, screenshot OCR rows, PDF text, or snapshot records. Optional case-insensitive filter.",
     "parameters": {"type": "object", "properties": {"file_id": {"type": "string"}, "contains": {"type": "string"}}, "required": ["file_id"]}},
    {"name": "activity", "description": "system_activity rows known at audit_at for some resources, or window='burst' for the deletion-worker rows around this case's reported_at. Results are claims.",
     "parameters": {"type": "object", "properties": {"resource_ids": {"type": "array", "items": {"type": "string"}}, "window": {"type": "string", "enum": ["burst"]}}}},
    {"name": "get_policy", "description": "The deletion rules (category -> required action -> final state) and the categories legal for a resource kind.",
     "parameters": {"type": "object", "properties": {"resource_id": {"type": "string"}}}},
    {"name": "check_draft", "description": "Dry run of submit_audit: returns the status and steps code would write, plus errors and warnings. Does not finish the case.",
     "parameters": {"$ref": "#/submit"}},
    {"name": "submit_audit", "description": "Final answer for this case. Code derives the status and writes the steps; errors are returned if the submission breaks a rule.",
     "parameters": {"$ref": "#/submit"}},
]

SUBMIT_SCHEMA = {
    "type": "object",
    "properties": {
        "authorises_deletion": {"type": "boolean"},
        "intent_reason": {"type": "string"},
        "subject_user_id": {"type": ["string", "null"]},
        "records": {"type": "array", "items": {"type": "object", "properties": {
            "system_id": {"type": "string"}, "resource_id": {"type": "string"},
            "data_category": {"type": "string"},
            "state_at_audit": {"type": "string", "enum": ["PRESENT", "IDENTIFIABLE", "ABSENT", "REDACTED", "ANONYMIZED",
                                                          "ACTIVE", "DISABLED", "BLOCKED"]},
            "evidence_id": {"type": "string"},
            "link_basis": {"type": "string"},
            "link_evidence_id": {"type": "string"},
            "role": {"type": ["string", "null"]}},
            "required": ["system_id", "resource_id", "data_category", "state_at_audit", "evidence_id"]}},
        "refused_candidates": {"type": "array", "items": {"type": "object", "properties": {
            "resource_id": {"type": "string"}, "reason": {"type": "string"}}}},
        "notes": {"type": "string"}},
    "required": ["authorises_deletion", "intent_reason", "records"],
}
for _t in TOOLS:
    if _t["parameters"] == {"$ref": "#/submit"}:
        _t["parameters"] = SUBMIT_SCHEMA

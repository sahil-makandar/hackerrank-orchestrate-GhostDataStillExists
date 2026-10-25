"""Linkage pre-pass: which resources might belong to a case's subject, and how sure are we?
Tiers: identifier, timing-unique, constraint-forced, timing-window, timing-ambiguous (never claimed).

The output is a list of *candidates with confidence labels and reasons*. It never decides state, policy
category or status: the agent verifies every candidate with its tools and may reject any of them.

Tiers
  identifier        the resource (or its trace / activity row) carries an identifier of the subject
                    (current or historical email / email hash / phone, or the user_id)
  timing-unique     no identifier, but exactly one case's deletion-worker burst can own the resource
  timing-ambiguous  no identifier and more than one case could own it -> never claimed, only flagged

Two clocks (decisions.md): an observation's own time (snapshot / event time) decides what can be KNOWN at audit_at;
the exporting file's capture time decides what can be CITED. A file exported after audit_at may therefore
support a link but is never cited.
"""
import collections
import csv
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from ingest.common import DATASET, RESOURCE_RE, extract_ids, system_of


def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@dataclass
class Candidate:
    resource_id: str
    system_id: str
    tier: str                                   # identifier | timing-unique | timing-ambiguous
    reasons: list = field(default_factory=list)
    evidence_ids: list = field(default_factory=list)   # citable file_id / activity_id values (captured <= audit_at)
    later_files: list = field(default_factory=list)    # supporting files exported after audit_at: usable, never cited


def load_activity() -> list:
    with open(DATASET / "system_activity.csv", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["_t"] = parse_time(r["event_time"])
    return rows


def load_requests(name: str = "deletion_requests.csv") -> list:
    with open(DATASET / name, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in ("requested_at", "reported_at", "audit_at"):
            r["_" + k] = parse_time(r[k])
    return rows


def info_time(o: Any, index: Any, clock: str) -> datetime:
    """When the observation counts as known: its own time (two-clock) or also its file's export time (strict)."""
    t = parse_time(o.obs_time)
    if clock == "strict":
        t = max(t, parse_time(index.manifest[o.file_id]["captured_at"]))
    return t


def identifier_candidates(identity: Any, audit_at: datetime, index: Any, activity: list, clock: str = "two") -> dict:
    """Candidates whose evidence carries an identifier of `identity`, using only evidence up to audit_at."""
    out = {}

    def add(rid, sysid, reason, evid, citable=True):
        if not RESOURCE_RE.fullmatch(rid):      # order numbers, statement numbers etc. are facts, not resources
            return
        c = out.setdefault(rid, Candidate(rid, sysid or system_of(rid) or "", "identifier"))
        if reason not in c.reasons:
            c.reasons.append(reason)
        target = c.evidence_ids if citable else c.later_files
        if evid not in target:
            target.append(evid)

    for o in (o for obs in index.by_resource.values() for o in obs):
        if info_time(o, index, clock) > audit_at:      # information clock
            continue
        citable = parse_time(index.manifest[o.file_id]["captured_at"]) <= audit_at   # citation clock
        hit = []
        if o.emails & identity.emails:
            hit.append("email")
        if o.hashes & identity.hashes:
            hit.append("email_hash")
        if identity.user_id in o.user_ids:
            hit.append("user_id")
        if o.phones & identity.phones:
            hit.append("phone")
        if hit:
            note = "" if citable else " (file exported after audit_at: not citable)"
            add(o.resource_id, o.system_id, f"{o.file_id}: matched {'+'.join(hit)}{note}", o.file_id, citable)

    emails = identity.emails
    for a in activity:  # activity rows that name the subject (e.g. LINK_RESOURCE_IDENTITY)
        if a["_t"] > audit_at:
            continue
        ids = extract_ids(a["details"])
        if ids["emails"] & emails or identity.user_id in ids["user_ids"]:
            add(a["resource_id"], a["target_system"], f'{a["activity_id"]}: {a["event_type"]} names the subject',
                a["activity_id"])
    return out


class TimingModel:
    """Which cases' deletion-worker bursts can own a deletion-worker event?

    Offsets of each (event_type, target_system) relative to a case's reported_at are learned ONLY from resources
    that already carry an identifier link (self-supervised; sample answers are never used). The account-delete
    result is additionally matched against the case's reported_status, a mapping learned the same way.
    """
    MAX_OFFSET_H = 6.0       # offsets beyond this are other contexts (the same resource reused), not the burst
    TRIM = 0.005             # quantile trimmed from each side before taking the range
    MARGIN_H = 0.05

    def __init__(self, cases, event_rows, subject_of=None):
        self.cases = cases
        self.subject_of = subject_of or {}   # case_id -> user_id; two cases of one person do not compete
        self.events = [a for a in event_rows if a["source_system"] == "deletion_worker"]
        self.by_resource = {}
        for a in self.events:
            self.by_resource.setdefault(a["resource_id"], []).append(a)
        self.ranges = {}
        self.medians = {}
        self.status_result = {}
        self._owners = {}

    @staticmethod
    def key(a: dict) -> tuple:
        # the result is part of the key: a failed action and a successful one happen at different offsets, and
        # successful anonymisations carry no identifier, so they can never be anchored (-> no claim, not a wrong claim)
        return (a["event_type"], a["target_system"], a["reported_result"])

    @staticmethod
    def offset_h(a: dict, case: dict) -> float:
        return (a["_t"] - case["_reported_at"]).total_seconds() / 3600

    def fit(self, anchored: list) -> "TimingModel":
        """anchored: [(case, [resource_id, ...])] resources linked to that case by identifiers."""
        offs, pair = {}, {}
        for case, rids in anchored:
            for rid in rids:
                for a in self.by_resource.get(rid, []):
                    o = self.offset_h(a, case)
                    if abs(o) <= self.MAX_OFFSET_H:
                        offs.setdefault(self.key(a), []).append(o)
                        if self.key(a)[:2] == ("DELETE_RECORD", "account_service"):
                            pair.setdefault(case["reported_status"], {}).setdefault(a["reported_result"], 0)
                            pair[case["reported_status"]][a["reported_result"]] += 1
        for k, v in offs.items():
            v.sort()
            lo = v[int(self.TRIM * len(v))] - self.MARGIN_H
            hi = v[len(v) - 1 - int(self.TRIM * len(v))] + self.MARGIN_H
            self.ranges[k] = (lo, hi, len(v))
        self.medians = {k: v[len(v) // 2] if len(v) % 2 else (v[len(v) // 2 - 1] + v[len(v) // 2]) / 2
                        for k, v in offs.items()}      # learned median offset per event key (hours from reported_at)
        self.status_result = {s: max(r, key=r.get) for s, r in pair.items()}
        # A burst never spans more than the widest learned range; any other case whose reported_at is that close
        # could own the event, whatever regime (failed / succeeded) the event is in.
        self.span_h = max((max(abs(lo), abs(hi)) for lo, hi, _ in self.ranges.values()), default=0.0)
        self._owners = {}
        self._near = {}
        return self

    def owners(self, a: dict) -> set:
        """Case ids whose burst could own event `a`. Empty if the event type was never seen with an identifier."""
        if a["activity_id"] in self._owners:
            return self._owners[a["activity_id"]]
        rng = self.ranges.get(self.key(a))
        if not rng:
            return set()
        out = set()
        for c in self.cases:
            if not rng[0] <= self.offset_h(a, c) <= rng[1]:
                continue
            if self.key(a) == ("DELETE_RECORD", "account_service") and \
                    self.status_result.get(c["reported_status"]) not in (None, a["reported_result"]):
                continue
            out.add(c["case_id"])
        self._owners[a["activity_id"]] = out
        return out

    def competitors(self, a: dict) -> set:
        """Cases whose burst could contain event `a`, judged by distance alone (regime-independent)."""
        if a["activity_id"] in self._near:
            return self._near[a["activity_id"]]
        out = set()
        for c in self.cases:
            if abs(self.offset_h(a, c)) > self.span_h:
                continue
            if self.key(a)[:2] == ("DELETE_RECORD", "account_service") and                     self.status_result.get(c["reported_status"]) not in (None, a["reported_result"]):
                continue
            out.add(c["case_id"])
        self._near[a["activity_id"]] = out
        return out

    def timing_candidates(self, case: dict, audit_at: datetime, known: set, owners: "OwnerIndex | None" = None, subject: str | None = None) -> dict:
        """Timing-only candidates for `case`; resources already in `known` (identifier tier) are skipped.

        If `owners` (an OwnerIndex) is given, a resource that carries only other people's identifiers in evidence
        available at audit_at is never claimed: negative evidence beats timing.
        """
        out = {}
        for rid, evs in self.by_resource.items():
            if rid in known:
                continue
            if owners is not None and owners.foreign(rid, audit_at, subject):
                continue
            evs = [a for a in evs if a["_t"] <= audit_at]
            mine = [a for a in evs if case["case_id"] in self.owners(a)]
            if not mine:
                continue
            me = self.subject_of.get(case["case_id"])
            others = set()
            for a in evs:
                others |= {c for c in self.competitors(a) if self.subject_of.get(c) != me or me is None}
            others.discard(case["case_id"])
            tier = "timing-ambiguous" if others else "timing-unique"
            reasons = [f'{a["activity_id"]}: {a["event_type"]} {a["reported_result"]} at {self.offset_h(a, case):+.2f}h from reported_at'
                       for a in mine]
            out[rid] = Candidate(rid, system_of(rid) or mine[0]["target_system"], tier, reasons,
                                 [a["activity_id"] for a in mine])
            if others:
                out[rid].reasons.append("also plausible for other cases: " + ", ".join(sorted(others)[:6]))
        return out


class OwnerIndex:
    """Who do the identifiers on a resource belong to? Used only for negative evidence (resource is someone else's)."""

    def __init__(self, resolver, index, activity, clock="two"):
        email_map, hash_map, phone_map = {}, {}, {}
        for uid, ident in resolver.users.items():
            for e in ident.emails:
                email_map.setdefault(e, set()).add(uid)
            for h in ident.hashes:
                hash_map.setdefault(h, set()).add(uid)
            for p in ident.phones:
                phone_map.setdefault(p, set()).add(uid)
        self._entries = {}   # rid -> [(time, frozenset(person keys))]

        def keys(emails, hashes, phones, user_ids):
            out = set(user_ids)                      # decoy user ids (not in user_identities) are still "someone"
            for e in emails:
                out |= email_map.get(e) or {"email:" + e}
            for h in hashes:
                out |= hash_map.get(h) or {"hash:" + h}
            for p in phones:
                out |= phone_map.get(p) or set()
            return out

        for obs in index.by_resource.values():
            for o in obs:
                k = keys(o.emails, o.hashes, o.phones, o.user_ids)
                if k:
                    t = info_time(o, index, clock)
                    self._entries.setdefault(o.resource_id, []).append((t, frozenset(k)))
        for a in activity:
            ids = extract_ids(a["details"])
            k = keys(ids["emails"], ids["hashes"], ids["phones"], ids["user_ids"])
            if k:
                self._entries.setdefault(a["resource_id"], []).append((a["_t"], frozenset(k)))

    def foreign(self, rid: str, audit_at: datetime, subject: str | None) -> bool:
        """True if, by audit_at, the resource carries identifiers of other people and none of the subject's."""
        seen = set()
        for t, k in self._entries.get(rid, ()):
            if t <= audit_at:
                seen |= k
        return bool(seen) and subject not in seen


class ForcedMatcher:
    """constraint-forced tier: account-delete events that can only belong to one case once ALL cases are considered.

    Premise (checked on identifier-anchored data: no case owns two account-delete events in its burst): each case owns at
    most one account-delete event and each event belongs to at most one case. A per-case check can only claim an event
    nobody else could own; a global assignment can also claim an event whose other possible owners are all provably busy.

    Safety rules, because "exactly one each" is not strictly true (550 requests vs 539 events):
      1. identifier-anchored cases and events, and accounts carrying other people's identifiers, leave the graph first;
      2. a connected component with more cases than events is skipped: some case in it owns nothing and we cannot say
         which one, so any forced-looking match there could be a neighbour's event pushed onto the wrong case;
      3. a match is claimed only if it is in EVERY maximum assignment of its component, and
      4. it stays in every maximum assignment when any ONE other case of the component is removed (that case owning no
         event must not change the answer).
    """
    EVENT = ("DELETE_RECORD", "account_service")
    TIER = "constraint-forced"
    REASON_TAIL = ("this match is in every complete assignment and stays so if any one other case owns no event. "
                   "Premise: one account-delete event per case.")

    def __init__(self, cases, model, ident_cands, owners, subject_of):
        self.cases = {c["case_id"]: c for c in cases}
        self.model, self.ident, self.owners, self.subject_of = model, ident_cands, owners, subject_of
        self.events = {a["activity_id"]: a for a in model.events if (a["event_type"], a["target_system"]) == self.EVENT}
        by_rid = {}
        for e, a in self.events.items():
            by_rid.setdefault(a["resource_id"], set()).add(e)
        # events whose account carries an identifier of the case's subject (the case is anchored, the event is owned)
        self.anchor_events = {cid: set().union(*[by_rid.get(r, set()) for r in cands]) for cid, cands in ident_cands.items()}

    def _graph(self, unanchor=()):
        """Adjacency case -> {event}, after removing anchored cases/events. `unanchor`: cases treated as unanchored."""
        owned_events, anchored = set(), set()
        for cid, evs in self.anchor_events.items():
            if evs and cid not in unanchor:
                owned_events |= evs
                anchored.add(cid)
        adj = {}
        for e, a in self.events.items():
            if e in owned_events:
                continue
            for cid in self.model.owners(a):
                c = self.cases.get(cid)
                if c is None or cid in anchored or a["_t"] > c["_audit_at"]:
                    continue
                if self.owners.foreign(a["resource_id"], c["_audit_at"], self.subject_of.get(cid)):
                    continue
                adj.setdefault(cid, set()).add(e)
        return adj

    @staticmethod
    def components(adj: dict) -> list:
        ev_cases = {}
        for c, es in adj.items():
            for e in es:
                ev_cases.setdefault(e, set()).add(c)
        seen, comps = set(), []
        for start in adj:
            if start in seen:
                continue
            stack, cs, es = [start], set(), set()
            while stack:
                c = stack.pop()
                if c in seen:
                    continue
                seen.add(c)
                cs.add(c)
                for e in adj[c]:
                    es.add(e)
                    stack.extend(ev_cases[e] - seen)
            comps.append((cs, es))
        return comps

    @staticmethod
    def max_matching(cases: set, adj: dict, banned: tuple | None = None) -> tuple:
        match = {}

        def augment(c, seen):
            for e in sorted(adj[c]):
                if (c, e) == banned or e in seen:
                    continue
                seen.add(e)
                if e not in match or augment(match[e], seen):
                    match[e] = c
                    return True
            return False

        size = sum(1 for c in sorted(cases) if augment(c, set()))
        return size, match

    def is_forced(self, cases: set, adj: dict, c: str, e: str) -> bool:
        size, _ = self.max_matching(cases, adj)
        return self.max_matching(cases, adj, banned=(c, e))[0] < size

    def run(self, unanchor: Iterable[str] = ()) -> tuple:
        """case_id -> Candidate (tier 'constraint-forced'); plus a per-component log for transparency."""
        adj = self._graph(unanchor)
        out, log = {}, []
        for cs, es in self.components(adj):
            sub = {c: adj[c] for c in cs}
            if len(cs) > len(es):
                log.append((sorted(cs), sorted(es), "skipped: more cases than events"))
                continue
            _, match = self.max_matching(cs, sub)
            for e, c in sorted(match.items()):
                if not self.is_forced(cs, sub, c, e):
                    continue
                robust = all(self.is_forced(cs - {x}, {k: v for k, v in sub.items() if k != x}, c, e) for x in cs - {c})
                if not robust:
                    continue
                a = self.events[e]
                others = sorted(cs - {c})
                reason = (f"{e}: {a['event_type']} {a['reported_result']} on {a['resource_id']} at "
                          f"{self.model.offset_h(a, self.cases[c]):+.2f}h from reported_at. {len(cs)} case(s) compete for "
                          f"{len(es)} account-delete event(s) in this window ({', '.join(others[:6]) or 'no other case'}); "
                          f"{self.REASON_TAIL}")
                out[c] = Candidate(a["resource_id"], system_of(a["resource_id"]) or "account_service", self.TIER,
                                   [reason], [e])
            log.append((sorted(cs), sorted(es), "matched"))
        return out, log


class WindowMatcher(ForcedMatcher):
    """timing-window tier: the same global one-to-one assignment, over a fixed tight window instead of the learned ranges.

    Candidate events of a case: account-delete events whose result matches the case's reported_status
    (COMPLETE -> SUCCESS, PARTIALLY_COMPLETE -> PARTIAL, NOT_STARTED -> FAILED), within [LO, HI] hours of reported_at,
    known by audit_at. Events of identifier-anchored cases and accounts carrying only other people's identifiers leave
    the graph first. A claim is made under the same rules as constraint-forced (in every maximum assignment, robust to
    removing any one other case, components with more cases than events skipped). Used for verification steps only.
    Measurements: decisions.md, evaluation/dev/burst_linkage.py and the hidden-link test in evaluation/dev/forced_gate.py.
    """
    TIER = "timing-window"
    REASON_TAIL = ("the result matches the case's reported_status and the event is inside the tight window; this match is in "
                   "every complete assignment and stays so if any one other case owns no event.")
    RESULT = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}
    LO, HI = -1.4, 0.2
    MARGIN_H = 0.2        # margin rule: a case without a strict claim takes the candidate that beats the second by this much

    def run(self, unanchor: Iterable[str] = ()) -> tuple:
        """Strict claims first (parent rules); then the margin rule for the cases left without a claim: the window
        candidate closest to the learned median offset, if it beats the second-closest by MARGIN_H hours and no other
        claim or margin pick uses the event. Hidden-link measurements: decisions.md."""
        out, log = super().run(unanchor)
        adj = self._graph(unanchor)
        used = {c.evidence_ids[0] for c in out.values()}
        picks = {}
        for cid, events in adj.items():
            if cid in out:
                continue
            ranked = sorted((abs(self.model.offset_h(self.events[e], self.cases[cid])
                                 - self.model.medians.get(self.model.key(self.events[e]), 0.0)), e)
                            for e in events if e not in used)
            if len(ranked) >= 2 and ranked[1][0] - ranked[0][0] >= self.MARGIN_H:
                picks[cid] = (ranked[0][1], ranked[1][0] - ranked[0][0], len(ranked))
        taken = collections.Counter(e for e, _, _ in picks.values())
        for cid, (e, margin, n) in sorted(picks.items()):
            if taken[e] > 1:
                continue                      # two cases picked the same event: neither is claimed
            a = self.events[e]
            reason = (f"{e}: {a['event_type']} {a['reported_result']} on {a['resource_id']} at "
                      f"{self.model.offset_h(a, self.cases[cid]):+.2f}h from reported_at; margin rule: closest of {n} window "
                      f"candidates to the learned median, ahead of the next by {margin:.2f} h (at least {self.MARGIN_H} h).")
            out[cid] = Candidate(a["resource_id"], system_of(a["resource_id"]) or "account_service", self.TIER, [reason], [e])
        return out, log

    def _graph(self, unanchor=()):
        owned, anchored = set(), set()
        for cid, evs in self.anchor_events.items():
            if evs and cid not in unanchor:
                owned |= evs
                anchored.add(cid)
        adj = {}
        for e, a in self.events.items():
            if e in owned:
                continue
            for cid, c in self.cases.items():
                if cid in anchored or self.RESULT.get(c["reported_status"]) != a["reported_result"] or a["_t"] > c["_audit_at"]:
                    continue
                if not self.LO <= self.model.offset_h(a, c) <= self.HI:
                    continue
                if self.owners.foreign(a["resource_id"], c["_audit_at"], self.subject_of.get(cid)):
                    continue
                adj.setdefault(cid, set()).add(e)
        return adj

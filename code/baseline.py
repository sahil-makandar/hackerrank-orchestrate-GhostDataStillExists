"""Deterministic baseline: the same pipeline the agent uses, with every judgement made by fixed rules.
`assess` returns the row for one case; it is also the per-case fallback of the agent.

It exists for three reasons: a floor to compare the agent against, a per-case fallback when the agent fails, and a test
of the policy / step builder / validator. It never reads sample answers and never looks at case ids.
"""
import os
import sys
from typing import Any

import linkage                          # noqa: E402
import state                            # noqa: E402
import validator                        # noqa: E402
from identity import IdentityResolver   # noqa: E402
from ingest import build                # noqa: E402
from ingest.common import system_of  # noqa: E402
from intent import authorises_deletion  # noqa: E402,F401  (re-exported for tests)
from policy import prefix_of            # noqa: E402
from steps import Item, build_partial, build_steps  # noqa: E402

# kinds that mean "a copy still exposes the subject" / "is in the required state", per treatment
NON_COMPLIANT = {"DELETE": {"PRESENT", "IDENTIFIABLE"}, "REDACT": {"PRESENT", "IDENTIFIABLE"},
                 "ANONYMIZE": {"PRESENT", "IDENTIFIABLE"}, "BLOCK": {"ACTIVE"}, "DISABLE": {"ACTIVE"}, "RETAIN": set()}
COMPLIANT = {"DELETE": {"ABSENT"}, "REDACT": {"REDACTED"}, "ANONYMIZE": {"ANONYMIZED"},
             "BLOCK": {"BLOCKED"}, "DISABLE": {"DISABLED"}, "RETAIN": {"PRESENT", "IDENTIFIABLE"}}


class Context:
    """Everything the pipeline needs, built once: evidence index, identities, activity and the timing model."""

    def __init__(self, clock="two"):
        self.clock = clock
        self.index = build.load()
        self.resolver = IdentityResolver()
        self.activity = linkage.load_activity()
        self.samples = linkage.load_requests("sample_deletion_requests.csv")
        self.cases = self.samples + linkage.load_requests()
        self.by_id = {c["case_id"]: c for c in self.cases}
        self.subject = {c["case_id"]: (self.resolver.resolve(c["email"]) or [None])[0] for c in self.cases}
        self.ident = {cid: (linkage.identifier_candidates(i, self.by_id[cid]["_audit_at"], self.index, self.activity, clock)
                            if i else {}) for cid, i in self.subject.items()}
        user = {cid: (i.user_id if i else None) for cid, i in self.subject.items()}
        self.owners = linkage.OwnerIndex(self.resolver, self.index, self.activity, clock)
        self.timing = linkage.TimingModel(self.cases, self.activity, user)
        self.timing.fit([(self.by_id[c], list(v)) for c, v in self.ident.items()])
        self.user = user
        self.forced, self.forced_log = linkage.ForcedMatcher(self.cases, self.timing, self.ident, self.owners, user).run()
        self.window, self.window_log = linkage.WindowMatcher(self.cases, self.timing, self.ident, self.owners, user).run()

    def candidates(self, case: dict) -> dict:
        """Claimable candidates: identifier, timing-unique, constraint-forced and timing-window tiers (ambiguous ones are
        never claimed). A timing-window account takes the place of the other timing-tier accounts of its case."""
        cid = case["case_id"]
        out = dict(self.ident[cid])
        for rid, c in self.timing.timing_candidates(case, case["_audit_at"], set(out), self.owners, self.user[cid]).items():
            if c.tier == "timing-unique":
                out[rid] = c
        f = self.forced.get(cid)
        if f and f.resource_id not in out:
            out[f.resource_id] = f
        w = self.window.get(cid)
        if w:
            out = {r: c for r, c in out.items() if not (r.startswith("account_") and c.tier != "identifier")}
            out[w.resource_id] = w
        return out

    def window_account_item(self, case: dict, rid: str, row_incomplete: bool) -> Item | None:
        """Verification-only Item for a timing-window account, or None. Never a DELETE: an account that is not provably
        gone is left out. With no reading at all it is added only to an INCOMPLETE row (an uncited check for later); a
        COMPLETE row needs a citable proof of the state, so an unprovable account is skipped there."""
        audit_at = case["_audit_at"]
        subject_files = frozenset(f for c in self.ident[case["case_id"]].values() for f in c.evidence_ids + c.later_files)
        rs = state.readings(self.index, rid, audit_at, self.clock, subject_files, case["_requested_at"])
        cur = state.current(rs, case["_requested_at"])
        if cur is not None and cur.kind not in COMPLIANT["DELETE"]:
            return None
        proof = state.proving_reading(rs, COMPLIANT["DELETE"], case["_requested_at"]) if cur is not None else None
        if proof is None and not row_incomplete:
            return None
        return Item(system_of(rid), rid, "DELETE", True, verify_evidence=proof.file_id if proof else "", always_verify=True)


def treatment_for(rid: str, obs: list) -> str | None:
    """Data category -> treatment, by resource kind and what the record looks like (fixed heuristics)."""
    p = prefix_of(rid)
    if p in ("account", "search", "cache", "export", "marketing", "object"):
        return "DELETE"
    if p == "email_delivery" or p == "tombstone":
        return "REDACT"
    if p in ("audit_record", "legal_hold"):
        return "RETAIN"
    if p == "backup":
        return "BLOCK"
    if p == "data_flow":
        return "DISABLE"
    if p == "analytics":
        optin = any("email_opt_in" in o.fields or o.fields.get("event") == "contact_lookup_completed" for o in obs)
        return "DELETE" if optin else "ANONYMIZE"
    if p == "support":
        message = any(k in o.fields for o in obs for k in ("message_text", "message_excerpt")) or \
            any(o.fields.get("record_type") == "SUPPORT_MESSAGE" or str(o.fields.get("event", "")).startswith("message_")
                for o in obs)
        return "REDACT" if message else "DELETE"
    return None


def _retained_audit_ok(obs, audit_at):
    """Security audit records are retained only inside their retention period (when the evidence states it)."""
    for o in obs:
        until = o.fields.get("retention_until")
        if until and linkage.parse_time(until) < audit_at:
            return False
    return True


def _pick_account(ctx, case, cands):
    accounts = {r: c for r, c in cands.items() if prefix_of(r) == "account"}
    timed = [r for r, c in accounts.items() if c.tier in ("timing-unique", "constraint-forced", "timing-window")]
    if len(timed) == 1:
        return timed[0]
    return next(iter(accounts)) if len(accounts) == 1 else None


def _partial(case, items, notes, reason):
    """Never certify what cannot be proven: INCOMPLETE, with the checks we can state (or [] when there are none)."""
    steps = build_partial(items)
    errors = validator.validate(case["case_id"], "INCOMPLETE", steps, case["_audit_at"], partial=True)
    if errors:   # a partial row must itself be valid; otherwise fall back to nothing at all
        notes.append("partial row dropped: " + validator.format_errors(errors)[:150])
        steps = []
    return {"status": "INCOMPLETE", "steps": steps, "notes": notes, "unresolved": reason, "partial": True}


def assess(ctx: Context, case: dict) -> dict:
    """Return {status, steps, notes, unresolved}. `unresolved` is set when no valid row can be produced."""
    notes = []
    if not authorises_deletion(case["request_text"]):
        return {"status": "NOT_APPLICABLE", "steps": [], "notes": notes, "unresolved": None}
    cid, audit_at = case["case_id"], case["_audit_at"]
    if ctx.subject[cid] is None:
        return _partial(case, [], notes, "request email resolves to no user")
    cands = ctx.candidates(case)
    subject_files = frozenset(f for c in ctx.ident[cid].values() for f in c.evidence_ids + c.later_files)
    account = _pick_account(ctx, case, cands)
    items, window_account = [], None
    for rid, cand in sorted(cands.items()):
        p = prefix_of(rid)
        if p == "account" and rid != account:
            continue
        if p == "account" and cand.tier == "timing-window":
            window_account = rid            # verification only: added after the loop, once the row's status is known
            continue
        obs = ctx.index.by_resource.get(rid, [])
        treat = treatment_for(rid, obs)
        rs = state.readings(ctx.index, rid, audit_at, ctx.clock, subject_files, case["_requested_at"])
        since = None if treat == "RETAIN" else case["_requested_at"]   # history before the request proves nothing
        cur = state.current(rs, since)
        if treat is None or cur is None:
            notes.append(f"{rid}: no usable state")
            continue
        system = system_of(rid)
        always = p in ("account", "audit_record")
        if p == "audit_record" and not _retained_audit_ok(obs, audit_at):
            continue
        if cur.kind in NON_COMPLIANT[treat]:
            proof = state.proving_reading(rs, NON_COMPLIANT[treat], since)
            if proof is None:
                notes.append(f"{rid}: non-compliant but no citable evidence")
                continue
            items.append(Item(system, rid, treat, False, finding_evidence=proof.file_id, always_verify=always))
        elif cur.kind in COMPLIANT[treat]:
            if treat == "DELETE" and not always:
                continue       # a deleted copy is simply gone: nothing to verify unless it is the account
            proof = state.proving_reading(rs, COMPLIANT[treat], since)
            items.append(Item(system, rid, treat, True, verify_evidence=proof.file_id if proof else "", always_verify=always))
        else:
            notes.append(f"{rid}: state {cur.kind} fits neither compliant nor non-compliant for {treat}")
    if window_account:
        extra = ctx.window_account_item(case, window_account, any(not i.compliant for i in items))
        if extra is not None:
            items.append(extra)
    status = "INCOMPLETE" if any(not i.compliant for i in items) else "COMPLETE"
    if status == "COMPLETE":
        if not items or not all(i.verify_evidence for i in items):   # a COMPLETE step must cite the evidence proving it
            return _partial(case, items, notes, "cannot prove every required state with citable evidence")
    steps = build_steps(status, items)
    errors = validator.validate(cid, status, steps, audit_at)
    if errors:
        return _partial(case, items, notes, "baseline row failed validation: " + validator.format_errors(errors)[:200])
    return {"status": status, "steps": steps, "notes": notes, "unresolved": None, "items": items}


def run(ctx: Context, cases: list) -> dict:
    return {c["case_id"]: assess(ctx, c) for c in cases}


def gate_report(req: Any, rows: Any) -> bool:
    """M3 gate part 4 (called by evaluation/m3_gate.py). True if the baseline ran and nothing invalid was emitted."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "evaluation"))
    import scoring
    import split
    ctx = Context()
    tuning = set(split.tuning_cases())          # held-out cases are scored only at the end
    samples = [c for c in ctx.samples if c["case_id"] in tuning]
    rows = [r for r in rows if r["case_id"] in tuning]
    results = run(ctx, samples)
    unresolved = {cid: r["unresolved"] for cid, r in results.items() if r["unresolved"]}
    emitted = {cid: (r["status"], r["steps"]) for cid, r in results.items()}
    ref = scoring.load_reference(rows)
    print(f"   baseline: {len(samples) - len(unresolved)} fully resolved rows, {len(unresolved)} partial "
          f"(INCOMPLETE with the validated steps we have) of {len(samples)}")
    for cid, why in list(unresolved.items())[:6]:
        print(f"     unresolved {cid}: {why}")
    s = scoring.score(emitted, ref)
    print("   " + scoring.format_score(f"baseline vs answers, {len(samples)} TUNING cases (partial rows included)", s))
    return len(emitted) == len(samples) and all(r["status"] in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")
                                                for r in results.values())

"""Identity resolution: request email (current or historical) -> user_id and every identifier ever held.
Matching ignores case; an email held by two users returns both, never a guess.
"""
import csv
from collections import defaultdict

from ingest.common import DATASET, norm_phone


class Identity:
    def __init__(self, user_id, name, emails, hashes, phones):
        self.user_id = user_id
        self.name = name
        self.emails = emails      # lower-cased, current + historical
        self.hashes = hashes      # lower-cased, current + historical
        self.phones = phones      # digits/+ only, current + historical


class IdentityResolver:
    def __init__(self):
        self.users = {}
        self.email_to_users = defaultdict(set)
        with open(DATASET / "user_identities.csv", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                self.users[r["user_id"]] = Identity(
                    r["user_id"], r["name"], {r["email"].lower()}, {r["email_hash"].lower()}, {norm_phone(r["phone"])})
        with open(DATASET / "user_audit_log.csv", encoding="utf-8", newline="") as f:
            self.audit = list(csv.DictReader(f))
        for a in self.audit:
            ident = self.users.get(a["user_id"])
            if ident is None:
                continue
            for value in (a["old_value"], a["new_value"]):
                if a["field_name"] == "email":
                    ident.emails.add(value.lower())
                elif a["field_name"] == "email_hash":
                    ident.hashes.add(value.lower())
                elif a["field_name"] == "phone":
                    ident.phones.add(norm_phone(value))
        for u, ident in self.users.items():
            for e in ident.emails:
                self.email_to_users[e].add(u)

    def resolve(self, email):
        """All identities that ever held this email (normally exactly one)."""
        return [self.users[u] for u in sorted(self.email_to_users.get(email.strip().lower(), ()))]

    def audit_ids(self, user_id, identifier):
        """audit_ids of the history rows that introduced/retired an identifier value (for citations)."""
        v = identifier.lower()
        return [a["audit_id"] for a in self.audit
                if a["user_id"] == user_id and v in (a["old_value"].lower(), a["new_value"].lower())]

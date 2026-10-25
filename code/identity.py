"""Identity resolution: current snapshot plus historical audit trail.

The request email may be a current or a historical address, so the resolver
keeps every value a person ever used. Matching is always token-exact: substring
matching would confuse ``user_32`` with ``user_320`` / ``user_322``, which occur
throughout the snapshot bundles.
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


@dataclass
class Subject:
    user_id: str
    name: str = ""
    emails: Set[str] = field(default_factory=set)
    phones: Set[str] = field(default_factory=set)
    hashes: Set[str] = field(default_factory=set)
    current_email: str = ""


class IdentityIndex:
    def __init__(self, dataset_dir: str):
        self.subjects: Dict[str, Subject] = {}
        self.by_email: Dict[str, str] = {}
        self.by_phone: Dict[str, str] = {}
        self.by_hash: Dict[str, str] = {}
        self._load(dataset_dir)

    # ------------------------------------------------------------------ load
    def _load(self, dataset_dir: str) -> None:
        with open(os.path.join(dataset_dir, "user_identities.csv"), newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                subj = Subject(user_id=row["user_id"], name=row.get("name", ""))
                subj.current_email = row.get("email", "")
                for value in (row.get("email"), row.get("phone"), row.get("email_hash")):
                    self._add_value(subj, value)
                self.subjects[subj.user_id] = subj

        path = os.path.join(dataset_dir, "user_audit_log.csv")
        if not os.path.exists(path):
            return
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                subj = self.subjects.get(row["user_id"])
                if not subj:
                    continue
                field_name = row.get("field_name", "")
                for value in (row.get("old_value"), row.get("new_value")):
                    if not value:
                        continue
                    if field_name == "email":
                        self._add_value(subj, value)
                    elif field_name == "phone":
                        self._add_value(subj, value, phone=True)
                    elif field_name == "email_hash":
                        self._add_value(subj, value, email_hash=True)
                    elif field_name == "name" and not subj.name:
                        subj.name = value

    def _add_value(self, subj: Subject, value: Optional[str], phone: bool = False,
                   email_hash: bool = False) -> None:
        if not value:
            return
        value = value.strip()
        if not value or value.lower() == "null":
            return
        if email_hash or len(value) == 64 and re.fullmatch(r"[0-9a-f]{64}", value):
            subj.hashes.add(value.lower())
            self.by_hash.setdefault(value.lower(), subj.user_id)
        elif phone or value.startswith("+"):
            subj.phones.add(value.lower())
            self.by_phone.setdefault(value.lower(), subj.user_id)
        elif "@" in value:
            subj.emails.add(value.lower())
            self.by_email.setdefault(value.lower(), subj.user_id)

    # -------------------------------------------------------------- lookups
    def resolve_email(self, email: str) -> Optional[str]:
        return self.by_email.get((email or "").strip().lower())

    def resolve_fact_subject(self, subject: Dict[str, str]) -> Optional[str]:
        """Map a fact's raw identifier fields onto a known user_id."""
        uid = subject.get("user_id") or subject.get("subject_reference") or subject.get("customer_reference")
        if uid and uid in self.subjects:
            return uid
        ident = subject.get("identifier")
        if ident:
            low = ident.strip().lower()
            if low in self.subjects:
                return low
            if low in self.by_email:
                return self.by_email[low]
            if low in self.by_phone:
                return self.by_phone[low]
            if low in self.by_hash:
                return self.by_hash[low]
        for key in ("email", "requester_email", "owner_email", "customer_email", "subject_email"):
            value = subject.get(key)
            if value:
                found = self.by_email.get(value.strip().lower())
                if found:
                    return found
        for key in ("phone", "requester_phone", "owner_phone"):
            value = subject.get(key)
            if value:
                found = self.by_phone.get(value.strip().lower())
                if found:
                    return found
        value = subject.get("email_hash")
        if value:
            found = self.by_hash.get(value.strip().lower())
            if found:
                return found
        return None

    def identifiers(self, user_id: str) -> List[str]:
        subj = self.subjects.get(user_id)
        if not subj:
            return []
        return [user_id, *sorted(subj.emails), *sorted(subj.phones), *sorted(subj.hashes)]


def identifier_pattern(values: List[str]) -> "re.Pattern[str]":
    """Token-exact alternation so ``user_32`` never matches ``user_320``."""
    parts = sorted({re.escape(v) for v in values if v}, key=len, reverse=True)
    return re.compile(r"(?<![0-9A-Za-z_])(?:" + "|".join(parts) + r")(?![0-9A-Za-z_])", re.I)

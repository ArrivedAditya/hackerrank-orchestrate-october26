"""Case planner: reconstruct state at ``audit_at``, apply policy, emit steps.

Everything here is deterministic. Inputs are the evidence facts, the identity
index and the supplied rule table; the output is the ``deletion_status`` and the
ordered ``steps_json`` array for one case.
"""

from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

import ir
import identity
import policy as policy_mod

# Categories deleted outright when present.
DELETE_CATEGORIES = {
    "USER_PROFILE", "SEARCH_INDEX_ENTRY", "CACHE_ENTRY", "SECONDARY_IDENTIFIER",
    "USER_EXPORT", "MARKETING_PROFILE", "SUPPORT_ATTACHMENT",
}
ANONYMIZE_CATEGORIES = {"ANALYTICS_PROFILE", "PRODUCT_TELEMETRY"}
REDACT_CATEGORIES = {"SUPPORT_MESSAGE", "EMAIL_DELIVERY_LOG"}
RETAIN_CATEGORIES = {"SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"}
# Legal holds are retained by finance/legal order rather than by the deletion
# workflow, so they are not part of the verified record set for a case.
SKIP_RETAIN_CATEGORIES = {"LEGAL_HOLD_RECORD"}

ACCOUNT_CATEGORY = "USER_PROFILE"


@dataclass
class RecordState:
    resource_id: str
    category: str
    system: str
    state: str
    pii: bool
    fact: Optional[ir.Fact]
    first_seen: str = ""
    attrs: Dict[str, str] = field(default_factory=dict)
    facts: List[ir.Fact] = field(default_factory=list)


class CaseAuditor:
    def __init__(self, dataset_dir: str):
        self.dataset_dir = dataset_dir
        self.identity = identity.IdentityIndex(dataset_dir)
        self.rules = policy_mod.load_rules(dataset_dir)
        self.facts, self.snapshots, self.stats = ir.load_evidence(
            dataset_dir, fixtures_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
        )
        self.activity = self._load_activity()
        self._index()

    # ---------------------------------------------------------------- indexes
    def _load_activity(self) -> List[Dict[str, str]]:
        path = os.path.join(self.dataset_dir, "system_activity.csv")
        if not os.path.exists(path):
            return []
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def _index(self) -> None:
        self._activity_positions: Dict[str, List[int]] = {}
        self._activity_ids: Dict[str, str] = {}
        for i, event in enumerate(self.activity):
            rid = event.get("resource_id", "")
            if rid:
                self._activity_positions.setdefault(rid, []).append(i)
                self._activity_ids.setdefault(rid, event.get("activity_id", ""))
        self.resource_owners: Dict[str, set] = {}
        self.by_subject: Dict[str, List[ir.Fact]] = {}
        for fact in self.facts:
            keys = set()
            uid = self.identity.resolve_fact_subject(fact.subject)
            if uid:
                keys.add(uid)
                self.resource_owners.setdefault(fact.resource_id, set()).add(uid)
                # A search document names the account it was built from, so that
                # account belongs to the same person. Without this the activity
                # fallback can attribute someone else's account to the subject.
                source = fact.attrs.get("source_resource_id")
                if isinstance(source, str) and source:
                    self.resource_owners.setdefault(source, set()).add(uid)
            for field_name in ("email", "identifier", "requester_email", "owner_email",
                               "customer_email", "subject_email"):
                value = fact.subject.get(field_name)
                if value and "@" in value:
                    keys.add(value.strip().lower())
            for key in keys:
                self.by_subject.setdefault(key, []).append(fact)
        for uid in self.by_subject:
            self.by_subject[uid].sort(key=lambda f: (f.captured_at, f.evidence_id))
        # category -> evidence ids, used to cite omission-based absence
        self.category_evidence: Dict[Tuple[str, str], Dict[str, set]] = {}
        for fact in self.facts:
            key = (fact.system, fact.category)
            self.category_evidence.setdefault(key, {}).setdefault(fact.evidence_id, set()).add(fact.resource_id)
        self.evidence_meta = {
            e["file_id"]: e for e in ir.load_manifest(self.dataset_dir)
        }

    # ------------------------------------------------------------------ state
    def records_at(self, uid: str, cutoff: str) -> Dict[str, RecordState]:
        """Latest known state per resource at ``cutoff``.

        A fact counts when the event it records happened at or before the
        cutoff. The artifact that carries the fact may have been written later;
        that only limits whether the fact can be *cited*.
        """
        records: Dict[str, RecordState] = {}
        for fact in self.by_subject.get(uid, []):
            if fact.observed_at > cutoff:
                continue
            current = records.get(fact.resource_id)
            if current is None:
                current = RecordState(
                    resource_id=fact.resource_id,
                    category=fact.category,
                    system=fact.system,
                    state=fact.state,
                    pii=fact.pii,
                    fact=fact,
                    first_seen=fact.captured_at,
                    attrs=dict(fact.attrs),
                )
                records[fact.resource_id] = current
            current.facts.append(fact)
            current.facts.sort(key=lambda f: (f.observed_at, f.evidence_id))
            latest = current.facts[-1]
            if latest.attrs.get("_cat_explicit"):
                current.category = latest.category
                current.system = latest.system
                current._cat_locked = True
            elif not getattr(current, "_cat_locked", False):
                current.category = latest.category
                current.system = latest.system
            current.state = _effective_state(latest, current.category)
            current.pii = latest.pii or _subject_has_direct_ids(latest.subject)
            current.fact = latest
            current.attrs = dict(latest.attrs)
        return records

    def _account_from_activity(self, row: Dict[str, str], cutoff: str,
                               others: Optional[List[str]] = None) -> Tuple[Optional[str], str]:
        """Fallback discovery of the account id from the global activity stream.

        Two signals, strongest first:

        1. Co-occurrence: the deletion run for one person emits a short block of
           rows; if any of the person's other resources appears in that block,
           the account row next to it belongs to the same person.
        2. Report time: the account row for a case lands within a couple of
           hours of the workflow's ``reported_at``.

        Used only when no artifact links the subject to an account.
        """
        reported = row.get("reported_at") or ""

        def owned_by_someone_else(resource_id: str) -> bool:
            owners = self.resource_owners.get(resource_id)
            return bool(owners) and uid not in owners

        uid = self.identity.resolve_email(row.get("email", "")) or (row.get("email") or "").strip().lower()

        if others and self._activity_positions:
            votes: Dict[str, float] = {}
            for rid in others[:8]:
                for pos in self._activity_positions.get(rid, []):
                    for offset in range(-6, 7):
                        idx = pos + offset
                        if idx < 0 or idx >= len(self.activity):
                            continue
                        event = self.activity[idx]
                        candidate = event.get("resource_id", "")
                        if not candidate.startswith("account_"):
                            continue
                        when = event.get("event_time", "")
                        if when > cutoff or owned_by_someone_else(candidate):
                            continue
                        votes[candidate] = votes.get(candidate, 0.0) + 1.0 / (abs(offset) + 1)
            if votes:
                best = max(votes.items(), key=lambda item: item[1])
                if best[1] >= 1.0:
                    return best[0], self._activity_ids.get(best[0], "")

        if not reported:
            return None, ""
        best_key: Optional[tuple] = None
        best_rid: Optional[str] = None
        for event in self.activity:
            rid = event.get("resource_id", "")
            if not rid.startswith("account_"):
                continue
            when = event.get("event_time", "")
            if when > cutoff or owned_by_someone_else(rid):
                continue
            distance = abs(_seconds_between(when, reported))
            if distance > 7200:
                continue
            priority = 0 if (event.get("event_type") == "DELETE_RECORD"
                             and event.get("source_system") == "deletion_worker") else 1
            key = (priority, distance)
            if best_key is None or key < best_key:
                best_key = key
                best_rid = rid
        return best_rid, self._activity_ids.get(best_rid or "", "")

    # ------------------------------------------------------------------ steps
    def audit(self, row: Dict[str, str]) -> Tuple[str, List[Dict[str, object]]]:
        uid = self.identity.resolve_email(row["email"]) or (row["email"] or "").strip().lower()
        cutoff = row["audit_at"]
        steps: List[Dict[str, object]] = []
        if not self.is_deletion_request(row.get("request_text", "")):
            return "NOT_APPLICABLE", []

        records = self.records_at(uid, cutoff) if uid else {}
        others = [rid for rid, rec in records.items() if rec.category != ACCOUNT_CATEGORY]
        account = self._pick_account(records, row, cutoff, others)
        findings: List[Dict[str, object]] = []
        remediation: List[Dict[str, object]] = []
        verification: List[Dict[str, object]] = []

        def add_finding(op: str, rid: str, system: str, evidence: str) -> None:
            findings.append(_step("FINDING", system, rid, op, _state_for(op), evidence))

        def add_remediation(op: str, rid: str, system: str) -> None:
            remediation.append(_step("REMEDIATION", system, rid, op, _state_for(op), ""))

        def add_verification(op: str, rid: str, system: str, evidence: str) -> None:
            verification.append(_step("VERIFICATION", system, rid, op, _state_for(op), evidence))

        # 1. The account itself.
        if account is not None:
            if account.state in {"PRESENT", "ACTIVE"}:
                add_finding("REPORT_REMAINING_RECORD", account.resource_id, account.system,
                            self._cite(account, records, cutoff))
                add_remediation("DELETE_RECORD", account.resource_id, account.system)
                add_verification("VERIFY_RECORD_ABSENT", account.resource_id, account.system, "")
            else:
                add_verification("VERIFY_RECORD_ABSENT", account.resource_id, account.system,
                                 self._cite_absence(account, cutoff))

        # 2. Every other record the subject owns.
        for rid, rec in sorted(records.items()):
            if account is not None and rid == account.resource_id:
                continue
            if rec.category == ACCOUNT_CATEGORY:
                continue
            action = self._action_for(rec)
            if action == "DELETE":
                if rec.state in {"PRESENT", "ACTIVE"}:
                    add_finding("REPORT_REMAINING_RECORD", rid, rec.system,
                                self._cite(rec, records, cutoff))
                    add_remediation("DELETE_RECORD", rid, rec.system)
                    add_verification("VERIFY_RECORD_ABSENT", rid, rec.system, "")
            elif action in {"REDACT", "TOMBSTONE"}:
                if rec.state in {"PRESENT", "ACTIVE"} and rec.pii:
                    add_finding("REPORT_EXPOSED_PII", rid, rec.system,
                                self._cite(rec, records, cutoff))
                    add_remediation("REDACT_RECORD", rid, rec.system)
                    add_verification("VERIFY_RECORD_REDACTED", rid, rec.system, "")
                elif rec.state in {"REDACTED"}:
                    add_verification("VERIFY_RECORD_REDACTED", rid, rec.system,
                                     self._cite(rec, records, cutoff))
            elif action == "ANONYMIZE":
                if rec.state in {"PRESENT", "ACTIVE"} and rec.pii:
                    add_finding("REPORT_EXPOSED_PII", rid, rec.system,
                                self._cite(rec, records, cutoff))
                    add_remediation("ANONYMIZE_RECORD", rid, rec.system)
                    add_verification("VERIFY_RECORD_ANONYMIZED", rid, rec.system, "")
                elif rec.state == "ANONYMIZED":
                    add_verification("VERIFY_RECORD_ANONYMIZED", rid, rec.system,
                                     self._cite(rec, records, cutoff))
            elif action == "RETAIN":
                if rec.category in SKIP_RETAIN_CATEGORIES:
                    continue
                if rec.state in {"PRESENT", "ACTIVE", "RETAINED"}:
                    add_verification("VERIFY_RECORD_PRESENT", rid, rec.system,
                                     self._cite(rec, records, cutoff))
            elif action == "RETAIN_BLOCKED":
                if rec.state in {"BLOCKED"}:
                    add_verification("VERIFY_RESTORE_BLOCKED", rid, rec.system,
                                     self._cite(rec, records, cutoff))
                else:
                    add_finding("REPORT_RESTORE_RISK", rid, rec.system,
                                self._cite(rec, records, cutoff))
                    add_remediation("BLOCK_RESTORE", rid, rec.system)
                    add_verification("VERIFY_RESTORE_BLOCKED", rid, rec.system, "")
            elif action == "DATA_FLOW":
                if rec.state in {"DISABLED", "ABSENT"}:
                    add_verification("VERIFY_DATA_FLOW_DISABLED", rid, rec.system,
                                     self._cite(rec, records, cutoff))
                else:
                    add_finding("REPORT_ACTIVE_DATA_FLOW", rid, rec.system,
                                self._cite(rec, records, cutoff))
                    add_remediation("DISABLE_DATA_FLOW", rid, rec.system)
                    add_verification("VERIFY_DATA_FLOW_DISABLED", rid, rec.system, "")

        status = "INCOMPLETE" if findings else "COMPLETE"
        if status == "INCOMPLETE":
            for step in verification:
                step["evidence_id"] = ""
        steps = self._order(findings, remediation, verification)
        return status, steps

    # --------------------------------------------------------------- helpers
    def _pick_account(self, records: Dict[str, RecordState], row: Dict[str, str],
                      cutoff: str, others: Optional[List[str]] = None) -> Optional[RecordState]:
        accounts = [r for r in records.values() if r.category == ACCOUNT_CATEGORY]
        if accounts:
            present = [r for r in accounts if r.state in {"PRESENT", "ACTIVE"}]
            if present:
                return max(present, key=lambda r: (r.fact.captured_at, r.resource_id))
            return max(accounts, key=lambda r: (r.fact.captured_at, r.resource_id))
        rid, activity_id = self._account_from_activity(row, cutoff, others)
        if rid:
            system = "account_service"
            return RecordState(resource_id=rid, category=ACCOUNT_CATEGORY, system=system,
                               state="ABSENT", pii=False, fact=None,
                               attrs={"_activity_id": activity_id})
        return None

    def _action_for(self, rec: RecordState) -> str:
        if rec.category == "DATA_FLOW":
            return "DATA_FLOW"
        if rec.category == "BACKUP_ARCHIVE":
            return "RETAIN_BLOCKED"
        if rec.category == "SUPPORT_MESSAGE":
            return policy_mod.support_action({**rec.attrs, "_category": rec.category})
        if rec.category == "SUPPORT_ATTACHMENT":
            return "DELETE"
        if rec.category in ANONYMIZE_CATEGORIES:
            return "ANONYMIZE"
        if rec.category in REDACT_CATEGORIES:
            return "REDACT"
        if rec.category in RETAIN_CATEGORIES:
            return "RETAIN"
        if rec.category == "ACCOUNT_TOMBSTONE":
            return "TOMBSTONE"
        return self.rules.for_category(rec.category)

    def _cite(self, rec: RecordState, records: Dict[str, RecordState], cutoff: str,
              require_state: Optional[str] = None) -> str:
        """Cite the freshest artifact that is available at the cutoff.

        ``require_state`` keeps the citation on the fact that actually proves
        the reported state instead of a later row about the same resource.
        """
        candidates = [f for f in rec.facts if f.captured_at <= cutoff
                      and (require_state is None or f.state == require_state)]
        if candidates:
            return candidates[-1].evidence_id
        return ""

    def _cite_absence(self, rec: RecordState, cutoff: str) -> str:
        """Cite the freshest artifact that captures the resource's domain and omits it."""
        explicit = [f for f in rec.facts if f.state == "ABSENT" and f.captured_at <= cutoff]
        if explicit:
            return explicit[-1].evidence_id
        key = (rec.system, rec.category)
        best: Optional[str] = None
        for evidence_id, resource_ids in self.category_evidence.get(key, {}).items():
            meta = self.evidence_meta.get(evidence_id, {})
            if meta.get("captured_at", "") > cutoff:
                continue
            if rec.resource_id in resource_ids:
                continue
            if best is None or (meta["captured_at"], evidence_id) > (
                self.evidence_meta[best]["captured_at"], best
            ):
                best = evidence_id
        if best:
            return best
        # Nothing else captured the resource; the global activity stream is the
        # only trace that the deletion reached it.
        return str(rec.attrs.get("_activity_id") or "")

    def _order(self, findings: List[Dict[str, object]], remediation: List[Dict[str, object]],
               verification: List[Dict[str, object]]) -> List[Dict[str, object]]:
        def key(step: Dict[str, object]):
            return (
                policy_mod.PHASE_ORDER[step["phase"]],
                policy_mod.OPERATION_ORDER.index(step["operation"]),
                step["system_id"],
                step["resource_id"],
            )

        ordered = sorted(findings + remediation + verification, key=key)
        for i, step in enumerate(ordered, start=1):
            step["step_no"] = i
        return [_normalise(step) for step in ordered]

    # ----------------------------------------------------------------- intent
    def is_deletion_request(self, text: str) -> bool:
        return classify_intent(text) == "DELETION"

def _seconds_between(a: str, b: str) -> float:
    from datetime import datetime
    try:
        da = datetime.strptime(a, "%Y-%m-%dT%H:%M:%SZ")
        db = datetime.strptime(b, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return 0.0
    return (da - db).total_seconds()


DIRECT_ID_FIELDS = {"name", "display_name", "full_name"}
SURVIVING_REF_FIELDS = {"user_id", "customer_reference", "subject_reference"}


def _subject_has_direct_ids(subject: Dict[str, str]) -> bool:
    """True when a fact still carries identifiers that tie it to a person.

    ``user_id`` is the stable internal reference that a compliant tombstone is
    allowed to keep, so it never counts as a direct identifier.
    """
    for key, value in (subject or {}).items():
        if not value or key in SURVIVING_REF_FIELDS:
            continue
        low = str(value).strip().lower()
        if "@" in low or low.startswith("+") or re.fullmatch(r"[0-9a-f]{64}", low):
            return True
        if key in DIRECT_ID_FIELDS:
            return True
    return False


def _effective_state(fact: ir.Fact, category: str = "") -> str:
    """Account tombstones persist as rows; the state depends on what is still in them.

    ``account_status=DELETED`` describes the account, not the record: the
    tombstone row is still there. If direct identifiers survive it is a PII
    exposure, if they are gone the tombstone is already compliant.
    """
    category = category or fact.category
    if category == "ACCOUNT_TOMBSTONE" and fact.state == "ABSENT":
        return "PRESENT" if fact.pii else "REDACTED"
    if fact.state in {"PRESENT", "ACTIVE"} and not fact.pii:
        # Records kept by policy are judged by what they still contain: a kept
        # row with no direct identifiers has already reached its required state.
        if category in ANONYMIZE_CATEGORIES:
            return "ANONYMIZED"
        if category in REDACT_CATEGORIES:
            return "REDACTED"
    return fact.state


def _state_for(operation: str) -> str:
    return {
        "REPORT_REMAINING_RECORD": "PRESENT",
        "REPORT_EXPOSED_PII": "PRESENT",
        "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
        "REPORT_RESTORE_RISK": "ACTIVE",
        "DISABLE_DATA_FLOW": "DISABLED",
        "BLOCK_RESTORE": "BLOCKED",
        "DELETE_RECORD": "ABSENT",
        "REDACT_RECORD": "REDACTED",
        "ANONYMIZE_RECORD": "ANONYMIZED",
        "VERIFY_RECORD_ABSENT": "ABSENT",
        "VERIFY_RECORD_PRESENT": "PRESENT",
        "VERIFY_RECORD_REDACTED": "REDACTED",
        "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED",
        "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
        "VERIFY_RESTORE_BLOCKED": "BLOCKED",
    }[operation]


def _step(phase: str, system: str, resource_id: str, operation: str, state: str,
          evidence_id: str) -> Dict[str, object]:
    return {
        "step_no": 0,
        "phase": phase,
        "system_id": system,
        "resource_id": resource_id,
        "operation": operation,
        "state": state,
        "evidence_id": evidence_id,
    }


def _normalise(step: Dict[str, object]) -> Dict[str, object]:
    return {
        "step_no": step["step_no"],
        "phase": step["phase"],
        "system_id": step["system_id"],
        "resource_id": step["resource_id"],
        "operation": step["operation"],
        "state": step["state"],
        "evidence_id": step["evidence_id"],
    }


# Intent classification lives here so a cached decision file can override it.
# Requests that explicitly limit or refuse deletion, in the five languages used
# by the corpus. A negation outranks every other signal: a person can name the
# data they want kept while asking for something else entirely.
NEGATIONS = (
    "not asking for deletion", "not a request to delete", "not a deletion request",
    "not a request to erase", "do not close the account", "do not delete",
    "don't delete", "please don't delete", "keep the account", "keep the account as it is",
    "leave my profile", "leave the account", "still need the account", "still use it",
    "without removing any of my records", "no deletion", "isn't a deletion",
    "não peço exclusão", "nao peco exclusao", "não quero excluir", "sem excluir",
    "no solicito la eliminación", "no quiero eliminar", "sin eliminar",
    "ne rien supprimer", "ne supprimez pas", "pas de suppression", "sans supprimer",
    "nicht löschen", "keine löschung", "nichts löschen",
    "削除しないで", "削除せず", "삭제하지 마", "삭제하지마",
)

# Requests for a different privacy action: access, copy, correction, opt-out or
# account help. These are authoritative only when no deletion verb is present.
NON_DELETION_ACTS = (
    "download my account information", "copy of the information held",
    "copy of last year's invoices", "can't access my profile", "resetting the login",
    "surname is misspelled", "correct it", "remove me from marketing lists",
    "stop the promotional emails", "change the email address used",
    "mobile number on my profile is out of date", "tell me what personal information",
    "portable format", "export my data", "marketing preferences", "information request",
    "consulter une copie", "accéder", "télécharger", "corriger", "modifier l'adresse",
    "cópia", "acessar", "corrigir", "atualizar", "descargar", "acceder",
    "auskunft", "berichtigen", "kopie", "사본", "정정", "開示", "訂正", "コピー",
)

# Explicit deletion commands. Localised verbs are listed because the corpus asks
# for deletion in five languages.
DELETE_COMMANDS = (
    "delete", "deletion", "erase", "erasure", "remove my account", "close my account",
    "closed my account", "de-identification", "de-identify", "destroy",
    "exclusão", "exclusao", "excluir", "eliminação", "eliminar", "apagar",
    "eliminación", "suprimir", "borrar",
    "suppression", "supprimer", "effacer",
    "löschung", "löschen", "entfernen",
    "削除", "삭제", "消去",
)

# Verbs that only suggest deletion when nothing else in the text names a
# different privacy action.
WEAK_DELETE_MARKERS = ("remov", "clos", "clean", "purge")


def classify_intent(text: str) -> str:
    """Return ``DELETION`` when the request authorises deletion.

    Order matters: an explicit refusal wins, then an explicit deletion command,
    then a competing privacy action, then weak verbs, then the default. Requests
    in this corpus are overwhelmingly deletion requests, so an unrecognised text
    keeps the deletion reading rather than silently becoming ``NOT_APPLICABLE``.
    """
    low = (text or "").lower()
    if any(marker in low for marker in NEGATIONS):
        return "NOT_DELETION"
    if any(marker in low for marker in DELETE_COMMANDS):
        return "DELETION"
    if any(marker in low for marker in NON_DELETION_ACTS):
        return "NOT_DELETION"
    if any(marker in low for marker in WEAK_DELETE_MARKERS):
        return "DELETION"
    return "DELETION"

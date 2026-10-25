"""Evidence intermediate representation.

Every participant-visible artifact is parsed into ``Fact`` records. A fact is a
single claim about one resource at one capture time:

    Fact(evidence_id, captured_at, system, resource_id, category, subject,
         state, pii, attrs, kind)

``state`` is normalised to the vocabulary the answer format uses
(PRESENT / ABSENT / REDACTED / ANONYMIZED / ACTIVE / DISABLED / BLOCKED /
RETAINED). ``pii`` records whether direct identifiers are still attached to the
record. ``subject`` carries whichever identifier fields the artifact exposes so
the identity graph can link the fact to a person.

Parsers are deliberately tolerant: the corpus uses five different log dialects,
four SQL topologies, twelve JSON collections, one plain-text state table and
binary artifacts. Anything that yields a ``resource_id`` and some state becomes
a fact; anything unparseable is skipped and counted in ``ParseStats``.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

CATEGORY_BY_PREFIX: Dict[str, Tuple[str, str]] = {
    "account": ("USER_PROFILE", "account_service"),
    "search": ("SEARCH_INDEX_ENTRY", "search_service"),
    "analytics": ("ANALYTICS_PROFILE", "analytics_platform"),
    "support": ("SUPPORT_MESSAGE", "support_platform"),
    "export": ("USER_EXPORT", "export_service"),
    "audit_record": ("SECURITY_AUDIT_LOG", "security_service"),
    "data_flow": ("DATA_FLOW", "integration_service"),
    "backup": ("BACKUP_ARCHIVE", "backup_service"),
    "object": ("SUPPORT_ATTACHMENT", "object_storage"),
    "marketing": ("MARKETING_PROFILE", "marketing_platform"),
    "tombstone": ("ACCOUNT_TOMBSTONE", "identity_service"),
    "cache": ("CACHE_ENTRY", "cache_service"),
    "email_log": ("EMAIL_DELIVERY_LOG", "email_delivery_service"),
    "mail": ("EMAIL_DELIVERY_LOG", "email_delivery_service"),
    "telemetry": ("PRODUCT_TELEMETRY", "analytics_platform"),
    "legal": ("LEGAL_HOLD_RECORD", "legal_service"),
    "security": ("SECURITY_AUDIT_LOG", "security_service"),
}

# Table names carry both category and replication topology.
SQL_TABLES: Dict[str, Tuple[str, str]] = {
    "customer_profiles": ("USER_PROFILE", "account_service"),
    "customer_profiles_primary": ("USER_PROFILE", "account_service"),
    "customer_profiles_failover_replica": ("USER_PROFILE", "account_service"),
    "customer_search_documents": ("SEARCH_INDEX_ENTRY", "search_service"),
    "search_documents_primary": ("SEARCH_INDEX_ENTRY", "search_service"),
    "customer_analytics_profiles": ("ANALYTICS_PROFILE", "analytics_platform"),
    "support_records": ("SUPPORT_MESSAGE", "support_platform"),
    "customer_exports": ("USER_EXPORT", "export_service"),
    "security_audit_records": ("SECURITY_AUDIT_LOG", "security_service"),
    "connector_subscriptions": ("DATA_FLOW", "integration_service"),
    "backup_restore_entries": ("BACKUP_ARCHIVE", "backup_service"),
}

# JSON collection name -> (category, system)
JSON_COLLECTIONS: Dict[str, Tuple[str, str]] = {
    "customer_profiles": ("USER_PROFILE", "account_service"),
    "customer_search_documents": ("SEARCH_INDEX_ENTRY", "search_service"),
    "customer_analytics_reports": ("ANALYTICS_PROFILE", "analytics_platform"),
    "support_conversations": ("SUPPORT_MESSAGE", "support_platform"),
    "ticket_attachments": ("SUPPORT_ATTACHMENT", "object_storage"),
    "customer_export_jobs": ("USER_EXPORT", "export_service"),
    "backup_catalog": ("BACKUP_ARCHIVE", "backup_service"),
    "connector_configurations": ("DATA_FLOW", "integration_service"),
    "security_audit_events": ("SECURITY_AUDIT_LOG", "security_service"),
    "deleted_customer_references": ("ACCOUNT_TOMBSTONE", "identity_service"),
    "retained_order_records": ("LEGAL_HOLD_RECORD", "legal_service"),
    "operational_record_history": ("SECURITY_AUDIT_LOG", "security_service"),
}

IDENTITY_FIELDS = ("user_id", "email", "phone", "email_hash", "name",
                   "customer_reference", "subject_reference", "requester_email",
                   "requester_name", "requester_phone", "subject_email",
                   "owner_email", "owner_phone", "customer_email", "display_name")


@dataclass
class Fact:
    """One claim about one resource.

    ``captured_at`` is when the artifact became available (the manifest capture
    time); ``observed_at`` is when the underlying event happened. State is
    reconstructed from ``observed_at`` because an event that happened before
    ``audit_at`` describes the world at ``audit_at`` even when the artifact that
    records it was written slightly later. Citations still require the artifact
    itself to be available at ``audit_at``.
    """

    evidence_id: str
    captured_at: str
    system: str
    resource_id: str
    category: str
    state: str
    subject: Dict[str, str] = field(default_factory=dict)
    pii: bool = False
    attrs: Dict[str, Any] = field(default_factory=dict)
    kind: str = "record"
    observed_at: str = ""

    def __post_init__(self) -> None:
        if not self.observed_at:
            self.observed_at = self.captured_at


@dataclass
class Snapshot:
    """A complete capture of one table: lets us assert absence."""

    evidence_id: str
    captured_at: str
    system: str
    category: str
    resource_ids: set
    topology: str = "primary"


@dataclass
class ParseStats:
    files: int = 0
    facts: int = 0
    snapshots: int = 0
    skipped_files: List[str] = field(default_factory=list)
    unparsed_lines: int = 0


def _initial_category(resource_id: str) -> Tuple[str, str]:
    """Longest-prefix lookup so ``data_flow_1`` does not resolve as ``data``."""
    best = None
    for prefix, value in CATEGORY_BY_PREFIX.items():
        if resource_id.startswith(prefix + "_"):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, value)
    if best:
        return best[1]
    return ("UNKNOWN", "unknown")


def _norm_state(value: str) -> Optional[str]:
    v = (value or "").strip().strip('"').lower()
    if v in {"not_found", "missing", "deleted", "absent", "0", "false"}:
        if v == "false":
            return None
        return "ABSENT"
    if v in {"found", "present", "active", "1", "true", "hit"}:
        if v == "true":
            return None
        return "PRESENT"
    if v in {"identifiable", "exposed", "pii"}:
        return "PRESENT"
    if v in {"anonymized", "anonymised", "unlinked"}:
        return "ANONYMIZED"
    if v in {"redacted"}:
        return "REDACTED"
    if v in {"blocked", "restore_blocked"}:
        return "BLOCKED"
    if v in {"retained", "legal_hold"}:
        return "RETAINED"
    if v in {"disabled", "stopped"}:
        return "DISABLED"
    return None


# ---------------------------------------------------------------- generic text

_KV_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*("([^"]*)"|[^\s]+)')
_BRACKET_SERVICE_RE = re.compile(r"\[(?:DEBUG|INFO|WARN|WARNING|ERROR|TRACE)\]\s*\[([a-z0-9_]+)\]", re.I)
_SYSLOG_SERVICE_RE = re.compile(r"<\d+>\d\s+\S+\s+([a-z0-9_]+)\s")
_PIPE_RE = re.compile(r"^\S+\s*\|\s*(?:DEBUG|INFO|WARN|ERROR)\s*\|\s*([a-z0-9_]+)\s*\|")
_EVENT_RE = re.compile(r"\b(?:event=|\|)(?:\s*)([a-z][a-z0-9_]+)\s*(?:\||$|[:=])")
_BRACKET_EVENT_RE = re.compile(r"\]\s*([a-z][a-z0-9_]+)\s*:")
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")


def parse_kv_line(line: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for m in _KV_RE.finditer(line):
        key = m.group(1).lower()
        val = m.group(3) if m.group(3) is not None else m.group(2)
        out[key] = val
    m = _BRACKET_SERVICE_RE.search(line)
    if m:
        out.setdefault("service", m.group(1))
    else:
        m = _SYSLOG_SERVICE_RE.search(line)
        if m:
            out.setdefault("service", m.group(1))
        else:
            m = _PIPE_RE.search(line)
            if m:
                out.setdefault("service", m.group(1))
    if "event" not in out:
        m = _BRACKET_EVENT_RE.search(line)
        if m:
            out["event"] = m.group(1)
        else:
            m = _EVENT_RE.search(line)
            if m:
                out["event"] = m.group(1)
    m = _TS_RE.match(line)
    if m:
        out.setdefault("__ts", m.group(1))
    return out


def _subject_from(kv: Dict[str, str]) -> Dict[str, str]:
    subject = {}
    for name in IDENTITY_FIELDS:
        val = kv.get(name)
        if val and val.lower() not in {"null", "none", ""}:
            subject[name] = val
    for key in ("lookup_key", "match_key", "subject_key", "customer_key", "identity_key",
                "owner_key", "scan_key", "correlation_key", "matched_value"):
        val = kv.get(key)
        if val and val.lower() not in {"null", "none", ""}:
            subject.setdefault("identifier", val)
    val = kv.get("subject_ref")
    if val and val.lower() not in {"null", "none", ""}:
        subject.setdefault("subject_reference", val)
    if "customer_reference" in subject and "user_id" not in subject:
        ref = subject["customer_reference"]
        if ref.startswith("user_"):
            subject["user_id"] = ref
    if "subject_reference" in subject and "user_id" not in subject:
        ref = subject["subject_reference"]
        if ref.startswith("user_"):
            subject["user_id"] = ref
    return subject


# ------------------------------------------------------------------ log facts

_COMPLETED_SUFFIXES = ("_completed", "_read_completed", "_query_completed")
_NEGATIVE_FIELDS = {
    "rows": lambda v: v.strip('"') in {"0", ""},
    "http_status": lambda v: v.strip('"') in {"404", "410"},
    "result": lambda v: v.strip('"').upper() in {"NOT_FOUND", "MISSING", "ABSENT"},
    "record_present": lambda v: v.strip('"').lower() == "false",
    "archive_present": lambda v: v.strip('"').lower() == "false",
    "present": lambda v: v.strip('"').lower() == "false",
}


def parse_log(text: str, evidence_id: str, captured_at: str) -> Tuple[List[Fact], ParseStats]:
    facts: List[Fact] = []
    stats = ParseStats()
    rows: List[Dict[str, str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        raw = line.strip()
        if raw.endswith("}") and "{" in raw:
            body = raw[raw.index("{"):]
            kv = _parse_json_line(body)
        else:
            kv = parse_kv_line(raw)
            m = re.search(r"\[(tr-[0-9A-Za-z-]+)\]", raw)
            if m:
                kv.setdefault("trace_id", m.group(1))
        if kv:
            m = _TS_RE.match(raw)
            if m:
                kv.setdefault("__ts", m.group(1))
            kv["__raw"] = raw
            rows.append(kv)

    # A trace binds identifiers seen on one line to resources seen on another,
    # which is how readback traces link an email to an account id.
    trace_ids: Dict[str, set] = {}
    for kv in rows:
        trace = kv.get("trace_id")
        if not trace:
            continue
        subject = _subject_from(kv)
        ids = {v for v in subject.values() if v}
        if ids:
            trace_ids.setdefault(trace, set()).update(ids)

    for kv in rows:
        rid = kv.get("resource_id") or kv.get("resource")
        if not rid or not re.match(r"^[a-z_]+_\d+$", rid):
            stats.unparsed_lines += 1
            continue
        category, system = _initial_category(rid)
        if kv.get("record_type"):
            cat = kv["record_type"].strip('"').upper()
            if cat in {
                "USER_PROFILE", "SEARCH_INDEX_ENTRY", "ANALYTICS_PROFILE",
                "SUPPORT_MESSAGE", "SUPPORT_ATTACHMENT", "USER_EXPORT",
                "SECURITY_AUDIT_LOG", "DATA_FLOW", "BACKUP_ARCHIVE",
                "ACCOUNT_TOMBSTONE", "EMAIL_DELIVERY_LOG", "MARKETING_PROFILE",
                "PRODUCT_TELEMETRY", "CACHE_ENTRY", "LEGAL_HOLD_RECORD",
            }:
                category = cat
                kv["_cat_explicit"] = "1"
        svc = kv.get("service")
        if svc:
            system = _canonical_system(svc, category)
        state: Optional[str] = None
        event = (kv.get("event") or "").lower()
        if category == "SUPPORT_MESSAGE" and (
            event.startswith("contact_") or "legacy_contact_index" in kv.get("source", "")
        ):
            # A contact hit on a support replica's legacy index is a cached
            # copy of the contact, so it is deleted rather than redacted.
            category = "CACHE_ENTRY"
            kv["_cat_explicit"] = "1"
        if category == "ANALYTICS_PROFILE" and (
            event.startswith("contact_") or "email_opt_in" in kv
        ):
            # A contact lookup carrying an opt-in flag is a marketing contact
            # copy sitting on the analytics cluster, not an aggregated profile.
            category = "MARKETING_PROFILE"
            system = "analytics_platform"
            kv["_cat_explicit"] = "1"
        negative = any(pred(kv[k]) for k, pred in _NEGATIVE_FIELDS.items() if k in kv)
        explicit = _norm_state(kv.get("record_status") or kv.get("status") or kv.get("result") or "")
        if negative:
            state = "ABSENT"
        elif explicit:
            state = explicit
        elif event.endswith(_COMPLETED_SUFFIXES):
            state = "PRESENT"
        elif "matched_value" in kv and kv["matched_value"].strip('"').lower() not in {"", "null"}:
            # A PII scan that matched an identifier proves the data is exposed.
            state = "PRESENT"
        elif "deletion_block" in kv and kv["deletion_block"].strip('"').lower() == "true":
            state = "BLOCKED"
        elif "eligible" in kv:
            state = "PRESENT" if kv["eligible"].strip('"').lower() in {"true", "yes"} else "BLOCKED"
        elif "decision" in kv:
            state = "BLOCKED" if kv["decision"].strip('"').lower() == "deny" else "PRESENT"
        elif "marker_found" in kv:
            # A deletion marker on the row means the delete reached this replica;
            # no marker means the row survived or came back.
            state = "ABSENT" if kv["marker_found"].strip('"').lower() == "true" else "PRESENT"
        elif "enabled" in kv:
            state = "DISABLED" if kv["enabled"].strip('"').lower() == "false" else "ACTIVE"
        elif "restore_allowed" in kv:
            state = "BLOCKED" if kv["restore_allowed"].strip('"').lower() == "false" else "PRESENT"
        if state is None:
            stats.unparsed_lines += 1
            continue

        subject = _subject_from(kv)
        if not subject:
            ident = next(iter(trace_ids.get(kv.get("trace_id"), set())), "")
            if ident:
                subject = {"identifier": ident}
        pii = any(
            kv.get(f) and kv[f].strip('"').lower() not in {"null", "none", ""}
            for f in ("customer_email", "email", "requester_email", "phone", "email_hash",
                      "correlation_key", "matched_value", "lookup_key", "subject_key",
                      "match_key", "customer_reference", "subject_reference", "subject_ref")
        )
        attrs = {k: v for k, v in kv.items() if k not in {"service", "event"}}
        facts.append(Fact(
            evidence_id=evidence_id,
            captured_at=captured_at,
            system=system,
            resource_id=rid,
            category=category,
            state=state,
            subject=subject,
            pii=pii,
            attrs=attrs,
            kind="log",
            observed_at=kv.get("__ts") or captured_at,
        ))
    stats.facts += len(facts)
    return facts, stats


def _parse_json_line(raw: str) -> Dict[str, str]:
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(doc, dict):
        return {}
    out: Dict[str, str] = {}
    for key, value in doc.items():
        if isinstance(value, bool):
            out[str(key).lower()] = "true" if value else "false"
        elif value is None:
            out[str(key).lower()] = "null"
        elif isinstance(value, (str, int, float)):
            out[str(key).lower()] = str(value)
    return out


def _canonical_system(service: str, category: str) -> str:
    svc = service.strip('"').lower()
    mapping = {
        "account_service": "account_service",
        "search_service": "search_service",
        "analytics_platform": "analytics_platform",
        "support_platform": "support_platform",
        "support_replica": "support_platform",
        "export_service": "export_service",
        "security_service": "security_service",
        "integration_service": "integration_service",
        "backup_service": "backup_service",
        "object_storage": "object_storage",
        "object_store": "object_storage",
        "marketing_platform": "marketing_platform",
        "identity_service": "identity_service",
        "account_replication_cluster": "account_service",
        "account_failover_replica": "account_service",
        "cache_service": "cache_service",
        "email_delivery_service": "email_delivery_service",
        "readback_collector": "",
    }
    if svc in mapping and mapping[svc]:
        return mapping[svc]
    return CATEGORY_BY_PREFIX.get(category.split("_")[0].lower(), ("", ""))[1] or "unknown"


# ------------------------------------------------------------------ SQL facts

_INSERT_RE = re.compile(r'INSERT INTO "?([A-Za-z0-9_]+)"? VALUES\s*\((.*)\);\s*$', re.MULTILINE)


def parse_sql(text: str, evidence_id: str, captured_at: str) -> Tuple[List[Fact], List[Snapshot], ParseStats]:
    facts: List[Fact] = []
    snapshots: List[Snapshot] = []
    stats = ParseStats()
    topo = "primary"
    m = re.search(r"Database topology:\s*([a-z_]+)", text)
    if m:
        topo = m.group(1)

    columns: Dict[str, List[str]] = {}
    for m in re.finditer(r"CREATE TABLE \"?([A-Za-z0-9_]+)\"?\s*\((.*?)\);", text, re.S):
        table = m.group(1)
        cols: List[str] = []
        for raw in _split_top_level(m.group(2)):
            raw = raw.strip()
            if not raw or raw.upper().startswith(("PRIMARY KEY", "FOREIGN KEY", "UNIQUE", "CHECK")):
                continue
            cols.append(raw.split()[0].strip('"'))
        columns[table] = cols

    table_ids: Dict[Tuple[str, str], set] = {}
    for m in _INSERT_RE.finditer(text):
        table = m.group(1)
        if table not in SQL_TABLES and table != "snapshot_scope" and not table.startswith("customer_data_"):
            continue
        values = _split_sql_values(m.group(2))
        cols = columns.get(table)
        if not cols:
            continue
        row = dict(zip(cols, values))
        if table == "snapshot_scope" or not row.get("resource_id"):
            continue
        if table.startswith("customer_data_"):
            fact, snap = _envelope_fact(row, evidence_id, captured_at, topo, table)
            if fact is None:
                continue
            facts.append(fact)
            table_ids.setdefault((fact.system, fact.category, table), set()).add(fact.resource_id)
            continue
        category, system = SQL_TABLES[table]
        rid = row.get("resource_id")
        if not rid:
            continue
        subject = {k: row[k] for k in IDENTITY_FIELDS if row.get(k) and row[k] not in {"NULL", "None", ""}}
        pii = bool(subject.get("email") or subject.get("phone") or subject.get("email_hash")
                   or subject.get("full_name") or subject.get("requester_email")
                   or subject.get("owner_email"))
        facts.append(Fact(
            evidence_id=evidence_id,
            captured_at=captured_at,
            system=system,
            resource_id=rid,
            category=category,
            state="PRESENT",
            subject=subject,
            pii=pii,
            attrs={"table": table, "topology": topo, **row},
            kind="snapshot_row",
            observed_at=row.get("updated_at") or captured_at,
        ))
        table_ids.setdefault((system, category, table), set()).add(rid)

    for (system, category, table), ids in table_ids.items():
        snapshots.append(Snapshot(
            evidence_id=evidence_id,
            captured_at=captured_at,
            system=system,
            category=category,
            resource_ids=ids,
            topology=table,
        ))
    stats.facts += len(facts)
    stats.snapshots += len(snapshots)
    return facts, snapshots, stats


def _envelope_fact(row, evidence_id, captured_at, topo, table):
    """Decode the ``customer_data_*`` envelope (record_json + collection_name)."""
    try:
        record = json.loads(row.get("record_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        return None, None
    rid = row.get("resource_id") or record.get("resource_id")
    if not rid:
        return None, None
    coll = row.get("collection_name") or ""
    origin = (row.get("origin_system") or "").strip()
    category, system = JSON_COLLECTIONS.get(coll, _initial_category(rid))
    if origin:
        system = _canonical_system(origin, category)
    subject = {}
    record = _flatten_identifiers(record)
    for k in IDENTITY_FIELDS:
        v = record.get(k)
        if v not in (None, "", "null"):
            subject[k] = str(v)
    if "customer_reference" in subject and subject["customer_reference"].startswith("user_"):
        subject.setdefault("user_id", subject["customer_reference"])
    state = _json_state(record)
    pii = any(subject.get(k) for k in ("email", "phone", "email_hash", "name",
                                       "requester_email", "owner_email"))
    fact = Fact(
        evidence_id=evidence_id,
        captured_at=captured_at,
        system=system,
        resource_id=rid,
        category=category,
        state=state,
        subject=subject,
        pii=pii,
        attrs={"table": table, "topology": topo, "collection": coll, **record},
        kind="snapshot_row",
        observed_at=row.get("updated_at") or record.get("updated_at") or captured_at,
    )
    return fact, None


def _split_top_level(raw: str) -> List[str]:
    """Split on commas that are not inside parentheses or string literals."""
    out: List[str] = []
    cur: List[str] = []
    depth = 0
    in_str = False
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "'" and not in_str:
            in_str = True
        elif ch == "'" and in_str:
            if not (i + 1 < len(raw) and raw[i + 1] == "'"):
                in_str = False
        if ch == "(" and not in_str:
            depth += 1
        elif ch == ")" and not in_str:
            depth -= 1
        if ch == "," and depth == 0 and not in_str:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    if cur:
        out.append("".join(cur))
    return out


def _split_sql_values(raw: str) -> List[str]:
    out: List[str] = []
    cur = []
    in_str = False
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "'" and not in_str:
            in_str = True
            cur.append(ch)
        elif ch == "'" and in_str:
            if i + 1 < len(raw) and raw[i + 1] == "'":
                cur.append("''")
                i += 1
            else:
                in_str = False
                cur.append(ch)
        elif ch == "," and not in_str:
            out.append("".join(cur).strip().strip("'"))
            cur = []
        else:
            cur.append(ch)
        i += 1
    if cur:
        out.append("".join(cur).strip().strip("'"))
    return [None if v == "NULL" else v for v in out]


# ----------------------------------------------------------------- JSON facts


# A snapshot bundle stores one capture time per collection. That inner time is
# the authoritative one: the file lands later than the data it quotes. Honouring
# it is only safe for append-only retained records, whose availability is what
# the cutoff decides. For operable records a newer snapshot of the same store
# can *supersede* a replica row (replication drift), so those keep the file-level
# capture time until supersede semantics are implemented; see
# code/evaluation/evaluation_report.md section 5.3.
SNAPSHOT_SCOPED_CATEGORIES = ("SECURITY_AUDIT_LOG",)


def parse_json(text: str, evidence_id: str, captured_at: str) -> Tuple[List[Fact], ParseStats]:
    facts: List[Fact] = []
    stats = ParseStats()
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        stats.skipped_files.append(evidence_id)
        return facts, stats
    collection = doc.get("collection") if isinstance(doc, dict) else None
    captures = doc.get("captured_at") if isinstance(doc, dict) else None
    ts = captures or captured_at

    def walk(node: Any, inherited: Optional[str], scope_ts: str) -> None:
        if isinstance(node, dict):
            coll = node.get("collection") if isinstance(node.get("collection"), str) else inherited
            if "resource_id" in node:
                rid = str(node["resource_id"])
                category, system = _json_category(coll, rid, node)
                subject = {}
                merged = _flatten_identifiers(node)
                for k in IDENTITY_FIELDS:
                    v = merged.get(k)
                    if v not in (None, "", "null") and isinstance(v, (str, int)):
                        subject[k] = str(v)
                if "customer_reference" in subject and subject["customer_reference"].startswith("user_"):
                    subject.setdefault("user_id", subject["customer_reference"])
                state = _json_state(node)
                pii = any(subject.get(k) for k in ("email", "phone", "email_hash", "name",
                                                   "requester_email", "owner_email"))
                # Retained audit rows carry the capture time of their own
                # snapshot; every other record is bound to the file-level time.
                fact_ts = scope_ts if category in SNAPSHOT_SCOPED_CATEGORIES else ts
                facts.append(Fact(
                    evidence_id=evidence_id,
                    captured_at=fact_ts,
                    system=system,
                    resource_id=rid,
                    category=category,
                    state=state,
                    subject=subject,
                    pii=pii,
                    attrs={k: v for k, v in node.items() if k != "recent_activity"},
                    kind="json",
                    observed_at=str(node.get("updated_at") or node.get("observed_at") or fact_ts),
                ))
            inner = node.get("captured_at")
            next_scope = inner if isinstance(inner, str) else scope_ts
            for v in node.values():
                walk(v, coll, next_scope)
        elif isinstance(node, list):
            for v in node:
                walk(v, inherited, scope_ts)

    walk(doc, collection, ts)
    stats.facts += len(facts)
    return facts, stats


NESTED_RECORD_KEYS = ("profile", "document", "record", "owner", "subject", "contact",
                      "customer", "attributes")


def _flatten_identifiers(node: Dict[str, Any]) -> Dict[str, Any]:
    """Carry identifiers that live one level down inside a wrapper object.

    Search documents keep the person under ``document``, account lookups under
    ``profile``; ``JSON_COLLECTIONS`` names the collection but not the wrapper,
    so the wrapper keys are probed explicitly rather than guessed by position.
    """
    merged = dict(node)
    for key in NESTED_RECORD_KEYS:
        nested = node.get(key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                merged.setdefault(k, v)
    return merged


def _json_category(coll: Optional[str], rid: str, node: Dict[str, Any]) -> Tuple[str, str]:
    if coll and coll in JSON_COLLECTIONS:
        return JSON_COLLECTIONS[coll]
    if isinstance(node.get("record_type"), str):
        cat = node["record_type"].upper()
        if cat in {v[0] for v in CATEGORY_BY_PREFIX.values()} | {v[0] for v in SQL_TABLES.values()}:
            return cat, _initial_category(rid)[1]
    return _initial_category(rid)


def _json_state(node: Dict[str, Any]) -> str:
    if node.get("restore_allowed") is False:
        return "BLOCKED"
    if node.get("enabled") is False:
        return "DISABLED"
    for key in ("state", "status", "record_status", "account_status"):
        val = node.get(key)
        if isinstance(val, str):
            norm = _norm_state(val)
            if norm:
                return norm
    return "PRESENT"


# ------------------------------------------------------------- plain-text facts

_STATE_ROW_RE = re.compile(
    r"^\s*-\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s*\|\s*([a-z_]+)\s*\|\s*([a-z_]+_\d+)\s*\|"
    r"\s*([A-Z_]+)\s*\|\s*([A-Z_]+)\s*\|\s*(.*)$"
)


def parse_text(text: str, evidence_id: str, captured_at: str) -> Tuple[List[Fact], ParseStats]:
    facts: List[Fact] = []
    stats = ParseStats()
    for line in text.splitlines():
        m = _STATE_ROW_RE.match(line)
        if not m:
            continue
        _, system, rid, category, state_raw, subject_raw = m.groups()
        state = _norm_state(state_raw) or state_raw
        subject: Dict[str, str] = {}
        token = subject_raw.strip()
        if "@" in token:
            subject["email"] = token
        elif token.lower().startswith("user_"):
            subject["user_id"] = token
        pii = state == "PRESENT" and "@" in token
        if state == "PRESENT" and "@" not in token:
            pii = False
        facts.append(Fact(
            evidence_id=evidence_id,
            captured_at=captured_at,
            system=system,
            resource_id=rid,
            category=category,
            state=state,
            subject=subject,
            pii=pii,
            attrs={"raw": line.strip()},
            kind="state_row",
            observed_at=m.group(1),
        ))
    stats.facts += len(facts)
    return facts, stats


# ------------------------------------------------------------------- loading


def load_manifest(dataset_dir: str) -> List[Dict[str, str]]:
    with open(os.path.join(dataset_dir, "evidence_files.csv"), newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_evidence(dataset_dir: str, fixtures_dir: Optional[str] = None):
    """Parse every manifest entry into facts and table snapshots."""
    facts: List[Fact] = []
    snapshots: List[Snapshot] = []
    stats = ParseStats()
    fixtures = {}
    if fixtures_dir:
        path = os.path.join(fixtures_dir, "binary_facts.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                fixtures = json.load(fh)

    for entry in load_manifest(dataset_dir):
        path = os.path.join(dataset_dir, entry["file_path"])
        stats.files += 1
        rel = entry["file_path"]
        ext = os.path.splitext(rel)[1].lower()
        try:
            if ext in {".png", ".pdf"}:
                for raw in fixtures.get(entry["file_id"], []):
                    facts.append(Fact(
                        evidence_id=entry["file_id"],
                        captured_at=entry["captured_at"],
                        system=raw.get("system", "unknown"),
                        resource_id=raw["resource_id"],
                        category=raw.get("category", _initial_category(raw["resource_id"])[0]),
                        state=raw.get("state", "PRESENT"),
                        subject=raw.get("subject", {}),
                        pii=bool(raw.get("pii", False)),
                        attrs=raw.get("attrs", {}),
                        kind="binary",
                        observed_at=entry["captured_at"],
                    ))
                continue
            with open(path, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
            if ext == ".sql":
                f, s, st = parse_sql(text, entry["file_id"], entry["captured_at"])
                facts.extend(f)
                snapshots.extend(s)
                stats.facts += st.facts
                stats.snapshots += st.snapshots
            elif ext == ".json":
                f, st = parse_json(text, entry["file_id"], entry["captured_at"])
                facts.extend(f)
                stats.facts += st.facts
            elif ext == ".log":
                f, st = parse_log(text, entry["file_id"], entry["captured_at"])
                facts.extend(f)
                stats.facts += st.facts
                stats.unparsed_lines += st.unparsed_lines
            elif ext in {".txt", ".md"}:
                f, st = parse_text(text, entry["file_id"], entry["captured_at"])
                facts.extend(f)
                stats.facts += st.facts
            else:
                stats.skipped_files.append(rel)
        except OSError:
            stats.skipped_files.append(rel)
    return facts, snapshots, stats

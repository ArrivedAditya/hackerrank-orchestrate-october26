"""Deletion policy: map a record's category and condition to a required action.

Mirrors ``dataset/deletion_rules.csv``. The rule file is read at run time so the
engine follows the supplied policy rather than a hardcoded copy, and the table
below only supplies the mapping from rule rows to planner behaviour.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

# Planner behaviour per rule id.
RULE_BEHAVIOUR: Dict[str, str] = {
    "rule_01": "DELETE",          # USER_PROFILE
    "rule_02": "DELETE",          # SECONDARY_IDENTIFIER
    "rule_03": "DELETE",          # SEARCH_INDEX_ENTRY
    "rule_04": "DELETE",          # CACHE_ENTRY
    "rule_05": "REDACT",          # SUPPORT_MESSAGE with shared context
    "rule_06": "DELETE",          # SUPPORT_ATTACHMENT owned only by subject
    "rule_07": "ANONYMIZE",       # ANALYTICS_PROFILE
    "rule_08": "ANONYMIZE",       # PRODUCT_TELEMETRY
    "rule_09": "DELETE",          # USER_EXPORT
    "rule_10": "DELETE",          # MARKETING_PROFILE
    "rule_11": "REDACT",          # EMAIL_DELIVERY_LOG with an address
    "rule_12": "RETAIN",          # SECURITY_AUDIT_LOG within retention
    "rule_13": "RETAIN",          # LEGAL_HOLD_RECORD
    "rule_14": "RETAIN_BLOCKED",  # BACKUP_ARCHIVE
    "rule_15": "TOMBSTONE",       # ACCOUNT_TOMBSTONE
}

# Categories that the rule file does not name directly but the corpus uses.
EXTRA_CATEGORY_RULES: Dict[str, str] = {
    "DATA_FLOW": "DATA_FLOW",
    "CACHE_ENTRY": "DELETE",
    "PRODUCT_TELEMETRY": "ANONYMIZE",
    "SECONDARY_IDENTIFIER": "DELETE",
    "UNKNOWN": "DELETE",
}


@dataclass
class Rules:
    by_category: Dict[str, Dict[str, str]]
    behaviour: Dict[str, str]

    def for_category(self, category: str) -> str:
        entry = self.by_category.get(category)
        if entry:
            return entry["behaviour"]
        return EXTRA_CATEGORY_RULES.get(category, "DELETE")


def load_rules(dataset_dir: str) -> Rules:
    path = os.path.join(dataset_dir, "deletion_rules.csv")
    by_category: Dict[str, Dict[str, str]] = {}
    behaviour: Dict[str, str] = dict(RULE_BEHAVIOUR)
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                rule_id = row["rule_id"]
                action = behaviour.get(rule_id, "DELETE")
                by_category[row["data_category"].upper()] = {
                    "rule_id": rule_id,
                    "action": row["required_action"].upper(),
                    "condition": row.get("condition", ""),
                    "behaviour": action,
                }
    return Rules(by_category=by_category, behaviour=behaviour)


def support_action(record: Dict[str, str]) -> str:
    """rule_05 vs rule_06 is decided by the data category, not by wording.

    A SUPPORT_MESSAGE always carries conversation context that the business
    keeps, so the identifying fields are redacted and the row survives. A
    SUPPORT_ATTACHMENT belongs only to the requesting customer, so it is
    deleted with the rest of the personal data.
    """
    if (record.get("_category") or "").upper() == "SUPPORT_ATTACHMENT":
        return "DELETE"
    return "REDACT"


def expected_pii(category: str) -> bool:
    """True when the category is expected to carry direct identifiers."""
    return category in {
        "USER_PROFILE", "SEARCH_INDEX_ENTRY", "ANALYTICS_PROFILE",
        "PRODUCT_TELEMETRY", "SUPPORT_MESSAGE", "SUPPORT_ATTACHMENT",
        "USER_EXPORT", "MARKETING_PROFILE", "EMAIL_DELIVERY_LOG",
        "ACCOUNT_TOMBSTONE", "CACHE_ENTRY", "SECONDARY_IDENTIFIER",
    }


def verification_state(action: str) -> Optional[str]:
    return {
        "DELETE": "ABSENT",
        "REDACT": "REDACTED",
        "TOMBSTONE": "REDACTED",
        "ANONYMIZE": "ANONYMIZED",
        "RETAIN": "PRESENT",
        "RETAIN_BLOCKED": "BLOCKED",
        "DATA_FLOW": "DISABLED",
    }.get(action)


VERIFY_OPERATION = {
    "ABSENT": ("VERIFY_RECORD_ABSENT", "ABSENT"),
    "PRESENT": ("VERIFY_RECORD_PRESENT", "PRESENT"),
    "REDACTED": ("VERIFY_RECORD_REDACTED", "REDACTED"),
    "ANONYMIZED": ("VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"),
    "DISABLED": ("VERIFY_DATA_FLOW_DISABLED", "DISABLED"),
    "BLOCKED": ("VERIFY_RESTORE_BLOCKED", "BLOCKED"),
}

REMEDIATION_OPERATION = {
    "DELETE": ("DELETE_RECORD", "ABSENT"),
    "REDACT": ("REDACT_RECORD", "REDACTED"),
    "TOMBSTONE": ("REDACT_RECORD", "REDACTED"),
    "ANONYMIZE": ("ANONYMIZE_RECORD", "ANONYMIZED"),
    "DATA_FLOW": ("DISABLE_DATA_FLOW", "DISABLED"),
    "RETAIN_BLOCKED": ("BLOCK_RESTORE", "BLOCKED"),
}

# Rank inside a phase, taken from the ordered operation table in the spec.
OPERATION_ORDER: List[str] = [
    "REPORT_REMAINING_RECORD",
    "REPORT_EXPOSED_PII",
    "REPORT_ACTIVE_DATA_FLOW",
    "REPORT_RESTORE_RISK",
    "DISABLE_DATA_FLOW",
    "BLOCK_RESTORE",
    "DELETE_RECORD",
    "REDACT_RECORD",
    "ANONYMIZE_RECORD",
    "VERIFY_RECORD_ABSENT",
    "VERIFY_RECORD_PRESENT",
    "VERIFY_RECORD_REDACTED",
    "VERIFY_RECORD_ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED",
    "VERIFY_RESTORE_BLOCKED",
]

PHASE_ORDER = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}

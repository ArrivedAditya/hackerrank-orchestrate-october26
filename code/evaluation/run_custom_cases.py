"""Participant-created evaluation cases.

The 32 public samples cover ordinary deletion and non-deletion requests but they
contain no ``NOT_APPLICABLE`` row and no case that mixes a live data flow with a
restorable backup. These four cases were written from the specification to cover
the gaps, using a private mini-dataset built at run time so that the engine is
exercised on inputs the samples never provide.

Run with:

    python3 code/evaluation/run_custom_cases.py

The script builds a temporary dataset, audits it with the real engine, compares
the result with the expectation written next to each case, and exits non-zero on
any mismatch.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import tempfile
from typing import Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
CODE_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(CODE_DIR)
sys.path.insert(0, CODE_DIR)

import plan  # noqa: E402


REQUESTS: List[Dict[str, str]] = [
    {
        "case_id": "custom_01",
        "email": "amelie.rousseau@example.com",
        "requested_at": "2026-09-02T08:00:00Z",
        "reported_status": "NOT_STARTED",
        "reported_at": "2026-09-02T09:00:00Z",
        "audit_at": "2026-09-03T08:00:00Z",
        "request_text": (
            "Je souhaite consulter une copie des données que vous conservez sur moi. "
            "Merci de ne rien supprimer, mon compte doit rester actif."
        ),
    },
    {
        "case_id": "custom_02",
        "email": "bruno.costa@example.com",
        "requested_at": "2026-09-04T10:00:00Z",
        "reported_status": "NOT_STARTED",
        "reported_at": "2026-09-04T11:00:00Z",
        "audit_at": "2026-09-05T10:00:00Z",
        "request_text": (
            "Parem de me enviar e-mails promocionais. Quero continuar usando a conta "
            "e manter meu histórico de pedidos, não peço exclusão."
        ),
    },
    {
        "case_id": "custom_03",
        "email": "dana.okafor@example.com",
        "requested_at": "2026-09-10T09:00:00Z",
        "reported_status": "PARTIALLY_COMPLETE",
        "reported_at": "2026-09-10T10:00:00Z",
        "audit_at": "2026-09-12T09:00:00Z",
        "request_text": (
            "Please permanently delete my account and everything linked to it. "
            "Confirm what is left when you are done."
        ),
    },
    {
        "case_id": "custom_04",
        "email": "elif.demir@example.com",
        "requested_at": "2026-09-20T09:00:00Z",
        "reported_status": "COMPLETE",
        "reported_at": "2026-09-20T10:00:00Z",
        "audit_at": "2026-09-22T09:00:00Z",
        "request_text": "Bitte löschen Sie mein Konto dauerhaft und alle zugehörigen Daten.",
    },
]

IDENTITIES = [
    "user_c1,Amelie Rousseau,amelie.rousseau@example.com,+33 6 11 22 33 44,"
    "1111111111111111111111111111111111111111111111111111111111111111,2026-01-02T00:00:00Z",
    "user_c2,Bruno Costa,bruno.costa@example.com,+55 11 95555 1234,"
    "2222222222222222222222222222222222222222222222222222222222222222,2026-01-02T00:00:00Z",
    "user_c3,Dana Okafor,dana.okafor@example.com,+44 7700 900123,"
    "3333333333333333333333333333333333333333333333333333333333333333,2026-01-02T00:00:00Z",
    "user_c4,Elif Demir,elif.demir@example.com,+90 532 123 45 67,"
    "4444444444444444444444444444444444444444444444444444444444444444,2026-01-02T00:00:00Z",
]

# One evidence file per audited case keeps the fixture readable; the engine only
# sees the usual manifest plus JSON artifacts.
EVIDENCE = {
    "evidence_c03": {
        "captured_at": "2026-09-11T12:00:00Z",
        "service_exports": {
            "account_service": {
                "profile_lookups": [
                    {
                        "observed_at": "2026-09-11T09:12:00Z",
                        "resource_id": "account_9001",
                        "status_code": 200,
                        "profile": {
                            "customer_reference": "user_c3",
                            "email": "dana.okafor@example.com",
                            "full_name": "Dana Okafor",
                            "phone": "+44 7700 900123",
                            "email_hash": None,
                        },
                    }
                ]
            },
            "search_service": {
                "index_entries": [
                    {
                        "observed_at": "2026-09-11T09:14:00Z",
                        "resource_id": "search_9002",
                        "email": "dana.okafor@example.com",
                        "customer_reference": "user_c3",
                    }
                ]
            },
            "integration_service": {
                "connectors": [
                    {
                        "observed_at": "2026-09-11T09:20:00Z",
                        "resource_id": "data_flow_9003",
                        "customer_email": "dana.okafor@example.com",
                        "customer_reference": "user_c3",
                        "enabled": True,
                        "last_delivery_status": "delivered",
                    }
                ]
            },
            "backup_service": {
                "restore_entries": [
                    {
                        "observed_at": "2026-09-11T09:30:00Z",
                        "resource_id": "backup_9004",
                        "customer_reference": "user_c3",
                        "restore_allowed": True,
                    }
                ]
            },
            "security_service": {
                "audit_events": [
                    {
                        "observed_at": "2026-09-10T09:05:00Z",
                        "resource_id": "audit_record_9005",
                        "subject_reference": "user_c3",
                        "retention_until": "2033-09-10T00:00:00Z",
                    }
                ]
            },
        },
    },
    "evidence_c04": {
        "captured_at": "2026-09-21T12:00:00Z",
        "service_exports": {
            "identity_service": {
                "deleted_customer_references": [
                    {
                        "observed_at": "2026-09-21T09:13:00Z",
                        "resource_id": "tombstone_9102",
                        "subject_reference": "user_c4",
                        "name": None,
                        "email": None,
                        "phone": None,
                        "email_hash": None,
                        "account_status": "DELETED",
                    }
                ]
            },
            "security_service": {
                "audit_events": [
                    {
                        "observed_at": "2026-09-20T09:05:00Z",
                        "resource_id": "audit_record_9103",
                        "subject_reference": "user_c4",
                        "retention_until": "2033-09-20T00:00:00Z",
                    }
                ]
            },
            "analytics_platform": {
                "profiles": [
                    {
                        "observed_at": "2026-09-21T09:15:00Z",
                        "resource_id": "analytics_9104",
                        "customer_reference": "user_c4",
                        "email": None,
                        "email_hash": None,
                        "phone": None,
                    }
                ]
            },
        },
    },
}

# A service log carries the account lookup that returned no row. The readback
# traces in the corpus use the same shape, and `rows=0` is what proves absence.
LOG_EVIDENCE = {
    "evidence_c05": (
        "2026-09-21T12:00:00Z",
        "2026-09-21T09:12:00Z level=INFO service=account_service instance=account-1 "
        "pod=account-7d21 region=eu-west-1 trace_id=tr-c04 resource_id=account_9101 "
        "event=profile_lookup_completed rows=0 customer_reference=user_c4\n",
    ),
}

EXPECTED = {
    # A request for a copy of the data does not authorise deletion.
    "custom_01": {"status": "NOT_APPLICABLE", "steps": []},
    # A marketing opt-out keeps the account and its history.
    "custom_02": {"status": "NOT_APPLICABLE", "steps": []},
    # Live connector plus a restorable archive must be stopped before the copies
    # are deleted; retained audit rows stay.
    "custom_03": {
        "status": "INCOMPLETE",
        "steps": [
            ("FINDING", "REPORT_REMAINING_RECORD", "account_service", "account_9001"),
            ("FINDING", "REPORT_REMAINING_RECORD", "search_service", "search_9002"),
            ("FINDING", "REPORT_ACTIVE_DATA_FLOW", "integration_service", "data_flow_9003"),
            ("FINDING", "REPORT_RESTORE_RISK", "backup_service", "backup_9004"),
            ("REMEDIATION", "DISABLE_DATA_FLOW", "integration_service", "data_flow_9003"),
            ("REMEDIATION", "BLOCK_RESTORE", "backup_service", "backup_9004"),
            ("REMEDIATION", "DELETE_RECORD", "account_service", "account_9001"),
            ("REMEDIATION", "DELETE_RECORD", "search_service", "search_9002"),
            ("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service", "account_9001"),
            ("VERIFICATION", "VERIFY_RECORD_ABSENT", "search_service", "search_9002"),
            ("VERIFICATION", "VERIFY_RECORD_PRESENT", "security_service", "audit_record_9005"),
            ("VERIFICATION", "VERIFY_DATA_FLOW_DISABLED", "integration_service", "data_flow_9003"),
            ("VERIFICATION", "VERIFY_RESTORE_BLOCKED", "backup_service", "backup_9004"),
        ],
    },
    # Everything already reached its required state, so only proof steps remain.
    "custom_04": {
        "status": "COMPLETE",
        "steps": [
            ("VERIFICATION", "VERIFY_RECORD_ABSENT", "account_service", "account_9101"),
            ("VERIFICATION", "VERIFY_RECORD_PRESENT", "security_service", "audit_record_9103"),
            ("VERIFICATION", "VERIFY_RECORD_REDACTED", "identity_service", "tombstone_9102"),
            ("VERIFICATION", "VERIFY_RECORD_ANONYMIZED", "analytics_platform", "analytics_9104"),
        ],
    },
}


def build_dataset(root: str) -> None:
    os.makedirs(os.path.join(root, "evidence", "records"), exist_ok=True)

    with open(os.path.join(root, "deletion_requests.csv"), "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(REQUESTS[0].keys()), lineterminator="\n")
        writer.writeheader()
        writer.writerows(REQUESTS)

    with open(os.path.join(root, "user_identities.csv"), "w", encoding="utf-8", newline="") as fh:
        fh.write("user_id,name,email,phone,email_hash,updated_at\n")
        for line in IDENTITIES:
            fh.write(line + "\n")

    with open(os.path.join(root, "user_audit_log.csv"), "w", encoding="utf-8", newline="") as fh:
        fh.write("audit_id,user_id,changed_at,field_name,old_value,new_value\n")

    with open(os.path.join(root, "system_activity.csv"), "w", encoding="utf-8", newline="") as fh:
        fh.write("activity_id,event_time,event_type,source_system,target_system,"
                 "resource_id,reported_result,details\n")

    shutil.copyfile(os.path.join(REPO_ROOT, "dataset", "deletion_rules.csv"),
                    os.path.join(root, "deletion_rules.csv"))

    manifest = ["file_id,file_path,origin,captured_at"]
    for file_id, payload in EVIDENCE.items():
        rel = f"evidence/records/{file_id}.json"
        with open(os.path.join(root, rel), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        origin = next(iter(payload["service_exports"]))
        manifest.append(f"{file_id},{rel},{origin},{payload['captured_at']}")
    for file_id, (captured_at, body) in LOG_EVIDENCE.items():
        rel = f"evidence/logs/{file_id}.log"
        os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
        with open(os.path.join(root, rel), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        manifest.append(f"{file_id},{rel},account_service,{captured_at}")
    with open(os.path.join(root, "evidence_files.csv"), "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(manifest) + "\n")


def summarise(steps: List[Dict[str, object]]) -> List[tuple]:
    return [(s["phase"], s["operation"], s["system_id"], s["resource_id"]) for s in steps]


def check_invariants(case_id: str, status: str, steps: List[Dict[str, object]]) -> List[str]:
    problems: List[str] = []
    if [s["step_no"] for s in steps] != list(range(1, len(steps) + 1)):
        problems.append(f"{case_id}: step numbers are not consecutive from 1")
    order = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}
    phases = [order[s["phase"]] for s in steps]
    if phases != sorted(phases):
        problems.append(f"{case_id}: phases are out of order")
    for step in steps:
        if status == "INCOMPLETE" and step["phase"] == "VERIFICATION" and step["evidence_id"]:
            problems.append(f"{case_id}: incomplete verification cites evidence")
        if status == "COMPLETE" and not step["evidence_id"]:
            problems.append(f"{case_id}: complete verification has no evidence")
    return problems


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="orchestrate-custom-")
    try:
        build_dataset(tmp)
        auditor = plan.CaseAuditor(tmp)
        failures: List[str] = []
        print(f"{'case':10} {'expected':16} {'actual':16} result")
        for request in REQUESTS:
            case_id = request["case_id"]
            status, steps = auditor.audit(request)
            want = EXPECTED[case_id]
            actual = summarise(steps)
            ok = status == want["status"] and actual == want["steps"]
            print(f"{case_id:10} {want['status']:16} {status:16} {'PASS' if ok else 'FAIL'}")
            if not ok:
                if status != want["status"]:
                    failures.append(f"{case_id}: status {status} != {want['status']}")
                for step in want["steps"]:
                    if step not in actual:
                        failures.append(f"{case_id}: missing {step}")
                for step in actual:
                    if step not in want["steps"]:
                        failures.append(f"{case_id}: unexpected {step}")
            failures.extend(check_invariants(case_id, status, steps))
        if failures:
            print("\nfailures:")
            for item in failures:
                print(" -", item)
            return 1
        print("\nall custom cases passed")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

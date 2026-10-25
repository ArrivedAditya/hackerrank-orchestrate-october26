"""Compare generated answers against ``dataset/sample_output.csv``.

Reports the metrics the evaluation report needs: status accuracy, step-level
precision/recall, field accuracy and sequence accuracy, plus a per-case diff of
the first mismatches so regressions are actionable.
"""

from __future__ import annotations

import csv
import json
import os
from typing import Dict, List, Tuple


def load_csv(path: str) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def compare(expected: str, actual: str) -> Dict[str, object]:
    exp = load_csv(expected)
    act = {row["case_id"]: row for row in load_csv(actual)}

    status_ok = 0
    step_tp = step_fp = step_fn = 0
    field_ok = field_total = 0
    seq_ok = 0
    seq_total = 0
    diffs: List[str] = []

    for row in exp:
        cid = row["case_id"]
        got = act.get(cid)
        if got is None:
            diffs.append(f"{cid}: missing row")
            continue
        if got["deletion_status"] == row["deletion_status"]:
            status_ok += 1
        else:
            diffs.append(f"{cid}: status {got['deletion_status']} != {row['deletion_status']}")
        want = json.loads(row["steps_json"])
        have = json.loads(got["steps_json"])
        want_keys = [_step_key(s) for s in want]
        have_keys = [_step_key(s) for s in have]
        matched = 0
        for key in have_keys:
            if key in want_keys:
                matched += 1
            else:
                step_fp += 1
        step_tp += matched
        step_fn += max(0, len(want_keys) - matched)
        for i, (w, h) in enumerate(zip(want, have)):
            for field in ("phase", "system_id", "resource_id", "operation", "state"):
                field_total += 1
                if w[field] == h[field]:
                    field_ok += 1
            field_total += 1
            if w["evidence_id"] == h["evidence_id"]:
                field_ok += 1
        seq_total += 1
        if [s["operation"] for s in want] == [s["operation"] for s in have]:
            seq_ok += 1
        if len(diffs) < 12 and (_status(want, row) or len(want) != len(have)):
            diffs.append(f"{cid}: {len(want)} vs {len(have)} steps | "
                         f"want {[s['operation'] + ':' + s['resource_id'] for s in want]} | "
                         f"got  {[s['operation'] + ':' + s['resource_id'] for s in have]}")

    n = len(exp)
    precision = step_tp / (step_tp + step_fp) if step_tp + step_fp else 0.0
    recall = step_tp / (step_tp + step_fn) if step_tp + step_fn else 0.0
    return {
        "cases": n,
        "status_accuracy": round(status_ok / n, 4) if n else 0.0,
        "step_precision": round(precision, 4),
        "step_recall": round(recall, 4),
        "field_accuracy": round(field_ok / field_total, 4) if field_total else 0.0,
        "sequence_accuracy": round(seq_ok / seq_total, 4) if seq_total else 0.0,
        "diffs": diffs,
    }


def _status(steps, row) -> bool:
    return row["deletion_status"] == "INCOMPLETE" and bool(steps)


def _step_key(step: Dict[str, object]) -> Tuple:
    return (step["phase"], step["operation"], step["system_id"], step["resource_id"])


def main() -> None:
    import sys

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sample_out = os.path.join(root, "dataset", "sample_output.csv")
    actual = sys.argv[1] if len(sys.argv) > 1 else os.path.join(root, "sample_predictions.csv")
    result = compare(sample_out, actual)
    for key in ("cases", "status_accuracy", "step_precision", "step_recall",
                "field_accuracy", "sequence_accuracy"):
        print(f"{key}: {result[key]}")
    if result["diffs"]:
        print("\ndiffs:")
        for line in result["diffs"]:
            print(" -", line)


if __name__ == "__main__":
    main()

"""Score a predictions file against a reference answers file.

Usage:
    python3 code/evaluation/metrics.py dataset/sample_output.csv code/sample_predictions.csv

Metrics are reported at four levels, matching what the challenge asks for:

* case level   - deletion_status accuracy
* step level   - precision / recall / F1 over (phase, operation, system, resource)
* field level  - accuracy of each individual field on aligned steps
* sequence     - share of cases whose complete ordered step list matches
"""

from __future__ import annotations

import csv
import json
import sys
from typing import Dict, List, Tuple

FIELDS = ["step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"]
IDENTITY = ("phase", "operation", "system_id", "resource_id")



def load(path: str) -> Dict[str, Dict[str, object]]:
    out: Dict[str, Dict[str, object]] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out[row["case_id"]] = {
                "status": row["deletion_status"],
                "steps": json.loads(row["steps_json"]),
            }
    return out


def score(reference: Dict[str, Dict[str, object]], predicted: Dict[str, Dict[str, object]]) -> Dict[str, float]:
    case_ids = sorted(set(reference) & set(predicted))
    status_ok = sum(1 for c in case_ids if reference[c]["status"] == predicted[c]["status"])

    tp = fp = fn = 0
    field_ok = field_total = 0
    sequence_ok = 0
    per_field = {name: [0, 0] for name in FIELDS}
    for case_id in case_ids:
        want: List[Dict[str, object]] = reference[case_id]["steps"]  # type: ignore[assignment]
        got: List[Dict[str, object]] = predicted[case_id]["steps"]  # type: ignore[assignment]
        want_keys = [tuple(s[k] for k in IDENTITY) for s in want]
        got_keys = [tuple(s[k] for k in IDENTITY) for s in got]
        tp += sum(1 for key in got_keys if key in want_keys)
        fp += sum(1 for key in got_keys if key not in want_keys)
        fn += sum(1 for key in want_keys if key not in got_keys)
        if want_keys == got_keys:
            sequence_ok += 1

        # Field-level scoring walks both lists in the required order, the way a
        # grader comparing array positions does. A missing step therefore costs
        # every field it would have contributed.
        for index in range(max(len(want), len(got))):
            want_step = want[index] if index < len(want) else None
            got_step = got[index] if index < len(got) else None
            for name in FIELDS:
                per_field[name][1] += 1
                field_total += 1
                if want_step is not None and got_step is not None and want_step.get(name) == got_step.get(name):
                    per_field[name][0] += 1
                    field_ok += 1

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "cases": len(case_ids),
        "status_accuracy": status_ok / len(case_ids) if case_ids else 0.0,
        "step_precision": precision,
        "step_recall": recall,
        "step_f1": f1,
        "field_accuracy": field_ok / field_total if field_total else 0.0,
        "sequence_accuracy": sequence_ok / len(case_ids) if case_ids else 0.0,
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "field_scores": {k: (v[0] / v[1] if v[1] else 0.0) for k, v in per_field.items()},
    }


def main(argv: List[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    reference = load(argv[1])
    predicted = load(argv[2])
    result = score(reference, predicted)
    print(f"cases: {result['cases']}")
    for key in ("status_accuracy", "step_precision", "step_recall", "step_f1",
                "field_accuracy", "sequence_accuracy"):
        print(f"{key}: {result[key]:.4f}")
    print(f"steps: {result['true_positives']} matched, {result['false_positives']} extra, "
          f"{result['false_negatives']} missing")
    print("field breakdown:")
    for name, value in result["field_scores"].items():
        print(f"  {name:12} {value:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

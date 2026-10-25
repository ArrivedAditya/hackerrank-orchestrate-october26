"""Entry point: audit every deletion request and write ``output.csv``.

Usage:
    python3 code/main.py                       # full dataset -> output.csv at repo root
    python3 code/main.py --sample              # 32 public samples -> code/sample_predictions.csv
    python3 code/main.py --input X --output Y  # explicit paths
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plan  # noqa: E402

HEADER = ["case_id", "deletion_status", "steps_json"]


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def case_sort_key(case_id: str):
    """Numeric-aware ordering so ``case_99`` sorts before ``case_100``."""
    match = re.fullmatch(r"([A-Za-z_]+)(\d+)", case_id)
    if match:
        return (match.group(1), int(match.group(2)), case_id)
    return (case_id, 0, case_id)


def write_rows(path: str, rows: List[Dict[str, object]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(HEADER)
        for row in rows:
            writer.writerow([row["case_id"], row["deletion_status"],
                             json.dumps(row["steps"], separators=(",", ":"))])


def main(argv: List[str] | None = None) -> int:
    root = repo_root()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=os.path.join(root, "dataset"))
    parser.add_argument("--input", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--sample", action="store_true")
    args = parser.parse_args(argv)

    dataset_dir = args.dataset
    source = args.input or os.path.join(
        dataset_dir, "sample_deletion_requests.csv" if args.sample else "deletion_requests.csv"
    )
    target = args.output or (
        os.path.join(root, "code", "sample_predictions.csv") if args.sample
        else os.path.join(root, "output.csv")
    )

    with open(source, newline="", encoding="utf-8") as fh:
        requests = list(csv.DictReader(fh))

    auditor = plan.CaseAuditor(dataset_dir)
    results: List[Dict[str, object]] = []
    for row in requests:
        status, steps = auditor.audit(row)
        results.append({"case_id": row["case_id"], "deletion_status": status, "steps": steps})
    results.sort(key=lambda r: case_sort_key(r["case_id"]))

    write_rows(target, results)
    print(f"wrote {len(results)} rows to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

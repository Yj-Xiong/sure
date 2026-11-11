#!/usr/bin/env python3
"""Create a rating-level sensitivity table without changing raw scores."""

import argparse
import csv
import json
import statistics
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    rows = list(csv.DictReader(args.input.open(newline="", encoding="utf-8-sig")))
    groups = {}
    for row in rows:
        key = (row.get("sample_id", ""), row.get("candidate_key", ""))
        try:
            groups.setdefault(key, []).append((row, float(row["score"])))
        except (KeyError, ValueError):
            continue
    flagged = []
    for key, values in groups.items():
        if len(values) < 8:
            continue
        scores = [score for _, score in values]
        ordered = sorted(scores)
        q1 = ordered[len(ordered) // 4]
        q3 = ordered[(3 * len(ordered)) // 4]
        iqr = q3 - q1
        median = statistics.median(scores)
        for row, score in values:
            outside = score < q1 - 3 * iqr or score > q3 + 3 * iqr
            if outside and abs(score - median) >= 20:
                flagged.append({"sample_id": key[0], "candidate_key": key[1], "listener_id": row.get("listener_id", row.get("user_id", "")), "score": score, "reason": "3×IQR and 20-point median deviation"})
    args.report.write_text(json.dumps({"schema": "sure.audio_eval.screening.v1", "rule": "rating-level 3×IQR + n≥8 + median deviation≥20; objective-independent", "flagged_count": len(flagged), "flagged": flagged, "raw_scores_unchanged": True}, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

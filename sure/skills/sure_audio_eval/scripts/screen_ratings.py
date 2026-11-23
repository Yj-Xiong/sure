#!/usr/bin/env python3
"""Create a rating-level sensitivity table without changing raw scores."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path

SCREENING_RULE_VERSION = "rating-level-3iqr-median-deviation-v1"


def normalize_listener(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def selected_models(path: Path) -> set[str]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    selected = manifest.get("analysis_candidate_keys")
    if not isinstance(selected, list) or not selected:
        raise SystemExit("task manifest must declare analysis_candidate_keys")
    candidates = {str(value) for value in manifest.get("candidate_keys", [])}
    models = {str(value) for value in selected}
    if not models.issubset(candidates):
        raise SystemExit("analysis_candidate_keys must be a subset of candidate_keys")
    roles = manifest.get("candidate_roles") if isinstance(manifest.get("candidate_roles"), dict) else {}
    allowed = {"model", "method", "enhancement", "enhanced_model", "system"}
    if any(str(roles.get(model, "")).casefold() not in allowed for model in models):
        raise SystemExit("analysis_candidate_keys must contain only model candidate roles")
    return models


def minimum_ratings(manifest: dict) -> int:
    return 8


def quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    index = probability * (len(ordered) - 1)
    low, high = math.floor(index), math.ceil(index)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    models = selected_models(args.manifest)
    min_count = minimum_ratings(manifest)
    rows = list(csv.DictReader(args.input.open(newline="", encoding="utf-8-sig")))
    groups = {}
    for row in rows:
        if str(row.get("group_complete", "true")).casefold() == "false":
            continue
        key = (row.get("sample_id", ""), row.get("candidate_key", ""))
        if key[1] not in models:
            continue
        try:
            groups.setdefault(key, []).append((row, float(row["score"])))
        except (KeyError, ValueError):
            continue
    flagged = []
    for key, values in groups.items():
        if len(values) < min_count:
            continue
        scores = [score for _, score in values]
        q1 = quantile(scores, 0.25)
        q3 = quantile(scores, 0.75)
        iqr = q3 - q1
        if iqr <= 0:
            continue
        median = statistics.median(scores)
        for row, score in values:
            outside = score < q1 - 3 * iqr or score > q3 + 3 * iqr
            if outside and abs(score - median) >= 20:
                flagged.append({"sample_id": key[0], "candidate_key": key[1], "listener_id": normalize_listener(row.get("listener_id", row.get("user_id", ""))), "score": score, "reason": "3×IQR and 20-point median deviation"})
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({"schema": "sure.audio_eval.screening.v1", "rule_version": SCREENING_RULE_VERSION, "minimum_ratings": min_count, "rule": "rating-level 3×IQR + n≥8 + median deviation≥20; objective-independent", "objective_independent": True, "flagged_count": len(flagged), "flagged": flagged, "raw_scores_unchanged": True}, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

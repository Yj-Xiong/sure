#!/usr/bin/env python3
"""Aggregate validated subjective rows into MOS, sample SD and t-CI95.

Only complete listener × sample groups contribute.  The original deduplicated
CSV remains the audit source; this stage writes new condition tables.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from functools import lru_cache
from pathlib import Path
from typing import Any

# Kept as a public compatibility table for callers that used the original
# importer.  t_critical_95 below evaluates arbitrary degrees of freedom.
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447,
       7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179,
       13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101,
       19: 2.093, 20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064,
       25: 2.060, 26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042}


def load_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def t_pdf(x: float, df: int) -> float:
    return math.exp(math.lgamma((df + 1) / 2) - math.lgamma(df / 2) - 0.5 * math.log(df * math.pi) - (df + 1) / 2 * math.log1p(x * x / df))


def t_cdf_positive(x: float, df: int) -> float:
    # Numerically integrate the Student-t density using adaptive Simpson.  The
    # quantile is evaluated only once per distinct sample size, so this remains
    # deterministic and inexpensive for normal listening panels.
    if x <= 0:
        return 0.5
    n = 256
    upper = x
    def simpson(count: int) -> float:
        h = upper / count
        total = t_pdf(0.0, df) + t_pdf(upper, df)
        for i in range(1, count):
            total += (4.0 if i % 2 else 2.0) * t_pdf(i * h, df)
        return total * h / 3.0
    area = simpson(n)
    for _ in range(8):
        refined = simpson(n * 2)
        if abs(refined - area) < 1e-11:
            area = refined
            break
        area, n = refined, n * 2
    return min(1.0, 0.5 + area)


@lru_cache(maxsize=None)
def t_critical_95(df: int) -> float | None:
    if df < 1:
        return None
    target = 0.975
    low, high = 0.0, 16.0
    for _ in range(70):
        mid = (low + high) / 2.0
        if t_cdf_positive(mid, df) < target:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def scale_from(manifest: dict[str, Any]) -> tuple[float | None, float | None]:
    scale = manifest.get("scale")
    if not isinstance(scale, dict):
        return None, None
    def number(value: Any) -> float | None:
        return float(value) if isinstance(value, (int, float)) and math.isfinite(float(value)) else None
    return number(scale.get("min")), number(scale.get("max"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--mos", type=Path, required=True)
    parser.add_argument("--ci95", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--task-id")
    args = parser.parse_args()
    manifest = load_json(args.manifest)
    task_id = args.task_id or (str(manifest.get("task_id")) if manifest.get("task_id") is not None else None)
    declared_samples = {str(x) for x in manifest.get("sample_ids", [])} if isinstance(manifest.get("sample_ids"), list) else set()
    declared_candidates = {str(x) for x in manifest.get("candidate_keys", [])} if isinstance(manifest.get("candidate_keys"), list) else set()
    scale_min, scale_max = scale_from(manifest)
    groups: dict[tuple[str, str], list[float]] = {}
    complete_rows = 0
    excluded_incomplete = 0
    provenance: dict[str, str | None] = {"task_id": task_id, "dataset_version": str(manifest.get("dataset_version")) if manifest.get("dataset_version") is not None else None, "audio_version": None}
    with args.input.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    # Re-check completeness from the dedup table, even when the producer did
    # not include group_complete.  This prevents partial batches entering MOS.
    by_listener_sample: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in rows:
        if task_id and row.get("task_id") != task_id:
            continue
        listener = " ".join((row.get("listener_id") or row.get("user_id") or "").strip().split())
        sample = row.get("sample_id", "")
        if listener and sample:
            by_listener_sample.setdefault((listener, sample), []).append(row)
    complete_groups: set[tuple[str, str]] = set()
    for group, group_rows in by_listener_sample.items():
        present = {r.get("candidate_key", "") for r in group_rows}
        marked = {r.get("group_complete", "").casefold() for r in group_rows}
        complete = bool(declared_candidates and present == declared_candidates) if declared_candidates else ("false" not in marked)
        if complete:
            complete_groups.add(group)
    for row in rows:
        if task_id and row.get("task_id") != task_id:
            continue
        sample, candidate = row.get("sample_id", ""), row.get("candidate_key", "")
        if declared_samples and sample not in declared_samples:
            continue
        if declared_candidates and candidate not in declared_candidates:
            continue
        listener = " ".join((row.get("listener_id") or row.get("user_id") or "").strip().split())
        if (listener, sample) not in complete_groups:
            excluded_incomplete += 1
            continue
        try:
            score = float(row.get("score", ""))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(score):
            continue
        if scale_min is not None and score < scale_min - 1e-9:
            continue
        if scale_max is not None and score > scale_max + 1e-9:
            continue
        groups.setdefault((sample, candidate), []).append(score)
        complete_rows += 1
        if provenance["audio_version"] is None and row.get("audio_version"):
            provenance["audio_version"] = row["audio_version"]
    args.mos.parent.mkdir(parents=True, exist_ok=True)
    mos_fields = ["sample_id", "candidate_key", "n", "mos", "sd", "task_id", "dataset_version", "audio_version"]
    ci_fields = ["sample_id", "candidate_key", "n", "ci95_low", "ci95_high", "method", "available", "task_id", "dataset_version", "audio_version"]
    with args.mos.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=mos_fields)
        writer.writeheader()
        for (sample, candidate), values in sorted(groups.items()):
            mean = sum(values) / len(values)
            sd = statistics.stdev(values) if len(values) > 1 else ""
            writer.writerow({"sample_id": sample, "candidate_key": candidate, "n": len(values), "mos": mean, "sd": sd, **provenance})
    with args.ci95.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ci_fields)
        writer.writeheader()
        for (sample, candidate), values in sorted(groups.items()):
            if len(values) < 2:
                low = high = ""
                method, available = "unavailable (n<2)", False
            else:
                critical = t_critical_95(len(values) - 1)
                mean = sum(values) / len(values)
                margin = critical * statistics.stdev(values) / math.sqrt(len(values)) if critical is not None else float("nan")
                low, high = mean - margin, mean + margin
                method, available = "t-distribution 95% CI", True
            writer.writerow({"sample_id": sample, "candidate_key": candidate, "n": len(values), "ci95_low": low, "ci95_high": high, "method": method, "available": available, **provenance})
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        report = {
            "schema": "sure.audio_eval.condition_summary.v1", "task_id": task_id,
            "analysis_unit": "sample_x_candidate", "mos_path": str(args.mos), "ci95_path": str(args.ci95),
            "condition_count": len(groups), "status": "success" if groups else "failed", "mos_scale": {"min": scale_min, "max": scale_max},
            "ci_method": "Student t 95% CI; unavailable when n < 2", "complete_groups": len(complete_groups),
            "excluded_incomplete_rows": excluded_incomplete, "aggregated_rows": complete_rows,
            "dataset_version": provenance["dataset_version"], "audio_version": provenance["audio_version"],
        }
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

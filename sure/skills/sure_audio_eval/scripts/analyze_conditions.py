#!/usr/bin/env python3
"""Compute model-condition MOS-to-objective correlations.

The analysis unit is one ``sample_id × model candidate`` condition point.
Reference and unprocessed conditions stay in the input tables, but never enter
the correlation rows. Screening is a rating-level sensitivity analysis and
only reads the subjective table; objective values and correlations cannot
affect which ratings are flagged.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any


DEFAULT_EXCLUDED = {"hidden_reference", "unprocessed"}
SCREENING_RULE_VERSION = "rating-level-3iqr-median-deviation-v1"


def read_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def sha256(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    # Keep the deterministic analyzer runnable on the Python 3.7 runtime used
    # by the current evaluation image; statistics.fmean arrived in Python 3.8.
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    den_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if den_x == 0 or den_y == 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / (den_x * den_y)


def ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    result = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and values[order[end + 1]] == values[order[start]]:
            end += 1
        rank = (start + end) / 2.0 + 1.0
        for index in range(start, end + 1):
            result[order[index]] = rank
        start = end + 1
    return result


def spearman(xs: list[float], ys: list[float]) -> float | None:
    return pearson(ranks(xs), ranks(ys)) if len(xs) >= 2 else None


def correlation(xs: list[float], ys: list[float]) -> tuple[float | None, float | None]:
    """Return Pearson and tie-aware Spearman coefficients."""
    return pearson(xs, ys), spearman(xs, ys)


def normalize_listener(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def role_for(candidate: str, manifest: dict[str, Any]) -> str:
    """Read role declarations without guessing a role from objective values."""
    for key in ("candidate_roles", "reference_roles"):
        mapping = manifest.get(key)
        if isinstance(mapping, dict) and candidate in mapping:
            return str(mapping[candidate]).casefold()
    for key in ("candidates", "candidate_manifest"):
        entries = manifest.get(key)
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and entry.get("candidate_key") == candidate:
                    return str(entry.get("role", entry.get("reference_role", "model"))).casefold()
    return "model"


def model_candidates(manifest: dict[str, Any], excluded: set[str]) -> set[str]:
    declared = manifest.get("candidate_keys")
    candidates = {str(item) for item in declared if isinstance(item, str)} if isinstance(declared, list) else set()
    if not candidates:
        entries = manifest.get("candidates")
        if isinstance(entries, list):
            candidates = {str(item["candidate_key"]) for item in entries if isinstance(item, dict) and isinstance(item.get("candidate_key"), str)}
    if not candidates:
        return set()
    selected = manifest.get("analysis_candidate_keys")
    if not isinstance(selected, list) or not selected:
        raise ValueError("task manifest must declare analysis_candidate_keys")
    selected_set = {str(item) for item in selected}
    if not selected_set.issubset(candidates):
        raise ValueError("analysis_candidate_keys must be a subset of candidate_keys")
    invalid = {
        candidate
        for candidate in selected_set
        if candidate in excluded or role_for(candidate, manifest) not in {"model", "enhancement", "enhanced_model", "system"}
    }
    if invalid:
        raise ValueError(f"analysis_candidate_keys contain non-model candidates: {sorted(invalid)}")
    return selected_set


def quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    index = probability * (len(ordered) - 1)
    low, high = math.floor(index), math.ceil(index)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def minimum_screening_ratings(manifest: dict[str, Any], model_candidates_set: set[str]) -> int:
    # The sensitivity rule is frozen across the standalone screener and the
    # joint analyzer. Eight ratings are the minimum group size.
    return 8


def screen_ratings(rows: list[dict[str, str]], model_candidates_set: set[str], min_ratings: int | None = None) -> tuple[set[tuple[str, str, str]], dict[str, Any]]:
    """Flag individual ratings using subjective values only."""
    groups: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for row in rows:
        if str(row.get("group_complete", "true")).casefold() == "false":
            continue
        if str(row.get("candidate_key", "")) not in model_candidates_set:
            continue
        try:
            score = float(row.get("score", ""))
        except (TypeError, ValueError):
            continue
        if math.isfinite(score):
            groups.setdefault((row.get("sample_id", ""), row.get("candidate_key", "")), []).append((normalize_listener(row.get("listener_id") or row.get("user_id")), score))
    flagged: set[tuple[str, str, str]] = set()
    details: list[dict[str, Any]] = []
    for (sample_id, candidate_key), values in sorted(groups.items()):
        if len(values) < (min_ratings or 8):
            continue
        scores = [score for _, score in values]
        q1, q3, median = quantile(scores, 0.25), quantile(scores, 0.75), statistics.median(scores)
        if q3 <= q1:
            continue
        bound = 3.0 * (q3 - q1)
        for listener, score in values:
            if (score < q1 - bound or score > q3 + bound) and abs(score - median) >= 20:
                key = (listener, sample_id, candidate_key)
                flagged.add(key)
                details.append({"sample_id": sample_id, "candidate_key": candidate_key, "listener_id": listener, "score": score, "reason": "3×IQR and 20-point median deviation"})
    return flagged, {"rule_version": SCREENING_RULE_VERSION, "minimum_ratings": min_ratings or 8, "rule": "rating-level 3×IQR + n≥8 + median deviation≥20", "objective_independent": True, "flagged_count": len(flagged), "flagged": details, "raw_scores_unchanged": True}


def mos_from_ratings(rows: list[dict[str, str]], excluded: set[tuple[str, str, str]], model_candidates_set: set[str]) -> dict[tuple[str, str], float]:
    groups: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        if str(row.get("group_complete", "true")).casefold() == "false":
            continue
        if str(row.get("candidate_key", "")) not in model_candidates_set:
            continue
        key = (normalize_listener(row.get("listener_id") or row.get("user_id")), row.get("sample_id", ""), row.get("candidate_key", ""))
        if key in excluded:
            continue
        try:
            score = float(row.get("score", ""))
        except (TypeError, ValueError):
            continue
        if math.isfinite(score):
            groups.setdefault((key[1], key[2]), []).append(score)
    return {key: sum(values) / len(values) for key, values in groups.items() if values}


def read_mos(path: Path) -> dict[tuple[str, str], float]:
    values: dict[tuple[str, str], float] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            try:
                value = float(row["mos"])
            except (KeyError, TypeError, ValueError):
                continue
            if finite(value):
                values[(row.get("sample_id", ""), row.get("candidate_key", ""))] = value
    return values


def read_objective(path: Path, task_id: str, plan: dict[str, Any], manifest: dict[str, Any], audio_manifest: dict[str, Any]) -> tuple[dict[str, dict[tuple[str, str], float]], dict[str, dict[str, Any]]]:
    values: dict[str, dict[tuple[str, str], float]] = {}
    metadata: dict[str, dict[str, Any]] = {}
    planned = {str(item.get("metric")): item for item in plan.get("metrics", []) if isinstance(item, dict) and item.get("metric")}
    samples = {str(value) for value in manifest.get("sample_ids", [])}
    candidates = {str(value) for value in manifest.get("candidate_keys", [])}
    audio_hashes = {(str(item.get("sample_id")), str(item.get("candidate_key"))): str(item.get("audio_sha256")) for item in audio_manifest.get("items", []) if isinstance(item, dict) and item.get("sample_id") and item.get("candidate_key") and item.get("audio_sha256")}
    raw_rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if isinstance(row, dict):
                raw_rows.append(row)
            if not isinstance(row, dict) or row.get("task_id") != task_id:
                continue
            metric = str(row.get("metric", ""))
            sample_id, candidate = str(row.get("sample_id", "")), str(row.get("candidate_key", ""))
            if not metric or not sample_id or not candidate:
                continue
            if sample_id not in samples or candidate not in candidates:
                raise ValueError(f"objective condition is outside the task manifest: {sample_id} × {candidate}")
            if row.get("task_version") != manifest.get("task_version") or row.get("dataset_version") != manifest.get("dataset_version") or row.get("audio_version") != (manifest.get("audio_policy") or {}).get("audio_version"):
                raise ValueError("objective row task/dataset/audio version mismatch")
            if metric not in planned:
                raise ValueError(f"objective metric {metric} is absent from objective_metric_plan.json")
            if planned[metric].get("participates_in_model_correlation", True) is False:
                continue
            try:
                value = float(row["value"])
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(value):
                continue
            if audio_hashes and row.get("audio_sha256") != audio_hashes.get((sample_id, candidate)):
                raise ValueError(f"objective audio hash mismatch: {sample_id} × {candidate}")
            declared = planned.get(metric, {})
            direction = str(declared.get("direction", row.get("direction", "")))
            planned_mode = str(declared.get("reference_mode", ""))
            row_mode = str(row.get("reference_mode", ""))
            if planned_mode and row_mode and ((planned_mode == "paired") != (row_mode == "paired")):
                raise ValueError(f"objective reference_mode disagrees with metric plan for {metric}")
            if direction.casefold() in {"lower_better", "lower-is-better", "lower"}:
                value = -value
            key = (sample_id, candidate)
            if key in values.setdefault(metric, {}):
                raise ValueError(f"duplicate objective condition for metric {metric}: {sample_id} × {candidate}")
            values[metric][key] = value
            metadata.setdefault(metric, {"metric_type": declared.get("type", row.get("metric_type", row.get("type"))), "direction": direction, "reference_mode": declared.get("reference_mode", row.get("reference_mode")), "pipeline_id": row.get("pipeline_id", declared.get("pipeline_id")), "engine_commit": row.get("engine_commit"), "runtime_lock": row.get("runtime_lock", row.get("runtime_lock_sha256")), "capability_manifest_sha256": row.get("capability_manifest_sha256", row.get("capabilities_sha256"))})
    # The strict objective contract is available in the shared script bundle.
    # Older callers without an audio manifest retain the lightweight reader
    # path, while full joint analysis validates the complete objective table.
    if audio_manifest:
        try:
            from objective_contract import validate_objective_rows
        except ImportError:
            validate_objective_rows = None
        if validate_objective_rows is not None:
            validate_objective_rows(raw_rows, manifest, plan, audio_manifest)
    return values, metadata


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mos", type=Path, required=True)
    parser.add_argument("--objective", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--metric-plan", type=Path)
    parser.add_argument("--subjective", type=Path)
    parser.add_argument("--screening-report", type=Path)
    parser.add_argument("--context", type=Path)
    parser.add_argument("--audio-manifest", type=Path)
    args = parser.parse_args()

    manifest = read_json(args.manifest)
    plan = read_json(args.metric_plan)
    context = read_json(args.context)
    excluded = {str(value) for value in manifest.get("excluded_candidates_from_correlation", DEFAULT_EXCLUDED) if isinstance(value, str)} | DEFAULT_EXCLUDED
    declared_models = model_candidates(manifest, excluded)
    mos_before = {key: value for key, value in read_mos(args.mos).items() if key[1] in declared_models}
    rows: list[dict[str, str]] = []
    if args.subjective and args.subjective.is_file():
        with args.subjective.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
    min_ratings = minimum_screening_ratings(manifest, declared_models)
    flagged, screening_detail = screen_ratings(rows, declared_models, min_ratings) if rows else (set(), {"rule_version": SCREENING_RULE_VERSION, "rule": "not run; no subjective source supplied", "objective_independent": True, "flagged_count": 0, "flagged": [], "raw_scores_unchanged": True})
    if args.screening_report and args.screening_report.is_file():
        supplied = read_json(args.screening_report)
        if supplied.get("rule_version") != SCREENING_RULE_VERSION or supplied.get("objective_independent") is not True:
            raise ValueError("screening report rule version or objective independence mismatch")
        supplied_flagged = supplied.get("flagged") if isinstance(supplied.get("flagged"), list) else []
        supplied_flagged_set = {
            (normalize_listener(item.get("listener_id")), str(item.get("sample_id", "")), str(item.get("candidate_key", "")))
            for item in supplied_flagged if isinstance(item, dict)
        }
        if supplied_flagged_set != flagged or supplied.get("flagged_count") != len(supplied_flagged_set) or supplied.get("raw_scores_unchanged") is not True:
            raise ValueError("screening report is stale relative to the current subjective rows")
        flagged = supplied_flagged_set
        screening_detail = supplied
    mos_after = mos_from_ratings(rows, flagged, declared_models) if rows else dict(mos_before)
    objective, metadata = read_objective(args.objective, args.task_id, plan, manifest, read_json(args.audio_manifest))
    all_candidates = {key[1] for values in objective.values() for key in values} | {key[1] for key in mos_before}
    if not declared_models:
        raise ValueError("task manifest must declare at least one model candidate role")

    correlations: list[dict[str, Any]] = []
    for metric in sorted(objective):
        info = metadata.get(metric, {})
        for screening_name, mos in (("before", mos_before), ("after", mos_after)):
            common = sorted(key for key in set(objective[metric]) & set(mos) if key[1] in declared_models and key[1] not in excluded)
            xs = [mos[key] for key in common]
            ys = [objective[metric][key] for key in common]
            correlations.append({"metric": metric, "screening": screening_name, "n": len(common), "model_count": len({key[1] for key in common}), "pearson": pearson(xs, ys), "spearman": spearman(xs, ys), "metric_type": info.get("metric_type"), "direction": info.get("direction"), "reference_mode": info.get("reference_mode"), "condition_keys": [{"sample_id": key[0], "candidate_key": key[1]} for key in common], "provenance": {key: value for key, value in info.items() if value is not None}})

    source_files = []
    for path, kind in ((args.mos, "mos"), (args.objective, "objective"), (args.manifest, "task_manifest"), (args.metric_plan, "metric_plan"), (args.subjective, "subjective"), (args.context, "context"), (args.audio_manifest, "audio_manifest")):
        digest = sha256(path)
        if digest:
            source_files.append({"path": str(path), "kind": kind, "sha256": digest})
    audio_manifest = read_json(args.audio_manifest)
    if not correlations:
        raise ValueError("no common model-condition points are available for correlation")
    report = {"schema": "sure.audio_eval.analysis.v1", "task_id": args.task_id, "dataset_version": manifest.get("dataset_version"), "task_version": manifest.get("task_version"), "audio_version": audio_manifest.get("audio_version", manifest.get("audio_policy", {}).get("audio_version")), "correlation_unit": "model_condition", "model_candidates": sorted(declared_models), "excluded_candidates": sorted(excluded), "metric_plan": [dict(item) for item in plan.get("metrics", []) if isinstance(item, dict)], "correlations": correlations, "screening": {"before": {"condition_count": len(mos_before), "screened": False}, "after": {"condition_count": len(mos_after), "screened": bool(rows), "flagged_count": screening_detail.get("flagged_count", 0)}, "rule_version": SCREENING_RULE_VERSION, **screening_detail}, "fit": {"enabled": False, "reason": "Model-condition MOS-to-metric correlation is the primary result; no fitted curve is reported."}, "p1401": {"layers": ["raw_listener_scores", "mos_sd_t_ci95", "objective_provenance", "model_condition_correlations", "screening_before_after"]}, "provenance": {"engine_commit": context.get("engine_commit"), "runtime_lock": context.get("runtime_lock_sha256", context.get("runtime_lock")), "capability_manifest_sha256": context.get("capability_manifest_sha256", context.get("capabilities_sha256")), "source_files": source_files}, "status": "success", "report_files": [str(args.output)]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.screening_report:
        args.screening_report.parent.mkdir(parents=True, exist_ok=True)
        args.screening_report.write_text(json.dumps({"schema": "sure.audio_eval.screening.v1", **screening_detail}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

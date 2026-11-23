"""Shared objective-row contract used by execution and analysis gates."""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, Set, Tuple


def validate_objective_rows(rows: Iterable[Dict[str, Any]], task: Dict[str, Any], plan: Dict[str, Any], audio: Dict[str, Any]) -> None:
    samples = {str(value) for value in task.get("sample_ids", [])}
    candidates = {str(value) for value in task.get("candidate_keys", [])}
    policy = task.get("audio_policy") if isinstance(task.get("audio_policy"), dict) else {}
    planned = {
        str(item.get("metric")): item
        for item in plan.get("metrics", [])
        if isinstance(item, dict) and item.get("metric") and item.get("participates_in_model_correlation", True) is not False
    }
    audio_hashes = {
        (str(item.get("sample_id")), str(item.get("candidate_key"))): str(item.get("audio_sha256")).lower()
        for item in audio.get("items", [])
        if isinstance(item, dict) and item.get("sample_id") and item.get("candidate_key") and item.get("audio_sha256")
    }
    expected: Set[Tuple[str, str, str]] = {
        (sample, candidate, metric)
        for sample in samples
        for candidate in candidates
        for metric in planned
    }
    seen: Set[Tuple[str, str, str]] = set()
    for row in rows:
        required = ("task_id", "task_version", "dataset_version", "audio_version", "sample_id", "candidate_key", "metric", "value", "direction", "reference_mode", "pipeline_id", "audio_sha256", "engine_commit", "capability_manifest_sha256")
        missing = [field for field in required if row.get(field) in (None, "")]
        if missing:
            raise ValueError("objective row missing required fields: {}".format(", ".join(missing)))
        if row.get("task_id") != task.get("task_id"):
            raise ValueError("objective row task_id differs from task manifest")
        if row.get("task_version") != task.get("task_version") or row.get("dataset_version") != task.get("dataset_version") or row.get("audio_version") != policy.get("audio_version"):
            raise ValueError("objective row task/dataset/audio version differs from task manifest")
        sample, candidate, metric = str(row["sample_id"]), str(row["candidate_key"]), str(row["metric"])
        if sample not in samples or candidate not in candidates:
            raise ValueError("objective row is outside task sample/candidate whitelist")
        spec = planned.get(metric)
        if spec is None:
            raise ValueError("objective metric is absent from metric plan: {}".format(metric))
        for field in ("direction", "reference_mode", "pipeline_id"):
            if row.get(field) != spec.get(field):
                raise ValueError("objective row {} differs from metric plan".format(field))
        if row.get("engine_commit") != plan.get("engine_commit"):
            raise ValueError("objective row engine_commit differs from locked plan")
        if row.get("capability_manifest_sha256") != plan.get("capability_manifest_sha256"):
            raise ValueError("objective row capability hash differs from locked plan")
        if audio_hashes.get((sample, candidate)) != str(row["audio_sha256"]).lower():
            raise ValueError("objective row audio hash differs from audio manifest")
        value = row.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
            raise ValueError("objective value must be finite")
        if row["reference_mode"] == "paired" and not row.get("reference_audio_sha256"):
            raise ValueError("paired objective row needs reference_audio_sha256")
        if row["reference_mode"] == "none" and row.get("reference_audio_sha256") is not None:
            raise ValueError("no-reference objective row must not carry reference_audio_sha256")
        key = (sample, candidate, metric)
        if key in seen:
            raise ValueError("duplicate objective condition: {}".format(key))
        seen.add(key)
    if seen != expected:
        raise ValueError("objective rows do not cover the complete task sample × candidate × metric matrix")

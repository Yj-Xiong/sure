#!/usr/bin/env python3
"""Resolve objective routes from the locked SURE capability manifest.

This planner never computes scores and never guesses metric semantics from a
metric name.  A requested metric must have one exact route in the capability
snapshot, and its direction must be declared by the task manifest or the
snapshot itself.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

SCHEMA = "sure.audio_eval.metric_plan.v1"
STAGES = ("resolve_task", "resolve_metrics", "validate_audio", "run_objective", "ingest_subjective", "aggregate_subjective", "joint_analysis", "screening", "render_report")
ALIASES = {"sisdr": "si_sdr", "si-sdr": "si_sdr", "wvmos": "wv_mos", "wv-mos": "wv_mos"}
MODEL_ROLES = {"model", "method", "enhancement", "enhanced_model", "system"}


class PlanError(ValueError):
    """A deterministic planning failure."""


def norm(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    return ALIASES.get(raw, raw)


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PlanError(f"expected JSON object: {path}")
    return value


def sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise PlanError(f"cannot hash {path}: {exc}") from exc


def text(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise PlanError(f"{field} must be a non-empty string")
    return result


def metric_specs(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Merge explicit ``metric_policies`` and ``objective_metrics`` entries."""
    result: dict[str, dict[str, Any]] = {}
    policies = manifest.get("metric_policies")
    if policies is not None:
        if not isinstance(policies, dict):
            raise PlanError("task manifest metric_policies must be an object keyed by metric")
        for metric, spec in policies.items():
            if not isinstance(spec, dict):
                raise PlanError(f"metric_policies.{metric} must be an object")
            result[norm(metric)] = dict(spec)
    declared = manifest.get("objective_metrics")
    if declared is None:
        return result
    entries: list[tuple[str, dict[str, Any]]] = []
    if isinstance(declared, dict):
        entries = [(str(key), value) for key, value in declared.items()]
    elif isinstance(declared, list):
        for item in declared:
            if not isinstance(item, dict):
                raise PlanError("task manifest objective_metrics entries must be objects")
            entries.append((str(item.get("metric") or ""), item))
    else:
        raise PlanError("task manifest objective_metrics must be an object or array")
    for raw_metric, spec in entries:
        metric = norm(raw_metric)
        if not metric or not isinstance(spec, dict):
            raise PlanError("each objective_metrics entry needs a metric and an object policy")
        current = result.setdefault(metric, {})
        for key, value in spec.items():
            if key in current and current[key] != value:
                raise PlanError(f"conflicting metric policy for {metric!r}: {key}")
            current[key] = value
    return result


def standards_from_markdown(path: Path) -> dict[str, list[str]]:
    """Read the maintained stage-to-standard table, without adding references."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PlanError(f"cannot read standards mapping {path}: {exc}") from exc
    mapping: dict[str, list[str]] = {}
    for line in lines:
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        stage = cells[0].strip("`")
        if len(cells) < 4 or stage not in STAGES or stage.startswith("-"):
            continue
        refs = [value.strip() for value in re.split(r"[；;]", cells[3]) if value.strip()]
        if refs:
            mapping[stage] = refs
    missing = [stage for stage in STAGES if not mapping.get(stage)]
    if missing:
        raise PlanError(f"standards mapping is missing stages: {', '.join(missing)}")
    return mapping


def route_for_metric(routes: list[Any], metric: str, policy: dict[str, Any]) -> dict[str, Any]:
    matches = [route for route in routes if isinstance(route, dict) and norm(route.get("metric")) == metric]
    pipeline = policy.get("pipeline_id")
    if pipeline:
        matches = [route for route in matches if route.get("pipeline_id") == pipeline]
    if not matches:
        suffix = f" and pipeline_id={pipeline!r}" if pipeline else ""
        raise PlanError(f"metric {metric!r} has no route in the locked capability manifest{suffix}")
    if len(matches) > 1:
        raise PlanError(f"metric {metric!r} has multiple routes; declare an exact pipeline_id")
    return matches[0]


def route_semantics(route: dict[str, Any], policy: dict[str, Any], metric: str) -> tuple[str, str, bool, list[str], list[str], bool]:
    required = [text(role, f"route {metric}.required_roles") for role in route.get("required_roles") or []]
    optional = [text(role, f"route {metric}.optional_roles") for role in route.get("optional_roles") or []]
    paired = "reference_audio" in required
    reference_mode = "paired" if paired else "none"
    metric_type = "intrusive" if paired else "non_intrusive"
    # The engine capability may list reference_audio as an optional role for a
    # no-reference provider.  The Harness policy is stricter: never pass a
    # paired reference to a non-intrusive metric, so omit it from executable
    # optional inputs while retaining the raw route under engine_route.
    if not paired:
        optional = [role for role in optional if role != "reference_audio"]
    if policy.get("reference_mode", reference_mode) != reference_mode:
        raise PlanError(f"metric {metric!r} reference_mode conflicts with locked route roles")
    if policy.get("type", metric_type) != metric_type:
        raise PlanError(f"metric {metric!r} type conflicts with locked route reference roles")
    if policy.get("matching_reference_required", paired) is not paired:
        raise PlanError(f"metric {metric!r} matching_reference_required conflicts with locked route")
    participates = policy.get("participates_in_model_correlation", policy.get("participates_in_correlation", True))
    if not isinstance(participates, bool):
        raise PlanError(f"metric {metric!r} participates_in_model_correlation must be boolean")
    return metric_type, reference_mode, paired, required, optional, participates


def direction(route: dict[str, Any], policy: dict[str, Any], task: dict[str, Any], metric: str) -> tuple[str, str]:
    value = policy.get("direction")
    source = "task_manifest.metric_policies"
    if value is None:
        value = route.get("direction")
        source = "engine_capability.route.direction"
    if value is None and isinstance(route.get("higher_is_better"), bool):
        value = "higher_better" if route["higher_is_better"] else "lower_better"
        source = "engine_capability.route.higher_is_better"
    task_policies = task.get("metric_policies")
    if value is None and isinstance(task_policies, dict) and isinstance(task_policies.get(metric), dict):
        value = task_policies[metric].get("direction")
        source = "engine_capability.metric_policies"
    if value not in {"higher_better", "lower_better"}:
        raise PlanError(f"metric {metric!r} has no explicit direction; refusing to guess")
    return str(value), source


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    manifest = load(args.task_manifest)
    capabilities = load(args.capabilities)
    if text(manifest.get("task_id"), "task_id") != args.task_id:
        raise PlanError("task manifest task_id mismatch")
    requested_task = args.engine_task or manifest.get("engine_task") or manifest.get("objective_task") or manifest.get("task_type")
    if not requested_task:
        raise PlanError("task manifest must declare engine_task before resolving objective metrics")
    task_name = norm(requested_task)
    tasks = capabilities.get("tasks")
    task = tasks.get(task_name) if isinstance(tasks, dict) else None
    if not isinstance(task, dict) or task.get("ready") is not True:
        raise PlanError(f"locked capability task {task_name!r} is missing or not ready")
    routes = task.get("routes")
    if not isinstance(routes, list) or not routes:
        raise PlanError(f"locked capability task {task_name!r} has no routes")
    requested = [norm(value) for value in args.metrics.split(",") if value.strip()]
    if not requested:
        raise PlanError("at least one objective metric is required")
    if len(set(requested)) != len(requested):
        raise PlanError("objective metrics must be unique after alias normalisation")
    policies = metric_specs(manifest)
    rows: list[dict[str, Any]] = []
    for metric in requested:
        policy = policies.get(metric, {})
        route = route_for_metric(routes, metric, policy)
        metric_type, reference_mode, matching, required, optional, participates = route_semantics(route, policy, metric)
        metric_direction, direction_source = direction(route, policy, task, metric)
        rows.append({"metric": metric, "type": metric_type, "direction": metric_direction, "direction_source": direction_source, "reference_mode": reference_mode, "pipeline_id": text(route.get("pipeline_id"), f"route {metric}.pipeline_id"), "input_contract": text(route.get("input_contract"), f"route {metric}.input_contract"), "required_audio_inputs": required, "optional_audio_inputs": optional, "matching_reference_required": matching, "participates_in_model_correlation": participates, "engine_route": dict(route)})
    runtime = load(args.runtime) if args.runtime else None
    engine = capabilities.get("engine") if isinstance(capabilities.get("engine"), dict) else {}
    engine_commit = text(engine.get("commit"), "capabilities.engine.commit")
    if runtime is not None and runtime.get("engine_commit") != engine_commit:
        raise PlanError(f"runtime lock engine_commit {runtime.get('engine_commit')!r} differs from capability manifest {engine_commit!r}")
    roles = manifest.get("candidate_roles")
    if not isinstance(roles, dict):
        raise PlanError("task manifest candidate_roles must be an object")
    candidate_keys = [str(key) for key in manifest.get("candidate_keys") or []]
    selected = manifest.get("analysis_candidate_keys")
    if not isinstance(selected, list) or not selected:
        raise PlanError("task manifest must declare analysis_candidate_keys before resolving metrics")
    models = [str(key) for key in selected]
    if len(set(models)) != len(models):
        raise PlanError("analysis_candidate_keys must be unique")
    if any(key not in candidate_keys for key in models):
        raise PlanError("analysis_candidate_keys must be a subset of candidate_keys")
    if any(str(roles.get(key) or "").strip().lower() not in MODEL_ROLES or key in {"hidden_reference", "unprocessed"} for key in models):
        raise PlanError("analysis_candidate_keys must contain only declared model candidate roles")
    if not models:
        raise PlanError("task manifest declares no model candidates")
    audio_policy = manifest.get("audio_policy")
    if not isinstance(audio_policy, dict):
        raise PlanError("task manifest audio_policy must be an object")
    runtime_lock_file = None
    runtime_lock_file_sha256 = None
    if args.runtime and isinstance(runtime, dict):
        lock_name = str(runtime.get("lock_file") or "").strip()
        if lock_name:
            lock_path = args.runtime.parent / lock_name
            if not lock_path.is_file():
                raise PlanError(f"locked runtime requirements file is missing: {lock_path}")
            # Persist the lock name from runtime.json rather than an absolute
            # checkout path so the artifact remains portable across devices.
            runtime_lock_file = lock_name
            runtime_lock_file_sha256 = sha256(lock_path)
    return {"schema": SCHEMA, "task_id": args.task_id, "task_version": text(manifest.get("task_version"), "task_version"), "dataset_version": text(manifest.get("dataset_version"), "dataset_version"), "audio_version": text(audio_policy.get("audio_version"), "audio_policy.audio_version"), "engine_task": task_name, "engine_commit": engine_commit, "runtime_lock_sha256": sha256(args.runtime) if args.runtime else None, "runtime_lock_file": runtime_lock_file, "runtime_lock_file_sha256": runtime_lock_file_sha256, "capability_manifest_sha256": sha256(args.capabilities), "analysis_unit": "model_condition", "excluded_candidates": ["hidden_reference", "unprocessed"], "model_candidate_keys": models, "standards_by_stage": standards_from_markdown(args.standards), "standards_source": str(args.standards), "capabilities_source": str(args.capabilities), "runtime_source": str(args.runtime) if args.runtime else None, "metrics": rows, "status": "success"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-manifest", type=Path, required=True)
    parser.add_argument("--capabilities", type=Path, required=True)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metrics", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--engine-task")
    parser.add_argument("--standards", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = build_plan(args)
    except Exception as exc:
        manifest = {}
        try:
            manifest = load(args.task_manifest)
        except Exception:
            pass
        result = {
            "schema": SCHEMA,
            "task_id": args.task_id,
            "task_version": str(manifest.get("task_version") or "unknown"),
            "dataset_version": str(manifest.get("dataset_version") or "unknown"),
            "audio_version": str((manifest.get("audio_policy") or {}).get("audio_version") or "unknown"),
            "metrics": [],
            "analysis_unit": "model_condition",
            "excluded_candidates": ["hidden_reference", "unprocessed"],
            "model_candidate_keys": [],
            "standards_by_stage": {},
            "status": "failed",
            "diagnostics": [{"code": type(exc).__name__, "message": str(exc)}],
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

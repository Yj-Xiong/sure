#!/usr/bin/env python3
"""Run locked SURE-EVALUATION routes or normalize an explicit result bundle.

Audio rows are evaluated through the checked-in engine. A missing or mismatched
lock identity is a hard failure; this adapter never fabricates a score.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_ENGINE_ROOT = REPO_ROOT / "sure" / "external" / "sure-evaluation"
DEFAULT_RUNTIME = REPO_ROOT / "sure" / "runtime" / "evaluation" / "runtime.json"
DEFAULT_CAPS = REPO_ROOT / "sure" / "runtime" / "evaluation" / "engine-capabilities.generated.json"
LOWER_IS_BETTER = {"wer", "cer", "mer", "der", "rmse", "loss"}


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pyproject_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def engine_commit(root: Path) -> str:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_CONFIG_") and key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}
    }
    rev = subprocess.run(
        ["git", "-c", f"safe.directory={root}", "rev-parse", "HEAD"],
        cwd=root, env=env, capture_output=True, text=True, check=False,
    )
    top = subprocess.run(
        ["git", "-c", f"safe.directory={root}", "rev-parse", "--show-toplevel"],
        cwd=root, env=env, capture_output=True, text=True, check=False,
    )
    if rev.returncode or top.returncode or not rev.stdout.strip():
        raise RuntimeError(f"evaluation engine is not a readable git checkout: {root}: {rev.stderr.strip()}")
    if Path(top.stdout.strip()).resolve() != root.resolve():
        raise RuntimeError(f"evaluation engine checkout is not independent: {root}")
    return rev.stdout.strip()


def locked_identity(engine_root: Path, runtime_path: Path, caps_path: Path) -> dict[str, Any]:
    if not engine_root.is_dir():
        raise RuntimeError(f"locked evaluation engine checkout is unavailable: {engine_root}")
    runtime = read_json(runtime_path)
    project = engine_root / "pyproject.toml"
    if not project.is_file():
        raise RuntimeError(f"evaluation engine pyproject.toml is missing: {engine_root}")
    commit = engine_commit(engine_root)
    expected = str(runtime.get("engine_commit") or "")
    if commit != expected:
        raise RuntimeError(f"evaluation engine commit differs from the locked runtime: expected={expected} actual={commit}")
    project_digest = pyproject_sha(project)
    if project_digest != runtime.get("engine_pyproject_sha256"):
        raise RuntimeError("evaluation engine pyproject.toml differs from the locked runtime after newline normalisation")
    if not caps_path.is_file():
        raise RuntimeError(f"engine capability manifest is missing: {caps_path}")
    caps = read_json(caps_path)
    cap_commit = str((caps.get("engine") or {}).get("commit") or "")
    if cap_commit != commit:
        raise RuntimeError(f"engine capability manifest commit differs from checkout: manifest={cap_commit} actual={commit}")
    lock_path = runtime_path.parent / str(runtime.get("lock_file") or "requirements.lock.txt")
    if not lock_path.is_file():
        raise RuntimeError(f"evaluation runtime lock is missing: {lock_path}")
    return {
        "engine_commit": commit,
        "engine_pyproject_sha256": project_digest,
        "capability_manifest_sha256": sha256_file(caps_path),
        "capability_manifest_path": str(caps_path),
        "runtime_lock_path": str(lock_path),
        "runtime_lock_sha256": sha256_file(lock_path),
        "runtime_version": runtime.get("runtime_version"),
        "materialization_version": runtime.get("materialization_version"),
        "python": runtime.get("python"),
    }


def normalize_metric(value: Any) -> str:
    return str(value).strip().lower().replace("_", "-")


def load_rows(path: Path) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            document = None
        if isinstance(document, dict) and isinstance(document.get("items"), list):
            rows = [item for item in document["items"] if isinstance(item, dict)]
            if not rows:
                raise ValueError(f"objective input items are empty: {path}")
            return rows
        if isinstance(document, list):
            rows = [item for item in document if isinstance(item, dict)]
            if not rows:
                raise ValueError(f"objective input array is empty: {path}")
            return rows
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at {path}:{line_no}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"objective input row must be an object at {path}:{line_no}")
        rows.append(value)
    if not rows:
        raise ValueError(f"objective input is empty: {path}")
    return rows


def normalize_existing(rows: list[dict[str, Any]], task_id: str, commit: str) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.get("task_id") != task_id:
            raise ValueError("objective row task_id mismatch")
        for field in ("sample_id", "candidate_key", "metric", "direction", "reference_mode"):
            if not isinstance(row.get(field), str) or not row[field]:
                raise ValueError(f"objective row missing {field}")
        value = row.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError("objective value must be finite")
        mode = row["reference_mode"]
        if mode == "paired" and not row.get("reference_audio_sha256"):
            raise ValueError("paired objective row needs reference_audio_sha256")
        if mode == "none" and row.get("reference_audio_sha256") is not None:
            raise ValueError("no-reference objective row must not carry a reference hash")
        if mode not in {"paired", "none"}:
            raise ValueError("reference_mode must be paired or none")
        key = (row["sample_id"], row["candidate_key"], row["metric"])
        if key in seen:
            raise ValueError(f"duplicate objective condition: {key}")
        seen.add(key)
        out.append({**row, "engine_commit": row.get("engine_commit", commit)})
    return out


def audio_path(row: dict[str, Any], field_names: tuple[str, ...]) -> str:
    for field in field_names:
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            path = Path(value).expanduser()
            if not path.is_file():
                raise ValueError(f"{field} does not exist: {path}")
            return str(path.resolve())
    raise ValueError(f"audio objective row needs {field_names[0]}")


def optional_audio(row: dict[str, Any], field: str) -> str:
    value = row.get(field)
    if value in (None, ""):
        return ""
    path = Path(str(value)).expanduser()
    if not path.is_file():
        raise ValueError(f"{field} does not exist: {path}")
    return str(path.resolve())


def checked_hash(path: str, declared: Any, field: str) -> str:
    actual = sha256_file(Path(path))
    if declared not in (None, "") and str(declared).lower() != actual:
        raise ValueError(f"{field} does not match file bytes: expected={declared} actual={actual}")
    return actual


def route_for(caps: dict[str, Any], task: str, metric: str) -> dict[str, Any]:
    task = {"speech_enhancement": "se", "speech-enhancement": "se"}.get(task, task)
    profile = (caps.get("tasks") or {}).get(task)
    if not isinstance(profile, dict) or profile.get("ready") is not True:
        raise ValueError(f"task {task!r} is not ready in the locked capability manifest")
    target = normalize_metric(metric)
    for route in profile.get("routes") or []:
        if not isinstance(route, dict):
            continue
        names = {normalize_metric(route.get("metric", ""))}
        names.update(normalize_metric(x) for x in route.get("aliases") or [])
        if target in names:
            return route
    supported = sorted(normalize_metric(x.get("metric", "")) for x in profile.get("routes") or [] if isinstance(x, dict))
    raise ValueError(f"metric {metric!r} has no locked route for task {task!r}; supported={supported}")


def reference_mode(route: dict[str, Any]) -> str:
    contract = str(route.get("input_contract") or "")
    required = {normalize_metric(x) for x in route.get("required_roles") or []}
    return "paired" if "reference-audio" in required or contract in {"scoring/si_sdr", "scoring/stoi", "scoring/pesq"} else "none"


def direction(metric: str) -> str:
    return "lower_is_better" if normalize_metric(metric) in LOWER_IS_BETTER else "higher_is_better"


def engine_python(runtime: dict[str, Any]) -> str:
    configured = os.environ.get("SURE_EVALUATION_PYTHON", "").strip()
    if not configured:
        raise RuntimeError("SURE_EVALUATION_PYTHON must point to the prepared locked Evaluation Runtime")
    executable = configured
    expected = str(runtime.get("python") or "").strip()
    # macOS framework Python exports __PYVENV_LAUNCHER__. When the parent
    # process is system Python, that variable can redirect a venv executable
    # back to the parent interpreter and silently drop its site-packages.
    # Remove it for every probe and engine invocation.
    os.environ.pop("__PYVENV_LAUNCHER__", None)
    if expected:
        probe_env = dict(os.environ)
        probe_env.pop("__PYVENV_LAUNCHER__", None)
        version = subprocess.run([executable, "-c", "import platform; print(platform.python_version())"], env=probe_env, capture_output=True, text=True, check=False)
        if version.stdout.strip().split(".")[:2] != expected.split(".")[:2]:
            raise RuntimeError(f"locked evaluation runtime requires Python {expected}; selected interpreter reports {version.stdout.strip() or version.stderr.strip()}")
    return executable


def run_engine(python: str, root: Path, metric: str, samples: Path, output: Path, device: str) -> tuple[dict[str, Any], str]:
    code = """
import json, sys
from sure_eval.evaluation.cli_adapters import build_pipeline_spec, run_pipeline_spec
task, metric, samples, output, device = sys.argv[1:]
spec = build_pipeline_spec(task, metric=metric)
summary = run_pipeline_spec(spec, output_dir=output, samples_jsonl=samples, device=device)
print(json.dumps(summary, ensure_ascii=False))
"""
    env = dict(os.environ)
    env.pop("__PYVENV_LAUNCHER__", None)
    env["PYTHONPATH"] = str(root / "src")
    completed = subprocess.run(
        [python, "-c", code, "se", metric, str(samples), str(output), device],
        cwd=root, env=env, capture_output=True, text=True, check=False, timeout=3600,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"locked engine route {metric} failed with exit code {completed.returncode}: {detail[-4000:]}")
    report_path = output / "report.json"
    if not report_path.is_file():
        raise RuntimeError(f"locked engine route {metric} produced no report.json")
    return read_json(report_path), completed.stdout.strip()


def scores_from_report(report: dict[str, Any], metric: str) -> dict[str, float]:
    canonical = normalize_metric(metric).replace("-", "_")
    details = report.get("details") if isinstance(report.get("details"), dict) else {}
    rows = details.get("rows") if isinstance(details.get("rows"), list) else []
    values: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        block = row.get("full_reference") if isinstance(row.get("full_reference"), dict) else row.get("mos")
        payload = block.get(canonical) if isinstance(block, dict) else None
        value = payload.get(canonical, payload.get("score")) if isinstance(payload, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            values[str(row.get("sample_id") or "")] = float(value)
    if not values:
        raise RuntimeError(f"engine report for {metric} contains no per-sample finite scores")
    return values


def compute_engine(rows: list[dict[str, Any]], task_id: str, task_version: str, dataset_version: str, audio_version: str, metrics: list[str], task: str, root: Path, identity: dict[str, Any], runtime: dict[str, Any], output: Path, device: str, metric_directions: dict[str, str] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    conditions: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for condition_index, row in enumerate(rows):
        sample_id = str(row.get("sample_id") or "")
        candidate = str(row.get("candidate_key") or "")
        if not sample_id or not candidate:
            raise ValueError("audio objective rows require sample_id and candidate_key")
        if (sample_id, candidate) in seen:
            raise ValueError(f"duplicate audio condition: {(sample_id, candidate)}")
        seen.add((sample_id, candidate))
        enhanced = audio_path(row, ("enhanced_audio", "audio_path", "prediction_audio", "audio", "path", "file_path"))
        reference = (
            optional_audio(row, "reference_audio")
            or optional_audio(row, "reference_audio_path")
            or optional_audio(row, "reference_path")
            or optional_audio(row, "reference")
        )
        noisy = optional_audio(row, "noisy_audio")
        conditions.append({
            "engine_id": f"condition-{condition_index:08d}",
            "sample_id": sample_id,
            "candidate_key": candidate,
            "enhanced_audio": enhanced,
            "reference_audio": reference,
            "noisy_audio": noisy,
            "audio_sha256": checked_hash(enhanced, row.get("audio_sha256"), "audio_sha256"),
            "reference_audio_sha256": checked_hash(reference, row.get("reference_audio_sha256"), "reference_audio_sha256") if reference else None,
        })
    caps = read_json(Path(identity["capability_manifest_path"]))
    python = engine_python(runtime)
    work_root = output.parent / "objective_engine_runs"
    work_root.mkdir(parents=True, exist_ok=True)
    route_entries: list[dict[str, Any]] = []
    result_rows: list[dict[str, Any]] = []
    for metric in metrics:
        metric_name = str(metric).strip()
        normalized = normalize_metric(metric_name)
        route = route_for(caps, task, normalized)
        mode = reference_mode(route)
        if mode == "paired" and any(not item["reference_audio"] for item in conditions):
            raise ValueError(f"full-reference metric {metric!r} requires reference_audio for every condition")
        if mode == "paired":
            by_sample: dict[str, set[str]] = {}
            for item in conditions:
                by_sample.setdefault(item["sample_id"], set()).add(str(item["reference_audio_sha256"]))
            if any(len(hashes) != 1 for hashes in by_sample.values()):
                raise ValueError(f"full-reference metric {metric!r} has mismatched reference audio within a sample")
        metric_dir = work_root / normalized.replace("-", "_")
        metric_dir.mkdir(parents=True, exist_ok=True)
        samples_path = metric_dir / "samples.jsonl"
        with samples_path.open("w", encoding="utf-8") as handle:
            for item in conditions:
                payload = {"sample_id": item["engine_id"], "enhanced_audio": item["enhanced_audio"], "noisy_audio": item["noisy_audio"]}
                if mode == "paired":
                    payload["reference_audio"] = item["reference_audio"]
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        report, stdout = run_engine(python, root, normalized, samples_path, metric_dir, device)
        values = scores_from_report(report, normalized)
        expected_pipeline_id = str(route.get("pipeline_id") or "")
        reported_pipeline_id = str(report.get("pipeline_id") or "")
        if expected_pipeline_id and reported_pipeline_id and reported_pipeline_id != expected_pipeline_id:
            raise RuntimeError(f"engine route identity mismatch for {metric}: expected={expected_pipeline_id} actual={reported_pipeline_id}")
        pipeline_id = reported_pipeline_id or expected_pipeline_id
        if not pipeline_id:
            raise RuntimeError(f"engine report for {metric} has no pipeline_id")
        route_entries.append({
            "metric": metric_name,
            "pipeline_id": pipeline_id,
            "input_contract": route.get("input_contract"),
            "reference_mode": mode,
            "nodes": route.get("nodes") or [],
            "engine_report_path": str(metric_dir / "report.json"),
            "pipeline_description_path": str(metric_dir / "pipeline_description.json"),
            "engine_stdout": stdout[-2000:],
        })
        for item in conditions:
            if item["engine_id"] not in values:
                raise RuntimeError(f"engine report for {metric} is missing condition {item['engine_id']}")
            result = {
                "task_id": task_id, "task_version": task_version, "dataset_version": dataset_version, "audio_version": audio_version,
                "sample_id": item["sample_id"], "candidate_key": item["candidate_key"],
                "metric": metric_name, "value": values[item["engine_id"]], "direction": (metric_directions or {}).get(metric_name, direction(metric_name)),
                "reference_mode": mode, "audio_sha256": item["audio_sha256"],
                "engine_commit": identity["engine_commit"], "pipeline_id": pipeline_id,
                "engine_pyproject_sha256": identity["engine_pyproject_sha256"],
                "runtime_lock_sha256": identity["runtime_lock_sha256"],
                "runtime_lock": identity["runtime_lock_sha256"],
                "capability_manifest_sha256": identity["capability_manifest_sha256"], "node_ids": route.get("nodes") or [],
                "capabilities_sha256": identity["capability_manifest_sha256"],
                "device": device,
            }
            if mode == "paired":
                result["reference_audio_sha256"] = item["reference_audio_sha256"]
            result_rows.append(result)
    route_plan = {"schema": "sure.audio_eval.objective_route_plan.v1", "task": task, "task_id": task_id, "task_version": task_version, "metrics": route_entries, "engine": {"root": str(root), "device": device, **identity}}
    return result_rows, route_entries, route_plan


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--engine-commit")
    parser.add_argument("--task-manifest", type=Path)
    parser.add_argument("--metric-plan", type=Path)
    parser.add_argument("--metrics")
    parser.add_argument("--task", default="se")
    parser.add_argument("--dataset-version", default="")
    parser.add_argument("--audio-version", default="")
    parser.add_argument("--device", help="Explicit engine device (for example cpu or cuda)")
    parser.add_argument("--engine-root", type=Path, default=DEFAULT_ENGINE_ROOT)
    parser.add_argument("--runtime-spec", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--capabilities", type=Path, default=DEFAULT_CAPS)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schema": "sure.audio_eval.objective_run.v1", "task_id": args.task_id,
        "metric_rows_path": str(args.output), "metrics": [], "row_count": 0,
        "engine_commit": args.engine_commit or "unresolved", "status": "failed",
        "route_plan_path": None,
        "audio_hash_policy": "joined by task_id × dataset_version × sample_id × candidate_key × audio_sha256",
        "diagnostics": [],
    }
    try:
        rows = load_rows(args.input)
        # Audio paths in objective_source are resolved relative to that source,
        # matching the engine samples_jsonl contract and avoiding cwd drift.
        for row in rows:
            for field in ("enhanced_audio", "audio_path", "prediction_audio", "audio", "path", "file_path", "reference_audio", "reference_audio_path", "reference_path", "reference", "noisy_audio"):
                value = row.get(field)
                if isinstance(value, str) and value and not Path(value).expanduser().is_absolute():
                    source_relative = (args.input.parent / value).resolve()
                    project_relative = (REPO_ROOT / value).resolve()
                    row[field] = str(source_relative if source_relative.exists() else project_relative)
        metric_plan = read_json(args.metric_plan) if args.metric_plan and args.metric_plan.is_file() else None
        planned_directions: dict[str, str] | None = None
        if metric_plan is not None:
            if metric_plan.get("task_id") != args.task_id:
                raise ValueError("metric plan task_id mismatch")
            planned_entries = [item for item in metric_plan.get("metrics", []) if isinstance(item, dict) and item.get("participates_in_model_correlation", True) is not False]
            planned_metrics = [str(item.get("metric")) for item in planned_entries if item.get("metric")]
            planned_directions = {str(item["metric"]): str(item["direction"]) for item in planned_entries if item.get("metric") and item.get("direction")}
            requested_metrics = [item.strip() for item in (args.metrics or "").split(",") if item.strip()]
            if requested_metrics and set(requested_metrics) != set(planned_metrics):
                raise ValueError("requested objective metrics differ from objective_metric_plan.json")
            args.metrics = ",".join(planned_metrics)
        report["metrics"] = [{"metric": str(row.get("metric") or "unresolved"), "route_status": "pending"} for row in rows if row.get("metric")]
        if all("value" in row for row in rows):
            commit = args.engine_commit or str(rows[0].get("engine_commit") or "")
            if not commit:
                raise ValueError("--engine-commit is required for precomputed objective rows")
            if args.engine_commit and any(str(row.get("engine_commit") or commit) != args.engine_commit for row in rows):
                raise ValueError("precomputed objective row engine_commit differs from requested locked commit")
            normalized = normalize_existing(rows, args.task_id, commit)
            args.output.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in normalized), encoding="utf-8")
            report.update({"metrics": [{"metric": metric, "route_status": "precomputed"} for metric in sorted({str(x["metric"]) for x in normalized})], "row_count": len(normalized), "engine_commit": commit, "status": "success"})
        else:
            runtime = read_json(args.runtime_spec)
            identity = locked_identity(args.engine_root.resolve(), args.runtime_spec.resolve(), args.capabilities.resolve())
            if args.engine_commit and args.engine_commit != identity["engine_commit"]:
                raise ValueError(f"requested engine commit differs from locked checkout: requested={args.engine_commit} actual={identity['engine_commit']}")
            context_path = args.report.parent / "evaluation_context.json"
            if context_path.is_file():
                context = read_json(context_path)
                context_commit = str(context.get("engine_commit") or "")
                context_capabilities = str(context.get("capability_manifest_sha256") or "")
                if context_commit and context_commit != identity["engine_commit"]:
                    raise ValueError(f"evaluation context engine commit differs from locked checkout: context={context_commit} actual={identity['engine_commit']}")
                if context_capabilities and context_capabilities != identity["capability_manifest_sha256"]:
                    raise ValueError("evaluation context capability manifest hash differs from locked capability manifest")
            if args.task_manifest is None:
                raise ValueError("--task-manifest is required when input rows contain audio paths")
            manifest = read_json(args.task_manifest)
            task_id = str(manifest.get("task_id") or args.task_id)
            if task_id != args.task_id:
                raise ValueError(f"task manifest task_id mismatch: {task_id} != {args.task_id}")
            declared_samples = {str(item) for item in manifest.get("sample_ids") or []}
            declared_candidates = {str(item) for item in manifest.get("candidate_keys") or []}
            if not declared_samples or not declared_candidates:
                raise ValueError("task manifest must declare non-empty sample_ids and candidate_keys")
            for row in rows:
                if str(row.get("sample_id") or "") not in declared_samples:
                    raise ValueError(f"sample_id is outside task manifest whitelist: {row.get('sample_id')!r}")
                if str(row.get("candidate_key") or "") not in declared_candidates:
                    raise ValueError(f"candidate_key is outside task manifest whitelist: {row.get('candidate_key')!r}")
            observed_conditions = {(str(row.get("sample_id") or ""), str(row.get("candidate_key") or "")) for row in rows}
            expected_conditions = {(sample_id, candidate) for sample_id in declared_samples for candidate in declared_candidates}
            if observed_conditions != expected_conditions:
                missing = sorted(expected_conditions - observed_conditions)
                extra = sorted(observed_conditions - expected_conditions)
                raise ValueError(f"objective source does not cover the task manifest matrix: missing={missing[:8]} extra={extra[:8]}")
            dataset_version = args.dataset_version or str(manifest.get("dataset_version") or "")
            audio_version = args.audio_version or str((manifest.get("audio_policy") or {}).get("audio_version") or "")
            task_version = str(manifest.get("task_version") or "")
            if not task_version or not dataset_version or not audio_version:
                raise ValueError("task manifest must declare task_version, dataset_version and audio_policy.audio_version")
            metrics = list(dict.fromkeys(x.strip() for x in (args.metrics or "").split(",") if x.strip()))
            if not metrics:
                raise ValueError("--metrics is required when input rows contain audio paths")
            device = args.device or os.environ.get("SURE_AUDIO_EVAL_DEVICE", "").strip()
            if not device:
                raise ValueError("--device (or SURE_AUDIO_EVAL_DEVICE) is required; no silent CPU/GPU fallback is allowed")
            scores, routes, route_plan = compute_engine(rows, task_id, task_version, dataset_version, audio_version, metrics, args.task, args.engine_root.resolve(), identity, runtime, args.output, device, planned_directions)
            args.output.write_text("".join(json.dumps(x, sort_keys=True) + "\n" for x in scores), encoding="utf-8")
            route_path = args.output.parent / "objective_route_plan.json"
            route_path.write_text(json.dumps(route_plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            report.update({
                "metrics": routes, "row_count": len(scores), "engine_commit": identity["engine_commit"],
                "route_plan_path": str(route_path), "runtime_lock_sha256": identity["runtime_lock_sha256"],
                "runtime_lock": identity["runtime_lock_sha256"],
                "capability_manifest_sha256": identity["capability_manifest_sha256"],
                "capabilities_sha256": identity["capability_manifest_sha256"],
                "engine_pyproject_sha256": identity["engine_pyproject_sha256"], "status": "success",
            })
    except Exception as exc:
        if not report["metrics"]:
            requested = [item.strip() for item in (args.metrics or "").split(",") if item.strip()]
            if requested:
                report["metrics"] = [{"metric": item, "route_status": "failed"} for item in requested]
        report["diagnostics"] = [{"code": type(exc).__name__, "message": str(exc)}]
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), file=sys.stderr)
        return 1
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Standalone structural checker for /sure_audio_eval artifacts.

The TypeScript hook is the in-process gate used by the agent. This script is
also provided as a deterministic command for CI and for a transferred run.
It deliberately checks contracts and provenance, not whether a correlation
looks good.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import subprocess
from datetime import datetime
from pathlib import Path

from score_checks import check_analysis as check_analysis_scores
from score_checks import check_condition_summary as check_condition_summary_scores
from score_checks import check_subjective_ingest as check_subjective_ingest_scores


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def fail(message: str) -> None:
    raise SystemExit(message)


def check_jsonl(path: Path, expected: int, run_dir: Path, report: dict) -> None:
    if not path.is_file():
        fail(f"missing objective rows: {path}")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != expected or not rows:
        fail(f"objective row count mismatch: {len(rows)} != {expected}")
    task = load(run_dir / "artifacts" / "task_manifest_resolved.json")
    plan = load(run_dir / "artifacts" / "objective_metric_plan.json")
    audio = load(run_dir / "artifacts" / "audio_manifest.json")
    samples = {str(value) for value in task.get("sample_ids", [])}
    candidates = {str(value) for value in task.get("candidate_keys", [])}
    audio_map = {(str(item.get("sample_id")), str(item.get("candidate_key"))): item for item in audio.get("items", []) if isinstance(item, dict)}
    planned = {str(item.get("metric")): item for item in plan.get("metrics", []) if isinstance(item, dict) and item.get("participates_in_model_correlation", True) is not False}
    expected_keys = {(sample, candidate, metric) for sample in samples for candidate in candidates for metric in planned}
    keys = set()
    for row in rows:
        for key in ("task_id", "task_version", "dataset_version", "audio_version", "sample_id", "candidate_key", "metric", "direction", "reference_mode", "pipeline_id", "audio_sha256", "engine_commit", "capability_manifest_sha256"):
            if not isinstance(row.get(key), str) or not row[key]:
                fail(f"objective row missing {key}")
        value = row.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            fail("objective value must be finite")
        if row["task_id"] != task.get("task_id") or row["task_version"] != task.get("task_version") or row["dataset_version"] != task.get("dataset_version") or row["audio_version"] != (task.get("audio_policy") or {}).get("audio_version"):
            fail("objective row task/dataset/audio version mismatch")
        if row["sample_id"] not in samples or row["candidate_key"] not in candidates:
            fail("objective row is outside task whitelist")
        spec = planned.get(row["metric"])
        if spec is None or row["direction"] != spec.get("direction") or row["reference_mode"] != spec.get("reference_mode") or row["pipeline_id"] != spec.get("pipeline_id"):
            fail("objective row differs from metric plan")
        if row["engine_commit"] != report.get("engine_commit") or row["engine_commit"] != plan.get("engine_commit") or row["capability_manifest_sha256"] != plan.get("capability_manifest_sha256"):
            fail("objective row provenance differs from locked plan")
        item = audio_map.get((row["sample_id"], row["candidate_key"]))
        if not item or row["audio_sha256"] != item.get("audio_sha256"):
            fail("objective row audio hash differs from audio manifest")
        if row["reference_mode"] == "paired" and not row.get("reference_audio_sha256"):
            fail("paired objective row needs reference_audio_sha256")
        if row["reference_mode"] == "none" and row.get("reference_audio_sha256") is not None:
            fail("no-reference objective row must not carry a reference hash")
        key = (row["sample_id"], row["candidate_key"], row["metric"])
        if key in keys:
            fail(f"duplicate objective row: {key}")
        keys.add(key)
    if keys != expected_keys:
        fail("objective rows do not cover the task sample × candidate × metric matrix")

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def check_metric_plan(artifact: dict) -> None:
    if artifact.get("status") != "success" or artifact.get("analysis_unit") != "model_condition":
        fail("objective metric plan must be successful and use model_condition")
    if set(artifact.get("excluded_candidates", [])) != {"hidden_reference", "unprocessed"}:
        fail("objective metric plan must exclude hidden_reference and unprocessed")
    model_keys = artifact.get("model_candidate_keys")
    if not isinstance(model_keys, list) or not model_keys:
        fail("objective metric plan must declare model_candidate_keys")
    metrics = artifact.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        fail("objective metric plan has no metrics")
    seen = set()
    for metric in metrics:
        if not isinstance(metric, dict):
            fail("metric plan entry must be an object")
        for field in ("metric", "type", "direction", "reference_mode", "pipeline_id", "input_contract"):
            if not isinstance(metric.get(field), str) or not metric[field]:
                fail(f"metric plan entry missing {field}")
        if metric["type"] not in {"intrusive", "non_intrusive"}:
            fail("metric type must be intrusive or non_intrusive")
        if metric["direction"] not in {"higher_better", "lower_better"}:
            fail("metric direction must be higher_better or lower_better")
        if metric["reference_mode"] not in {"paired", "none"}:
            fail("metric reference_mode must be paired or none")
        if metric["metric"] in seen:
            fail(f"duplicate metric plan entry: {metric['metric']}")
        seen.add(metric["metric"])

def resolve_audio_path(value: str, run_dir: Path) -> Path:
    path = Path(value)
    if path.is_absolute(): return path
    candidate = (run_dir / path).resolve()
    if candidate.is_file(): return candidate
    return (run_dir.parent.parent.parent / path).resolve()


def probe_audio(path: Path) -> dict:
    result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name,sample_rate,channels,duration:format=format_name", "-of", "json", str(path)], capture_output=True, text=True, check=False)
    if result.returncode != 0: fail(f"audio is not decodable: {path}: {result.stderr.strip()}")
    try: data = json.loads(result.stdout)
    except json.JSONDecodeError as exc: fail(f"ffprobe returned invalid JSON for {path}: {exc}")
    stream = (data.get("streams") or [{}])[0]
    return {"codec_name": stream.get("codec_name"), "sample_rate": int(stream["sample_rate"]) if stream.get("sample_rate") else None, "channels": int(stream["channels"]) if stream.get("channels") else None, "duration_seconds": float(stream["duration"]) if stream.get("duration") else None, "format": str((data.get("format") or {}).get("format_name") or "")}


def measure_loudness(path: Path, target: float) -> tuple[float, float]:
    result = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", f"loudnorm=I={target}:TP=-1:print_format=json", "-f", "null", "-"], capture_output=True, text=True, check=False)
    if result.returncode != 0: fail(f"loudness measurement failed for {path}: {result.stderr.strip()}")
    matches = re.findall(r"\{\s*\"input_i\".*?\}", result.stderr, flags=re.S)
    if not matches: fail(f"loudness measurement returned no loudnorm result for {path}")
    try:
        data = json.loads(matches[-1])
        integrated = float(data["input_i"])
        true_peak = float(data["input_tp"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        fail(f"invalid loudnorm result for {path}: {exc}")
    return integrated, true_peak


def check_audio_manifest(artifact: dict, run_dir: Path) -> None:
    if artifact.get("validated") is not True or not isinstance(artifact.get("items"), list) or not artifact["items"]:
        fail("audio manifest must have validated=true and non-empty items")
    task_path = run_dir / "artifacts" / "task_manifest_resolved.json"
    task = load(task_path) if task_path.is_file() else {}
    policy = task.get("audio_policy") if isinstance(task.get("audio_policy"), dict) else {}
    samples = set(map(str, task.get("sample_ids", [])))
    candidates = set(map(str, task.get("candidate_keys", [])))
    seen = set()
    references: dict[str, str] = {}
    for item in artifact["items"]:
        if not isinstance(item, dict):
            fail("audio manifest item must be an object")
        sample, candidate = str(item.get("sample_id", "")), str(item.get("candidate_key", ""))
        if not sample or not candidate or (sample, candidate) in seen:
            fail("audio manifest sample/candidate identity is missing or duplicated")
        if samples and sample not in samples or candidates and candidate not in candidates:
            fail("audio manifest condition is outside task whitelist")
        seen.add((sample, candidate))
        path_value = item.get("path") or item.get("audio_path") or item.get("enhanced_audio")
        if not isinstance(path_value, str) or not path_value:
            fail("audio manifest item requires path")
        path = resolve_audio_path(path_value, run_dir)
        if not path.is_file():
            fail(f"audio path missing: {path_value}")
        if str(item.get("audio_sha256", "")).lower() != sha256(path):
            fail(f"audio SHA-256 mismatch: {sample}/{candidate}")
        info = probe_audio(path)
        expected_format = str(item.get("format") or "").lower().strip()
        if expected_format and expected_format not in info["format"].lower().split(","): fail("audio format differs from manifest")
        if policy.get("sample_rate") is not None and info["sample_rate"] != int(policy["sample_rate"]): fail("audio sample rate differs from manifest policy")
        if policy.get("channels") is not None and info["channels"] != int(policy["channels"]): fail("audio channels differ from manifest policy")
        if policy.get("duration_seconds") is not None and (info["duration_seconds"] is None or not math.isclose(info["duration_seconds"], float(policy["duration_seconds"]), abs_tol=0.05)): fail("audio duration differs from manifest policy")
        lufs_target = policy.get("lufs_target", policy.get("target_lufs", policy.get("integrated_lufs_target")))
        true_peak_max = policy.get("true_peak_max_dbTP", policy.get("true_peak_max"))
        if lufs_target is not None or true_peak_max is not None:
            target = float(lufs_target if lufs_target is not None else -23.0)
            integrated, true_peak = measure_loudness(path, target)
            if lufs_target is not None and (not math.isfinite(integrated) or abs(integrated - float(lufs_target)) > float(policy.get("lufs_tolerance", 1.0))): fail("audio integrated loudness differs from policy")
            if true_peak_max is not None and true_peak > float(true_peak_max) + float(policy.get("true_peak_tolerance", 0.1)): fail("audio true peak exceeds policy")
        reference_value = item.get("reference_path") or item.get("reference_audio")
        if reference_value:
            reference = resolve_audio_path(str(reference_value), run_dir)
            if not reference.is_file() or not item.get("reference_audio_sha256") or sha256(reference) != str(item["reference_audio_sha256"]).lower(): fail("reference audio path or SHA-256 mismatch")
            previous = references.get(sample)
            if previous and previous != item["reference_audio_sha256"]: fail("reference audio differs within a sample")
            references[sample] = str(item["reference_audio_sha256"]).lower()
    expected = {(sample, candidate) for sample in samples for candidate in candidates}
    if seen != expected: fail("audio manifest does not cover the complete task sample × candidate matrix")


def check_subjective_ingest(artifact: dict, run_dir: Path) -> None:
    dedup = Path(str(artifact.get("dedup_path") or ""))
    if not dedup.is_absolute(): dedup = (run_dir / dedup).resolve()
    if not dedup.is_file(): fail("subjective dedup table is missing")
    if not isinstance(artifact.get("raw_sources"), list) or not artifact["raw_sources"]: fail("raw subjective sources are missing")
    with dedup.open(newline="", encoding="utf-8-sig") as handle:
        fields = set(next(csv.reader(handle), []))
    required = {"task_id", "listener_id", "sample_id", "candidate_key", "score", "submitted_at", "group_complete"}
    if not required.issubset(fields): fail("subjective dedup table is missing required fields")


def check_condition_summary(artifact: dict, run_dir: Path) -> None:
    for key in ("mos_path", "ci95_path"):
        path = Path(str(artifact.get(key) or ""))
        if not path.is_absolute(): path = (run_dir / path).resolve()
        if not path.is_file(): fail(f"missing condition summary table: {key}")
    if not isinstance(artifact.get("condition_count"), int) or artifact["condition_count"] < 1: fail("condition_count must be positive")


def check_analysis(artifact: dict, run_dir: Path) -> None:
    if artifact.get("correlation_unit") != "model_condition" or artifact.get("status") != "success": fail("analysis must be a successful model_condition report")
    rows = artifact.get("correlations")
    if not isinstance(rows, list) or not rows: fail("analysis correlations are missing")
    plan = load(run_dir / "artifacts" / "objective_metric_plan.json")
    metrics = {str(item.get("metric")) for item in plan.get("metrics", []) if isinstance(item, dict) and item.get("participates_in_model_correlation", True) is not False}
    for row in rows:
        if not isinstance(row, dict) or row.get("metric") not in metrics or row.get("screening") not in {"before", "after"}: fail("analysis row does not match the metric plan")
        if not isinstance(row.get("n"), int) or row["n"] < 1 or not isinstance(row.get("model_count"), int) or row["model_count"] < 1: fail("analysis row has no common model conditions")
        if "pearson" not in row or "spearman" not in row: fail("analysis row is missing Pearson or Spearman")


def check_final_manifest(path: Path, run_dir: Path) -> None:
    manifest = load(path)
    if manifest.get("schema") != "sure.audio_eval.final_manifest.v1": fail("final manifest schema is invalid")
    entries = manifest.get("artifacts")
    if not isinstance(entries, list) or manifest.get("artifact_count") != len(entries): fail("final manifest artifact_count mismatch")
    required = {"analysis_html", "analysis_report", "evaluation_context", "tool_registry", "task_manifest", "audio_manifest", "subjective_rows", "mos", "ci95", "objective_rows"}
    roles = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("role"), str) or not isinstance(entry.get("path"), str) or not re.fullmatch(r"[a-fA-F0-9]{64}", str(entry.get("sha256", ""))): fail("final manifest artifact entry is invalid")
        if entry["role"] in roles: fail(f"duplicate final manifest role: {entry['role']}")
        roles.add(entry["role"])
        artifact_path = Path(entry["path"])
        if not artifact_path.is_absolute(): artifact_path = (run_dir / artifact_path).resolve()
        if not artifact_path.is_file() or sha256(artifact_path) != str(entry["sha256"]).lower(): fail(f"final manifest hash mismatch: {entry['path']}")
    if not required.issubset(roles): fail(f"final manifest missing roles: {sorted(required - roles)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--produces", type=Path, required=True)
    parser.add_argument("--unit", required=True)
    args = parser.parse_args()
    artifact = load(args.produces)
    if not isinstance(artifact, dict):
        fail("artifact must be a JSON object")
    context_path = args.run_dir / "artifacts" / "evaluation_context.json"
    registry_path = args.run_dir / "artifacts" / "tool_registry.json"
    if not context_path.is_file() or not registry_path.is_file():
        fail("evaluation_context.json and tool_registry.json are required")
    if args.unit == "run_objective":
        if artifact.get("status") == "failed":
            if not isinstance(artifact.get("diagnostics"), list) or not artifact["diagnostics"]:
                fail("failed objective run must retain diagnostics")
            print("run_objective: failed with recorded diagnostics")
            return
        path_value = artifact.get("metric_rows_path")
        if not isinstance(path_value, str):
            fail("metric_rows_path is required")
        rows_path = Path(path_value)
        if not rows_path.is_absolute():
            rows_path = args.run_dir / rows_path
        check_jsonl(rows_path, artifact.get("row_count", -1), args.run_dir, artifact)
    elif args.unit == "resolve_metrics":
        check_metric_plan(artifact)
    elif args.unit == "validate_audio":
        check_audio_manifest(artifact, args.run_dir)
    elif args.unit == "ingest_subjective":
        check_subjective_ingest_scores(artifact, args.run_dir)
    elif args.unit == "aggregate_subjective":
        check_condition_summary_scores(artifact, args.run_dir)
    elif args.unit == "joint_analysis":
        check_analysis_scores(artifact, args.run_dir)
    elif args.unit == "render_report":
        report = artifact.get("analysis_html")
        if not isinstance(report, str):
            fail("analysis_html is required")
        report_path = Path(report)
        if not report_path.is_absolute():
            report_path = args.run_dir / report_path
        if not report_path.is_file():
            fail(f"missing report: {report_path}")
        if artifact.get("report_persisted") is not True:
            fail("report_persisted must be true")
        manifest_value = artifact.get("manifest_path")
        if not isinstance(manifest_value, str): fail("manifest_path is required")
        manifest_path = Path(manifest_value)
        if not manifest_path.is_absolute(): manifest_path = (args.run_dir / manifest_path).resolve()
        check_final_manifest(manifest_path, args.run_dir)
    print(f"{args.unit}: ok")


if __name__ == "__main__":
    main()

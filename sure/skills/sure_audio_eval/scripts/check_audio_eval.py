#!/usr/bin/env python3
"""Standalone structural checker for /sure_audio_eval artifacts.

The TypeScript hook is the in-process gate used by the agent. This script is
also provided as a deterministic command for CI and for a transferred run.
It deliberately checks contracts and provenance, not whether a correlation
looks good.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import wave
import struct
from datetime import datetime
from pathlib import Path


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def fail(message: str) -> None:
    raise SystemExit(message)


def check_jsonl(path: Path, expected: int) -> None:
    if not path.is_file():
        fail(f"missing objective rows: {path}")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != expected or not rows:
        fail(f"objective row count mismatch: {len(rows)} != {expected}")
    keys = set()
    for row in rows:
        for key in ("sample_id", "candidate_key", "metric", "direction", "reference_mode", "engine_commit"):
            if not isinstance(row.get(key), str) or not row[key]:
                fail(f"objective row missing {key}")
        value = row.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            fail("objective value must be finite")
        if row["reference_mode"] == "paired" and not row.get("reference_audio_sha256"):
            fail("paired objective row needs reference_audio_sha256")
        if row["reference_mode"] == "none" and row.get("reference_audio_sha256") is not None:
            fail("no-reference objective row must not carry a reference hash")
        key = (row["sample_id"], row["candidate_key"], row["metric"])
        if key in keys:
            fail(f"duplicate objective row: {key}")
        keys.add(key)

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

def check_audio_manifest(artifact: dict, run_dir: Path) -> None:
    if artifact.get("validated") is not True or not isinstance(artifact.get("items"), list) or not artifact["items"]:
        fail("audio manifest must have validated=true and non-empty items")
    task_path = run_dir / "artifacts" / "task_manifest_resolved.json"
    task = load(task_path) if task_path.is_file() else {}
    policy = task.get("audio_policy") if isinstance(task.get("audio_policy"), dict) else {}
    samples = set(map(str, task.get("sample_ids", [])))
    candidates = set(map(str, task.get("candidate_keys", [])))
    seen = set()
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
        path = Path(path_value)
        if not path.is_absolute():
            path = (run_dir / path).resolve()
            if not path.is_file():
                path = (run_dir.parent.parent.parent / path_value).resolve()
        if not path.is_file():
            fail(f"audio path missing: {path_value}")
        if str(item.get("audio_sha256", "")).lower() != sha256(path):
            fail(f"audio SHA-256 mismatch: {sample}/{candidate}")
        try:
            with wave.open(str(path), "rb") as handle:
                rate, channels, frames = handle.getframerate(), handle.getnchannels(), handle.getnframes()
                duration = frames / rate if rate else 0
                if policy.get("sample_rate") is not None and rate != int(policy["sample_rate"]): fail("audio sample rate differs from manifest policy")
                if policy.get("channels") is not None and channels != int(policy["channels"]): fail("audio channels differ from manifest policy")
                if policy.get("duration_seconds") is not None and not math.isclose(duration, float(policy["duration_seconds"]), abs_tol=0.05): fail("audio duration differs from manifest policy")
        except (wave.Error, EOFError, OSError) as exc:
            fail(f"audio is not decodable WAV: {path}: {exc}")


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
        check_jsonl(rows_path, artifact.get("row_count", -1))
    elif args.unit == "resolve_metrics":
        check_metric_plan(artifact)
    elif args.unit == "validate_audio":
        check_audio_manifest(artifact, args.run_dir)
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
    print(f"{args.unit}: ok")


if __name__ == "__main__":
    main()

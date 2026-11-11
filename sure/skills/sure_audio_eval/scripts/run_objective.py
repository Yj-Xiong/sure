#!/usr/bin/env python3
"""Execute or strictly adapt locked objective metric rows.

This stage never invents values. Audio execution requires a verifiable engine
checkout; absent engine metadata produces a failed diagnostic. Precomputed rows
are accepted only when their route and provenance are explicit.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def rows_from(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".json":
        value = load(path)
        rows = value.get("items") if isinstance(value, dict) else value
    else:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
        raise ValueError("objective source must contain non-empty JSON objects")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--task-manifest", type=Path, required=True)
    parser.add_argument("--metric-plan", type=Path, required=True)
    parser.add_argument("--engine-root", type=Path)
    parser.add_argument("--device")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schema": "sure.audio_eval.objective_run.v1",
        "task_id": args.task_id,
        "metric_rows_path": str(args.output),
        "metrics": [],
        "row_count": 0,
        "engine_commit": "unresolved",
        "status": "failed",
        "route_plan_path": None,
        "diagnostics": [],
    }
    try:
        manifest = load(args.task_manifest)
        plan = load(args.metric_plan)
        if manifest.get("task_id") != args.task_id or plan.get("task_id") != args.task_id:
            raise ValueError("task_id differs between task manifest, metric plan and invocation")
        expected = {(str(s), str(c)) for s in manifest.get("sample_ids", []) for c in manifest.get("candidate_keys", [])}
        planned = {str(item.get("metric")): item for item in plan.get("metrics", []) if isinstance(item, dict)}
        source_rows = rows_from(args.input)
        if not all("value" in row for row in source_rows):
            engine_root = args.engine_root
            if engine_root is None or not engine_root.is_dir():
                raise RuntimeError("locked SURE evaluation engine checkout is unavailable")
            if not (engine_root / ".git").exists():
                raise RuntimeError("locked SURE evaluation engine has no git metadata; engine commit cannot be verified")
            if not args.device:
                raise RuntimeError("--device is required; no silent CPU/GPU fallback")
            raise RuntimeError("engine execution bridge is unavailable in this checkout; no objective score was generated")
        output_rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for row in source_rows:
            sample, candidate, metric = str(row.get("sample_id", "")), str(row.get("candidate_key", "")), str(row.get("metric", ""))
            key = (sample, candidate, metric)
            if (sample, candidate) not in expected or metric not in planned:
                raise ValueError(f"objective row is outside task/metric plan: {key}")
            if key in seen:
                raise ValueError(f"duplicate objective row: {key}")
            seen.add(key)
            value = row.get("value")
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                raise ValueError(f"objective value is not finite: {key}")
            for field in ("audio_sha256", "pipeline_id", "engine_commit", "capability_manifest_sha256"):
                if not isinstance(row.get(field), str) or not row[field]:
                    raise ValueError(f"objective row missing {field}: {key}")
            mode = str(row.get("reference_mode"))
            if mode != planned[metric].get("reference_mode"):
                raise ValueError(f"reference_mode disagrees with metric plan: {key}")
            if str(row.get("pipeline_id")) != str(planned[metric].get("pipeline_id")):
                raise ValueError(f"pipeline_id disagrees with metric plan: {key}")
            expected_direction = str(planned[metric].get("direction"))
            if expected_direction and str(row.get("direction")) not in {expected_direction, expected_direction.replace("_better", "_is_better")}:
                raise ValueError(f"direction disagrees with metric plan: {key}")
            expected_capability = str(plan.get("capability_manifest_sha256") or "")
            if expected_capability and str(row.get("capability_manifest_sha256")) != expected_capability:
                raise ValueError(f"capability manifest hash disagrees with metric plan: {key}")
            if mode == "paired" and not isinstance(row.get("reference_audio_sha256"), str):
                raise ValueError(f"paired row missing reference_audio_sha256: {key}")
            if mode == "none" and row.get("reference_audio_sha256") is not None:
                raise ValueError(f"no-reference row carries reference hash: {key}")
            output_rows.append(dict(row))
        if not output_rows:
            raise ValueError("objective source has no rows")
        args.output.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in output_rows), encoding="utf-8")
        report.update({
            "metrics": [{"metric": metric, "route_status": "precomputed"} for metric in sorted({row["metric"] for row in output_rows})],
            "row_count": len(output_rows),
            "engine_commit": sorted({row["engine_commit"] for row in output_rows})[0],
            "status": "success",
            "audio_hash_policy": "source row SHA-256 is required",
        })
    except Exception as exc:
        report["diagnostics"] = [{"code": type(exc).__name__, "message": str(exc)}]
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return 1
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

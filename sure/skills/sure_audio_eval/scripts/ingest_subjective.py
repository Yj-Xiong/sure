#!/usr/bin/env python3
"""Validate and deterministically ingest subjective listening submissions.

Raw source files are never modified.  Only rows that satisfy the task
manifest contract participate in latest-valid de-duplication; rejected rows
are retained in a sidecar CSV with an explicit reason.  A listening group is
``listener x sample`` and is complete only when every manifest candidate has a
valid score.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

REQUIRED = (
    "task_id", "task_version", "dataset_version", "audio_version", "listener_id",
    "session_id", "device_type", "round", "submission_kind", "sample_id",
    "candidate_key", "candidate_position", "score", "scale_max", "submitted_at",
    "audio_sha256",
)
HEX64 = re.compile(r"^[0-9a-f]{64}$", re.I)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"invalid task manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit("task manifest must be a JSON object")
    return value


def as_nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def parse_timestamp(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Naive timestamps are ambiguous across machines and are therefore not a
    # valid provenance timestamp.  Keep the original string in rejected rows.
    return parsed if parsed.tzinfo is not None else None


def normalize_listener(value: str) -> str:
    # Whitespace is presentation noise. Listener IDs remain case-sensitive so
    # explicitly distinct IDs do not silently collapse into one participant.
    return " ".join(str(value).strip().split())


def manifest_candidates(manifest: dict[str, Any]) -> list[str]:
    values = manifest.get("candidate_keys")
    if isinstance(values, list):
        return [str(v) for v in values if as_nonempty(v)]
    # Some task manifests use candidate objects while retaining the same
    # whitelist semantics.
    values = manifest.get("candidates")
    if isinstance(values, list):
        out: list[str] = []
        for item in values:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and as_nonempty(item.get("candidate_key")):
                out.append(str(item["candidate_key"]))
        return out
    return []


def manifest_audio_hashes(manifest: dict[str, Any]) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for key in ("audio_manifest", "audio", "audio_items", "items"):
        values = manifest.get(key)
        if isinstance(values, dict):
            values = values.get("items", [])
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            sample, candidate, digest = item.get("sample_id"), item.get("candidate_key"), item.get("audio_sha256")
            if as_nonempty(sample) and as_nonempty(candidate) and isinstance(digest, str) and HEX64.fullmatch(digest):
                out[(str(sample), str(candidate))] = digest.lower()
    return out


def expected_scale(manifest: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
    scale = manifest.get("scale")
    if not isinstance(scale, dict):
        return None, None, None
    def num(key: str) -> float | None:
        value = scale.get(key)
        return float(value) if isinstance(value, (int, float)) and math.isfinite(float(value)) else None
    return num("min"), num("max"), num("step")


def row_error(row: dict[str, str], *, task_id: str, manifest: dict[str, Any], candidates: list[str], hashes: dict[tuple[str, str], str], task_version: str | None, dataset_version: str | None, audio_version: str | None) -> str | None:
    for field in REQUIRED:
        if not as_nonempty(row.get(field)):
            return f"missing {field}"
    if row.get("task_id") != task_id:
        return "task_id mismatch"
    if task_version and row.get("task_version") != task_version:
        return "task_version mismatch"
    expected_dataset = dataset_version or (str(manifest.get("dataset_version")) if manifest.get("dataset_version") is not None else None)
    if expected_dataset and row.get("dataset_version") != expected_dataset:
        return "dataset_version mismatch"
    expected_audio = audio_version
    policy = manifest.get("audio_policy")
    if expected_audio is None and isinstance(policy, dict) and policy.get("audio_version") is not None:
        expected_audio = str(policy["audio_version"])
    if expected_audio and row.get("audio_version") != expected_audio:
        return "audio_version mismatch"
    sample = row.get("sample_id", "")
    if isinstance(manifest.get("sample_ids"), list) and sample not in [str(v) for v in manifest["sample_ids"]]:
        return "sample_id not in manifest whitelist"
    candidate = row.get("candidate_key", "")
    if candidates and candidate not in candidates:
        return "candidate_key not in manifest whitelist"
    try:
        position = int(row.get("candidate_position", ""))
    except ValueError:
        return "candidate_position must be an integer"
    if candidates and (position < 1 or position > len(candidates) or candidates[position - 1] != candidate):
        return "candidate_position does not match manifest candidate order"
    try:
        score = float(row.get("score", ""))
        scale_max = float(row.get("scale_max", ""))
    except ValueError:
        return "score and scale_max must be numeric"
    if not math.isfinite(score) or not math.isfinite(scale_max):
        return "score and scale_max must be finite"
    lo, hi, step = expected_scale(manifest)
    if hi is not None and not math.isclose(scale_max, hi, rel_tol=0.0, abs_tol=1e-9):
        return "scale_max mismatch"
    if lo is not None and score < lo - 1e-9:
        return "score below scale minimum"
    if hi is not None and score > hi + 1e-9:
        return "score above scale maximum"
    if step is not None and step > 0 and lo is not None:
        units = (score - lo) / step
        if not math.isclose(units, round(units), rel_tol=0.0, abs_tol=1e-7):
            return "score does not follow manifest scale step"
    if not parse_timestamp(row.get("submitted_at", "")):
        return "submitted_at must be an ISO-8601 timestamp with timezone"
    digest = row.get("audio_sha256", "").lower()
    if not HEX64.fullmatch(digest):
        return "audio_sha256 must be a 64-character SHA-256"
    expected_hash = hashes.get((sample, candidate))
    if expected_hash and digest != expected_hash:
        return "audio_sha256 mismatch"
    if not as_nonempty(row.get("listener_id")):
        return "listener_id is empty"
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", nargs="+", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--audio-manifest", type=Path, help="validated audio_manifest.json used to verify condition hashes")
    parser.add_argument("--invalid-output", type=Path)
    parser.add_argument("--task-version")
    parser.add_argument("--dataset-version")
    parser.add_argument("--audio-version")
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    candidates = manifest_candidates(manifest)
    hashes = manifest_audio_hashes(manifest)
    if args.audio_manifest:
        hashes.update(manifest_audio_hashes(load_manifest(args.audio_manifest)))
    expected_task_version = args.task_version or (str(manifest["task_version"]) if manifest.get("task_version") is not None else None)
    sources: list[dict[str, Any]] = []
    valid: list[dict[str, str]] = []
    invalid: list[dict[str, str]] = []
    source_order = 0
    observed_versions: tuple[str, str, str] | None = None
    for source in args.input:
        count = 0
        with source.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise SystemExit(f"subjective source has no header: {source}")
            for row_number, raw in enumerate(reader, 2):
                count += 1
                row = {str(k): (str(v).strip() if v is not None else "") for k, v in raw.items()}
                error = row_error(row, task_id=args.task_id, manifest=manifest, candidates=candidates, hashes=hashes, task_version=expected_task_version, dataset_version=args.dataset_version, audio_version=args.audio_version)
                if error is None:
                    versions = (row.get("task_version", ""), row.get("dataset_version", ""), row.get("audio_version", ""))
                    if observed_versions is None:
                        observed_versions = versions
                    elif versions != observed_versions:
                        error = "task/dataset/audio version differs from other source rows"
                if error:
                    invalid.append({**row, "source": str(source), "source_row": str(row_number), "rejection_reason": error})
                else:
                    row["listener_id"] = normalize_listener(row["listener_id"])
                    row["_source"] = str(source)
                    row["_source_row"] = str(row_number)
                    row["_order"] = str(source_order)
                    valid.append(row)
                source_order += 1
        sources.append({"path": str(source), "sha256": sha256(source), "rows": count})
    # Latest-valid is timestamp order with source order as a deterministic tie
    # breaker.  Session and round stay in the winning row as provenance.
    latest: dict[tuple[str, str, str, str], dict[str, str]] = {}
    for row in valid:
        key = (row["task_id"], row["listener_id"], row["sample_id"], row["candidate_key"])
        previous = latest.get(key)
        stamp = parse_timestamp(row["submitted_at"])
        prev_stamp = parse_timestamp(previous["submitted_at"]) if previous else None
        if previous is None or (stamp and prev_stamp and stamp > prev_stamp) or (stamp == prev_stamp and int(row["_order"]) > int(previous["_order"])):
            latest[key] = row
    # Mark complete listener/sample groups.  Incomplete groups remain visible in
    # the deduplicated audit table but are excluded by aggregate_subjective.py.
    by_group: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for row in latest.values():
        by_group.setdefault((row["task_id"], row["listener_id"], row["sample_id"]), []).append(row)
    declared = set(candidates)
    for row in latest.values():
        key = (row["task_id"], row["listener_id"], row["sample_id"])
        present = {entry["candidate_key"] for entry in by_group[key]}
        row["group_complete"] = "true" if declared and present == declared else "false"
        for hidden in ("_source", "_source_row", "_order"):
            row.pop(hidden, None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fields = list(REQUIRED) + ["group_complete"]
    extras = sorted({k for row in latest.values() for k in row if k not in fields})
    fields.extend(extras)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted(latest.values(), key=lambda r: (r.get("sample_id", ""), r.get("candidate_key", ""), r.get("listener_id", ""))))
    invalid_path = args.invalid_output or args.output.with_name(args.output.stem + ".invalid.csv")
    with invalid_path.open("w", newline="", encoding="utf-8") as handle:
        invalid_fields = sorted({k for row in invalid for k in row}) or ["rejection_reason"]
        writer = csv.DictWriter(handle, fieldnames=invalid_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(invalid)
    complete_groups = sum(1 for rows in by_group.values() if declared and {r["candidate_key"] for r in rows} == declared)
    report = {
        "schema": "sure.audio_eval.subjective_ingest.v1", "task_id": args.task_id,
        "task_version": expected_task_version or (observed_versions[0] if observed_versions else None), "dataset_version": args.dataset_version or manifest.get("dataset_version") or (observed_versions[1] if observed_versions else None),
        "audio_version": args.audio_version or (manifest.get("audio_policy", {}).get("audio_version") if isinstance(manifest.get("audio_policy"), dict) else None) or (observed_versions[2] if observed_versions else None),
        "raw_sources": sources, "dedup_path": str(args.output), "invalid_rows_path": str(invalid_path),
        "latest_valid_rule": "latest valid submitted_at per task_id × normalized_listener_id × sample_id × candidate_key",
        "complete_group_rule": "listener_id × sample_id contains every manifest candidate exactly once",
        "complete_groups": complete_groups, "valid_rows": len(valid), "dedup_rows": len(latest),
        "status": "failed" if not latest else ("success" if not invalid else "success_with_rejections"), "invalid_rows": len(invalid),
        "duplicate_rows": len(valid) - len(latest), "manifest_path": str(args.manifest) if args.manifest else None,
        "audio_manifest_path": str(args.audio_manifest) if args.audio_manifest else None,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

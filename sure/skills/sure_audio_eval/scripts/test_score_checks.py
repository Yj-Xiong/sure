#!/usr/bin/env python3
"""Regression tests for independent subjective score gates."""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from score_checks import check_condition_summary, check_subjective_ingest


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ScoreChecksTest(unittest.TestCase):
    def _fixture(self) -> tuple[Path, dict, Path]:
        directory = Path(tempfile.mkdtemp())
        artifacts = directory / "artifacts"
        artifacts.mkdir()
        task = {
            "task_id": "t1", "task_version": "v1", "dataset_version": "d1",
            "sample_ids": ["s1"], "candidate_keys": ["m1", "m2"],
            "analysis_candidate_keys": ["m1", "m2"],
            "candidate_roles": {"m1": "model", "m2": "model"},
            "scale": {"min": 0, "max": 100, "step": 1},
            "audio_policy": {"audio_version": "a1"},
        }
        (artifacts / "task_manifest_resolved.json").write_text(json.dumps(task), encoding="utf-8")
        audio_items = [{"sample_id": "s1", "candidate_key": candidate, "audio_sha256": "a" * 64} for candidate in ("m1", "m2")]
        (artifacts / "audio_manifest.json").write_text(json.dumps({"items": audio_items}), encoding="utf-8")
        raw = directory / "raw.csv"
        fields = ["task_id", "task_version", "dataset_version", "audio_version", "listener_id", "session_id", "device_type", "round", "submission_kind", "sample_id", "candidate_key", "candidate_position", "score", "scale_max", "submitted_at", "audio_sha256"]
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = []
        for listener, base in (("Alice", 40), ("alice", 60)):
            for candidate, position, score in (("m1", 1, base), ("m2", 2, base + 10)):
                rows.append({"task_id": "t1", "task_version": "v1", "dataset_version": "d1", "audio_version": "a1", "listener_id": listener, "session_id": "s", "device_type": "headphones", "round": "1", "submission_kind": "complete", "sample_id": "s1", "candidate_key": candidate, "candidate_position": str(position), "score": str(score), "scale_max": "100", "submitted_at": (start + timedelta(seconds=len(rows))).isoformat(), "audio_sha256": "a" * 64})
        rows.append({**rows[0], "score": "101", "submitted_at": (start + timedelta(seconds=30)).isoformat()})
        with raw.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        report_path = artifacts / "subjective_ingest_report.json"
        subprocess.run([sys.executable, str(ROOT / "ingest_subjective.py"), "--input", str(raw), "--output", str(directory / "dedup.csv"), "--report", str(report_path), "--task-id", "t1", "--manifest", str(artifacts / "task_manifest_resolved.json"), "--audio-manifest", str(artifacts / "audio_manifest.json"), "--invalid-output", str(directory / "invalid.csv")], check=True)
        return directory, json.loads(report_path.read_text(encoding="utf-8")), raw

    def test_ingest_gate_preserves_rejections_and_case_sensitive_ids(self) -> None:
        directory, report, _ = self._fixture()
        check_subjective_ingest(report, directory)
        with (directory / "dedup.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual({row["listener_id"] for row in rows}, {"Alice", "alice"})
        self.assertEqual(report["status"], "success_with_rejections")
        self.assertEqual(report["invalid_rows"], 1)

    def test_condition_gate_rejects_stale_mos(self) -> None:
        directory, report, _ = self._fixture()
        check_subjective_ingest(report, directory)
        mos = directory / "mos.csv"
        ci = directory / "ci.csv"
        subprocess.run([sys.executable, str(ROOT / "aggregate_subjective.py"), "--input", str(directory / "dedup.csv"), "--mos", str(mos), "--ci95", str(ci), "--report", str(directory / "artifacts" / "condition_summary.json"), "--manifest", str(directory / "artifacts" / "task_manifest_resolved.json"), "--task-id", "t1"], check=True)
        summary = json.loads((directory / "artifacts" / "condition_summary.json").read_text(encoding="utf-8"))
        check_condition_summary(summary, directory)
        with mos.open(newline="", encoding="utf-8") as handle:
            mos_rows = list(csv.DictReader(handle))
        mos_rows[0]["mos"] = str(float(mos_rows[0]["mos"]) + 1.0)
        with mos.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=mos_rows[0].keys())
            writer.writeheader()
            writer.writerows(mos_rows)
        with self.assertRaises(ValueError):
            check_condition_summary(summary, directory)


if __name__ == "__main__":
    unittest.main()

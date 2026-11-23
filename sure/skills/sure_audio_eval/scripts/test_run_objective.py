#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "run_objective.py"


class RunObjectiveContractTest(unittest.TestCase):
    def test_precomputed_rows_must_match_metric_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            manifest = run / "task.json"
            manifest.write_text(json.dumps({
                "task_id": "t1",
                "task_version": "v1",
                "dataset_version": "d1",
                "sample_ids": ["s1"],
                "candidate_keys": ["model_a"],
                "audio_policy": {"audio_version": "a1"},
            }), encoding="utf-8")
            plan = run / "plan.json"
            plan.write_text(json.dumps({
                "task_id": "t1",
                "metrics": [{
                    "metric": "allowed",
                    "direction": "higher_better",
                    "reference_mode": "none",
                    "pipeline_id": "se.any.allowed.v1",
                    "participates_in_model_correlation": True,
                }],
                "capability_manifest_sha256": "a" * 64,
            }), encoding="utf-8")
            (run / "audio_manifest.json").write_text(json.dumps({
                "items": [{"sample_id": "s1", "candidate_key": "model_a", "audio_sha256": "b" * 64}],
            }), encoding="utf-8")
            source = run / "objective.jsonl"
            source.write_text(json.dumps({
                "task_id": "t1", "task_version": "v1", "dataset_version": "d1", "audio_version": "a1",
                "sample_id": "s1", "candidate_key": "model_a", "metric": "wrong", "value": 1.0,
                "direction": "higher_better", "reference_mode": "none", "pipeline_id": "se.any.allowed.v1",
                "audio_sha256": "b" * 64, "engine_commit": "e", "capability_manifest_sha256": "a" * 64,
            }) + "\n", encoding="utf-8")
            output = run / "out.jsonl"
            report = run / "report.json"
            result = subprocess.run([
                sys.executable, str(SCRIPT), "--input", str(source), "--output", str(output), "--report", str(report),
                "--task-id", "t1", "--task-manifest", str(manifest), "--metric-plan", str(plan), "--engine-commit", "e",
            ], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("absent from metric plan", report.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Regression tests for the standalone report renderer."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "render_report.py"


class RenderReportTest(unittest.TestCase):
    def test_model_condition_sections_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            analysis = run / "analysis.json"
            analysis.write_text(json.dumps({
                "schema": "sure.audio_eval.analysis.v1",
                "task_id": "t1",
                "status": "success",
                "correlation_unit": "model_condition",
                "correlations": [{"metric": "nisqa", "screening": "before", "n": 4, "model_count": 2, "pearson": 0.9, "spearman": 1.0, "metric_type": "non_intrusive", "direction": "higher_better", "reference_mode": "none"}],
                "screening": {"before": {"condition_count": 4}, "after": {"condition_count": 4}, "rule_version": "v1"},
                "provenance": {"source_files": [{"path": "mos.csv", "kind": "mos", "sha256": "a" * 64}]},
            }), encoding="utf-8")
            task = run / "task.json"
            task.write_text(json.dumps({"task_id": "t1", "task_version": "v1", "dataset_version": "d1", "scale": {"min": 0, "max": 100}}), encoding="utf-8")
            output, manifest, report = run / "analysis.html", run / "final.json", run / "run.json"
            subprocess.run([sys.executable, str(SCRIPT), "--analysis", str(analysis), "--output", str(output), "--task-id", "t1", "--task-manifest", str(task), "--manifest", str(manifest), "--report", str(report), "--created-at", "2026-01-01T00:00:00Z"], check=True)
            text = output.read_text(encoding="utf-8")
            self.assertIn("Objective metric plan", text)
            self.assertIn("Model-condition MOS-to-metric correlation", text)
            self.assertIn("Screening before and after", text)
            self.assertIn("Source SHA-256", text)
            final = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(final["schema"], "sure.audio_eval.final_manifest.v1")
            self.assertEqual(final["created_at"], "2026-01-01T00:00:00Z")
            self.assertTrue(all(len(entry["sha256"]) == 64 for entry in final["artifacts"]))
            run_report = json.loads(report.read_text(encoding="utf-8"))
            self.assertTrue(run_report["report_persisted"])


if __name__ == "__main__":
    unittest.main()

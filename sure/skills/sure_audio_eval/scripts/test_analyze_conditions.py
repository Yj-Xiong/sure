#!/usr/bin/env python3
"""Regression tests for model-condition MOS-to-metric analysis."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "analyze_conditions.py"


class AnalyzeConditionsTest(unittest.TestCase):
    def test_model_only_before_after_per_metric(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            manifest = run / "task.json"
            manifest.write_text(json.dumps({
                "task_id": "t1",
                "task_version": "v1",
                "dataset_version": "d1",
                "candidate_keys": ["hidden_reference", "unprocessed", "model_a", "model_b"],
                "candidate_roles": {"hidden_reference": "hidden_reference", "unprocessed": "unprocessed", "model_a": "model", "model_b": "model"},
                "excluded_candidates_from_correlation": ["hidden_reference", "unprocessed"],
            }), encoding="utf-8")
            plan = run / "plan.json"
            plan.write_text(json.dumps({"metrics": [{"metric": "stoi", "type": "intrusive", "direction": "higher_better", "reference_mode": "paired"}, {"metric": "nisqa", "type": "non_intrusive", "direction": "higher_better", "reference_mode": "none"}]}), encoding="utf-8")
            mos = run / "mos.csv"
            with mos.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["sample_id", "candidate_key", "n", "mos", "sd"])
                writer.writeheader()
                for sample, a, b in (("s1", 40, 60), ("s2", 50, 70)):
                    for candidate, score in (("hidden_reference", 90), ("unprocessed", 20), ("model_a", a), ("model_b", b)):
                        writer.writerow({"sample_id": sample, "candidate_key": candidate, "n": 2, "mos": score, "sd": 1})
            objective = run / "objective.jsonl"
            rows = []
            for sample, values in (("s1", (4, 0.4)), ("s2", (5, 0.5))):
                for candidate, index in (("hidden_reference", 9), ("unprocessed", 1), ("model_a", values[0]), ("model_b", values[0] + 1)):
                    rows.extend([{"task_id": "t1", "sample_id": sample, "candidate_key": candidate, "metric": "stoi", "value": index, "direction": "higher_better", "reference_mode": "paired", "engine_commit": "e"}, {"task_id": "t1", "sample_id": sample, "candidate_key": candidate, "metric": "nisqa", "value": index / 10, "direction": "higher_better", "reference_mode": "none", "engine_commit": "e"}])
            objective.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            output = run / "analysis.json"
            subprocess.run([sys.executable, str(SCRIPT), "--mos", str(mos), "--objective", str(objective), "--output", str(output), "--task-id", "t1", "--manifest", str(manifest), "--metric-plan", str(plan)], check=True)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["correlation_unit"], "model_condition")
            self.assertEqual(report["model_candidates"], ["model_a", "model_b"])
            self.assertEqual(len(report["correlations"]), 4)
            self.assertEqual({row["screening"] for row in report["correlations"]}, {"before", "after"})
            self.assertTrue(all(row["n"] == 4 and row["model_count"] == 2 for row in report["correlations"]))
            self.assertTrue(all(row["reference_mode"] in {"paired", "none"} for row in report["correlations"]))
            self.assertNotIn("hidden_reference", json.dumps(report["correlations"]))
            self.assertNotIn("unprocessed", json.dumps(report["correlations"]))

    def test_requires_metric_plan_and_declared_models(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            mos = run / "mos.csv"
            mos.write_text("sample_id,candidate_key,n,mos,sd\ns1,model_a,2,50,1\n", encoding="utf-8")
            objective = run / "objective.jsonl"
            objective.write_text(json.dumps({"task_id": "t1", "sample_id": "s1", "candidate_key": "model_a", "metric": "m", "value": 1, "direction": "higher_better", "reference_mode": "none"}) + "\n", encoding="utf-8")
            with self.assertRaises(subprocess.CalledProcessError):
                subprocess.run([sys.executable, str(SCRIPT), "--mos", str(mos), "--objective", str(objective), "--output", str(run / "analysis.json"), "--task-id", "t1"], check=True, stderr=subprocess.PIPE, text=True)


if __name__ == "__main__":
    unittest.main()

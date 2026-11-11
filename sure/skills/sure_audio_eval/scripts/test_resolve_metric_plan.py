#!/usr/bin/env python3
"""Focused contract tests for the deterministic metric planner."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from resolve_metric_plan import PlanError, build_plan


ROOT = Path(__file__).resolve().parents[4]
PACKAGE = ROOT / "sure" / "skills" / "sure_audio_eval"


class ResolveMetricPlanTest(unittest.TestCase):
    def args(self, manifest: Path, metrics: str) -> Namespace:
        return Namespace(
            task_manifest=manifest,
            capabilities=ROOT / "sure" / "runtime" / "evaluation" / "engine-capabilities.generated.json",
            runtime=ROOT / "sure" / "runtime" / "evaluation" / "runtime.json",
            output=Path("/tmp/metric-plan-test.json"),
            metrics=metrics,
            task_id="speech-quality-example-v1",
            engine_task=None,
            standards=PACKAGE / "references" / "STANDARDS.md",
        )

    def test_resolves_exact_locked_routes(self) -> None:
        manifest = PACKAGE / "examples" / "task_manifest.json"
        result = build_plan(self.args(manifest, "pesq,dnsmos"))
        self.assertEqual(result["engine_task"], "se")
        self.assertEqual([row["pipeline_id"] for row in result["metrics"]], ["se.any.pesq.pesq_v1", "se.any.dnsmos.dnsmos_v1"])
        self.assertRegex(result["capability_manifest_sha256"], r"^[a-f0-9]{64}$")

    def test_rejects_metric_absent_from_locked_capabilities(self) -> None:
        manifest = PACKAGE / "examples" / "task_manifest.json"
        with self.assertRaisesRegex(PlanError, "no route"):
            build_plan(self.args(manifest, "nisqa_mos"))

    def test_rejects_missing_direction_in_manifest_and_route(self) -> None:
        source = PACKAGE / "examples" / "task_manifest.json"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            payload = json.loads(source.read_text(encoding="utf-8"))
            payload["metric_policies"]["pesq"].pop("direction")
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(PlanError, "no explicit direction"):
                build_plan(self.args(path, "pesq"))


if __name__ == "__main__":
    unittest.main()

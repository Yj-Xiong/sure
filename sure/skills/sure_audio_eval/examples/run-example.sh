#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../../../" && pwd)
OUTPUT_DIR=${1:-"$ROOT/.sure/examples/audio-eval-plan"}
PACKAGE="$ROOT/sure/skills/sure_audio_eval"

mkdir -p "$OUTPUT_DIR"
python3 "$PACKAGE/scripts/resolve_metric_plan.py" \
  --task-manifest "$PACKAGE/examples/task_manifest.json" \
  --capabilities "$ROOT/sure/runtime/evaluation/engine-capabilities.generated.json" \
  --runtime "$ROOT/sure/runtime/evaluation/runtime.json" \
  --output "$OUTPUT_DIR/objective_metric_plan.json" \
  --metrics pesq,dnsmos \
  --task-id speech-quality-example-v1 \
  --engine-task se \
  --standards "$PACKAGE/references/STANDARDS.md"

printf 'objective metric plan: %s\n' "$OUTPUT_DIR/objective_metric_plan.json"

#!/usr/bin/env python3
"""Render a standalone HTML report from validated audio-evaluation artifacts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def load_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def records(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.is_file():
        return []
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(parsed, dict):
        return [parsed]
    return [value for value in parsed if isinstance(value, dict)] if isinstance(parsed, list) else []


def cell(value: Any) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return html.escape(str(value if value is not None else ""))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest_entry(path: Path, role: str, base: Path) -> dict[str, Any]:
    try:
        shown = path.relative_to(base).as_posix()
    except ValueError:
        shown = str(path)
    entry: dict[str, Any] = {"role": role, "path": shown, "sha256": file_sha256(path)}
    if path.suffix.lower() in {".csv", ".jsonl", ".ndjson"}:
        entry["row_count"] = len(records(path))
    try:
        parsed = load_json(path)
        if isinstance(parsed.get("schema"), str):
            entry["schema"] = parsed["schema"]
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return entry


def table(rows: Iterable[dict[str, Any]], columns: list[tuple[str, str]], empty: str = "No records supplied.") -> str:
    materialized = list(rows)
    if not materialized:
        return f"<p class='muted'>{html.escape(empty)}</p>"
    head = "".join(f"<th>{html.escape(label)}</th>" for label, _ in columns)
    body = "\n".join("<tr>" + "".join(f"<td>{cell(row.get(key))}</td>" for _, key in columns) + "</tr>" for row in materialized)
    return f"<div class='table-wrap'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def metadata_rows(task_id: str, analysis: dict[str, Any], manifest: dict[str, Any], audio: dict[str, Any], context: dict[str, Any]) -> list[dict[str, Any]]:
    provenance = analysis.get("provenance") if isinstance(analysis.get("provenance"), dict) else {}
    return [
        {"field": "Task", "value": task_id},
        {"field": "Task version", "value": manifest.get("task_version", analysis.get("task_version", ""))},
        {"field": "Dataset version", "value": manifest.get("dataset_version", analysis.get("dataset_version", ""))},
        {"field": "Audio version", "value": audio.get("audio_version", analysis.get("audio_version", ""))},
        {"field": "Scale", "value": manifest.get("scale", "")},
        {"field": "Engine commit", "value": provenance.get("engine_commit", context.get("engine_commit", ""))},
        {"field": "Runtime lock", "value": provenance.get("runtime_lock", context.get("runtime_lock_sha256", context.get("runtime_lock", "")))},
        {"field": "Capability manifest SHA-256", "value": provenance.get("capability_manifest_sha256", context.get("capability_manifest_sha256", ""))},
        {"field": "Run status", "value": analysis.get("status", "")},
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--task-manifest", dest="task_manifest", type=Path)
    parser.add_argument("--metric-plan", type=Path)
    parser.add_argument("--context", type=Path)
    parser.add_argument("--audio-manifest", type=Path)
    parser.add_argument("--subjective", type=Path)
    parser.add_argument("--mos", type=Path)
    parser.add_argument("--ci95", type=Path)
    parser.add_argument("--objective", type=Path)
    parser.add_argument("--diagnostics", type=Path)
    parser.add_argument("--manifest", dest="artifact_manifest", type=Path, help="Write a content-addressed final artifact manifest.")
    parser.add_argument("--report", type=Path, help="Write the run report after the final manifest.")
    parser.add_argument("--created-at", help="Deterministic UTC timestamp for the final manifest.")
    args = parser.parse_args()

    analysis = load_json(args.analysis)
    manifest = load_json(args.task_manifest)
    metric_plan = load_json(args.metric_plan)
    context = load_json(args.context)
    audio = load_json(args.audio_manifest)
    task_id = str(manifest.get("task_id", args.task_id))
    metric_rows = analysis.get("metric_plan") if isinstance(analysis.get("metric_plan"), list) else metric_plan.get("metrics", [])
    correlations = analysis.get("correlations") if isinstance(analysis.get("correlations"), list) else []
    correlation_rows = [row for row in correlations if isinstance(row, dict)]
    correlation_rows.sort(key=lambda row: (str(row.get("screening", "")), -(abs(float(row["pearson"])) if isinstance(row.get("pearson"), (int, float)) else -1.0), str(row.get("metric", ""))))
    for index, row in enumerate(correlation_rows, 1):
        row.setdefault("rank", index)
    audio_items = audio.get("items", []) if isinstance(audio.get("items"), list) else []
    provenance = analysis.get("provenance") if isinstance(analysis.get("provenance"), dict) else {}
    source_files = provenance.get("source_files", []) if isinstance(provenance.get("source_files"), list) else []
    screening = analysis.get("screening", {})

    style = "body{font:15px system-ui,-apple-system,sans-serif;max-width:1200px;margin:2rem auto;padding:0 1rem;color:#18212b;line-height:1.45}h1{margin-bottom:.25rem}h2{margin-top:2rem;border-bottom:1px solid #d8dee6;padding-bottom:.25rem}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{border:1px solid #d8dee6;padding:.4rem .5rem;text-align:left;vertical-align:top}th{background:#edf2f7}tr:nth-child(even){background:#fafbfc}.table-wrap{overflow-x:auto}.muted{color:#5b6773}pre{white-space:pre-wrap;background:#f6f8fa;border:1px solid #d8dee6;border-radius:4px;padding:.75rem;overflow:auto;font-size:.84rem}.status{padding:.65rem;border-radius:4px;background:#f6f8fa;border:1px solid #d8dee6}.failure{background:#fff1f0;border-color:#d4380d}"
    status_class = "failure" if analysis.get("status") == "failed" else ""
    html_text = f"""<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Audio evaluation {html.escape(task_id)}</title><style>{style}</style></head><body>
<h1>Audio evaluation report</h1><p class='muted'>The primary analysis unit is one sample × model candidate condition point. Hidden reference and unprocessed conditions are retained in source tables and excluded from MOS-to-metric correlation.</p>
<div class='status {status_class}'><strong>Run status:</strong> {cell(analysis.get('status', 'unknown'))}</div>
<h2>Task and provenance</h2>{table(metadata_rows(task_id, analysis, manifest, audio, context), [('Field', 'field'), ('Value', 'value')])}
<h2>Audio conditions</h2>{table(audio_items, [('Sample', 'sample_id'), ('Candidate', 'candidate_key'), ('Format', 'format'), ('Sample rate', 'sample_rate'), ('Channels', 'channels'), ('Duration', 'duration_seconds'), ('Audio SHA-256', 'audio_sha256')])}
<h2>Objective metric plan</h2>{table(metric_rows, [('Metric', 'metric'), ('Type', 'type'), ('Direction', 'direction'), ('Reference mode', 'reference_mode'), ('Pipeline', 'pipeline_id'), ('Model correlation', 'participates_in_model_correlation')])}
<h2>Raw listener scores</h2>{table(records(args.subjective), [('Task', 'task_id'), ('Task version', 'task_version'), ('Dataset version', 'dataset_version'), ('Audio version', 'audio_version'), ('Listener', 'listener_id'), ('Session', 'session_id'), ('Device', 'device_type'), ('Round', 'round'), ('Submission', 'submission_kind'), ('Sample', 'sample_id'), ('Candidate', 'candidate_key'), ('Position', 'candidate_position'), ('Score', 'score'), ('Submitted', 'submitted_at'), ('Audio SHA-256', 'audio_sha256')])}
<h2>MOS, SD and t-CI95</h2>{table(records(args.mos), [('Sample', 'sample_id'), ('Candidate', 'candidate_key'), ('n', 'n'), ('MOS', 'mos'), ('SD', 'sd')])}{table(records(args.ci95), [('Sample', 'sample_id'), ('Candidate', 'candidate_key'), ('n', 'n'), ('CI95 low', 'ci95_low'), ('CI95 high', 'ci95_high'), ('Method', 'method')])}
<h2>Objective provenance</h2>{table(records(args.objective), [('Sample', 'sample_id'), ('Candidate', 'candidate_key'), ('Metric', 'metric'), ('Value', 'value'), ('Direction', 'direction'), ('Reference mode', 'reference_mode'), ('Engine commit', 'engine_commit'), ('Capability hash', 'capability_manifest_sha256'), ('Audio SHA-256', 'audio_sha256')])}
<h2>Model-condition MOS-to-metric correlation ranking</h2>{table(correlation_rows, [('Rank', 'rank'), ('Metric', 'metric'), ('Screening', 'screening'), ('n', 'n'), ('Models', 'model_count'), ('Pearson', 'pearson'), ('Spearman', 'spearman'), ('Type', 'metric_type'), ('Direction', 'direction'), ('Reference mode', 'reference_mode')])}
<h2>Screening before and after</h2><pre>{html.escape(json.dumps(screening, indent=2, ensure_ascii=False, sort_keys=True, default=str))}</pre>
<h2>Source SHA-256 and diagnostics</h2>{table(source_files, [('Path', 'path'), ('Kind', 'kind'), ('SHA-256', 'sha256')])}
"""
    if args.diagnostics and args.diagnostics.is_file():
        html_text += f"<h2>Failure diagnostics</h2><pre>{html.escape(args.diagnostics.read_text(encoding='utf-8'))}</pre>"
    elif analysis.get("diagnostics"):
        html_text += f"<h2>Failure diagnostics</h2><pre>{html.escape(json.dumps(analysis['diagnostics'], indent=2, ensure_ascii=False, default=str))}</pre>"
    else:
        html_text += "<h2>Failure diagnostics</h2><p class='muted'>No failure diagnostics supplied.</p>"
    html_text += "</body></html>\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html_text, encoding="utf-8")

    if args.report and not args.artifact_manifest:
        raise SystemExit("--report requires --manifest")
    if args.artifact_manifest:
        base = args.output.parent.parent if args.output.parent.name == "artifacts" else args.output.parent
        inputs = [
            (args.output, "analysis_html"),
            (args.analysis, "analysis_report"),
            (args.artifact_manifest, "final_manifest"),
            (args.context, "evaluation_context"),
            (args.task_manifest, "task_manifest"),
            (args.audio_manifest, "audio_manifest"),
            (args.subjective, "subjective_rows"),
            (args.mos, "mos"),
            (args.ci95, "ci95"),
            (args.objective, "objective_rows"),
            (args.diagnostics, "diagnostics"),
        ]
        # The final manifest cannot hash itself.  It is created after all other
        # entries, and the run report records its resulting digest.
        entries = [manifest_entry(path, role, base) for path, role in inputs if path and path.is_file() and path != args.artifact_manifest]
        final = {
            "schema": "sure.audio_eval.final_manifest.v1",
            "task_id": task_id,
            "task_version": manifest.get("task_version", analysis.get("task_version")),
            "dataset_version": manifest.get("dataset_version", analysis.get("dataset_version")),
            "audio_version": audio.get("audio_version", analysis.get("audio_version")),
            "created_at": args.created_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "artifacts": entries,
            "artifact_count": len(entries),
            "provenance": provenance,
        }
        args.artifact_manifest.parent.mkdir(parents=True, exist_ok=True)
        args.artifact_manifest.write_text(json.dumps(final, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps({"schema": "sure.audio_eval.run_report.v1", "report_persisted": True, "analysis_html": str(args.output), "manifest_path": str(args.artifact_manifest), "artifact_manifest_sha256": file_sha256(args.artifact_manifest), "status": "success"}, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

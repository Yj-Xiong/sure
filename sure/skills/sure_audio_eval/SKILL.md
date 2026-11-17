# /sure_audio_eval

Run a reproducible speech-quality evaluation that joins objective metrics with subjective listening scores. This command is an orchestration layer; it does not replace `/sure_eval`, which remains evaluation-only for an existing prediction bundle.

## Invocation

```text
/sure_audio_eval task_manifest=<path> subjective_source=<path[,path...]> objective_source=<path[,path...]> objective_metrics=<metric,...> [engine_commit=<commit>] [capabilities_sha256=<sha256>] [analysis_unit=model_condition] [output_dir=<dir>]
```

The task manifest must declare the complete sample and candidate whitelist, the user-selected `analysis_candidate_keys` model whitelist, audio version, reference roles, scale, analysis policy and an `interface_contract` conforming to `schemas/interface_contract.schema.json`. The contract freezes the listening interface decisions that affect reproducibility: leaderboard creation and publication, note visibility, automatic or uploaded sample source, clean/noisy/processed pairing, target management, score validation, complete-task submission, mobile entry and resume controls. `resolve_task` freezes which models are eligible for the primary correlation. `resolve_metrics` then records the requested metric type, direction, reference mode and exact route against that frozen model set. For every requested objective metric it must also declare `metric_policies.<metric>.direction` (`higher_better` or `lower_better`). `type`, `reference_mode` and `matching_reference_required` are checked against the locked engine route; they are not inferred from a metric name. Sources must be inside the project or an approved input root. Use the locked SURE evaluation engine for objective routes; use a deterministic importer for raw CSV/JSON listening submissions.

## State machine

1. `resolve_task` writes `task_manifest_resolved.json` and freezes `evaluation_context.json` and `tool_registry.json`. The interface contract is checked here so later stages cannot silently change how a listening task is sampled, scored, submitted or published.
2. `resolve_metrics` runs `scripts/resolve_metric_plan.py` against the checked-in `engine-capabilities.generated.json`, `runtime.json` and `references/STANDARDS.md`, then writes `objective_metric_plan.json` with the exact route, engine commit, runtime lock hash and capability hash. A missing route or missing direction is a failed stage; no fallback metric or old report is accepted.
3. `validate_audio` writes the audio inventory and checks hashes, format, sample rate, duration, loudness and true peak.
4. `run_objective` writes one objective row per declared sample, candidate and metric, with route and engine provenance.
5. `ingest_subjective` validates raw ratings, keeps source rows, applies latest-valid deduplication and records complete groups.
6. `aggregate_subjective` writes condition MOS, standard deviation and t-CI95 without filling missing groups.
7. `joint_analysis` computes Pearson/Spearman between subjective MOS and each planned objective metric on model-only condition points. Reference and unprocessed input are excluded; no four candidate-scope result is created.
   Its screening substep reads only the subjective score distribution for the frozen model candidates. The versioned rule is rating-level `n≥8`, `3×IQR` and at least 20 points from the group median. It writes before and after summaries plus a flagged list. Objective scores, regression residuals and correlation coefficients are unavailable to the rule. High-leverage observations remain diagnostic records and are never deleted because a correlation changes.
8. `render_report` writes a standalone HTML report and `main_agent_run_report.json` only after all upstream gates pass.

Read the package `AGENTS.md` before working. Each unit owns its artifact; do not merge later results into an earlier artifact. Repair a gate by changing its inputs or output, not by repeating an unchanged command.

Read `references/STANDARDS.md` before producing `objective_metric_plan.json`; every stage must carry the corresponding reference sources from that mapping.

## Required final call

Call `sure_finish` only after `main_agent_run_report.json` and the final manifest exist. Use `status: failed` when a required input, route, artifact or gate cannot be satisfied. Never claim that an objective score was computed when the engine did not produce its row and provenance.

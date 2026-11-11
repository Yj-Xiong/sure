# Audio evaluation Harness rules

This package is the deterministic control layer for subjective listening scores and objective speech-quality metrics. The agent chooses the task scope and invokes registered tools; scripts decide whether inputs and outputs satisfy the contract.

The stage-to-standard mapping is maintained in `references/STANDARDS.md`. Do not add a new reference basis in a gate or metric plan without checking that mapping and stating the boundary of the reference.

## Required input contract

- `task_manifest` is the only source of truth for `task_id`, `dataset_version`, sample IDs, candidate keys, scale, audio version, reference roles and the requested analysis unit. Do not infer missing samples, candidates, versions or metric direction.
- Every audio condition is identified by sample ID and candidate key and carries a source path, format metadata and SHA-256. Full-reference metrics require the matching reference for the same sample; no-reference metrics must not receive that reference.
- The manifest records sample rate, channels, duration, integrated loudness policy and true-peak limit. A failed audio check stops the run. There is no silent resampling, loudness fallback or fabricated model output.
- The public reference is playback-only. A hidden reference may be one of the scored candidates and is retained in the scoring table, but it is not a member of the method/model correlation matrix. The unprocessed input is treated the same way.

## Subjective score rules

- Raw submissions are immutable. Validate listener ID, task ID, round, sample ID, candidate key, scale, finite score and submission time before deduplication.
- The latest valid record wins for `(task_id, normalized_listener_id, sample_id, candidate_key)`. Session and round remain provenance; they do not silently create a second independent listener.
- A complete listening group requires every declared candidate to have one valid score. Do not fill missing values or treat a partial batch as complete.
- Build one condition point per `sample_id × candidate_key` by averaging listeners. Pearson and Spearman use these condition points with equal weight. Candidate means across samples are auxiliary only.
- Report MOS, sample standard deviation, t-distribution 95% CI, listener count and missingness. Keep raw, deduplicated, screened-before and screened-after tables.

## Screening and analysis

- Screening is versioned rating-level sensitivity analysis. It cannot inspect objective scores, regression residuals or correlation values to decide what to remove, and it never deletes an entire listener only because that listener differs from the group.
- Correlation analysis uses model-only condition points. Each objective metric is correlated with the subjective MOS over common `sample_id × model candidate` conditions. Hidden reference and unprocessed input are excluded, and there is no four-scope correlation split.
- Each correlation row carries the metric, common-point count, model count, Pearson and Spearman. MOS, CI95, screening sensitivity and provenance remain separate report layers.
- The report follows: experiment and audio condition, raw listener scores, MOS and CI95, objective rows, model-condition MOS-to-metric correlations, screening sensitivity and provenance.

## Release rules

- Every stage writes a schema-validated artifact under the run directory. Paths, row counts, hashes, task/version identity and metric engine commit are recorded.
- The frozen context records the external engine commit and capability-manifest hash when supplied by the locked runtime. A missing or changed engine identity is a provenance problem, not a reason to reuse an older report silently.
- A successful finish requires the context, registry, task/audio, objective, subjective, condition, analysis, HTML and final run-report artifacts. A missing or stale artifact is a failed gate, not an incomplete success.
- Only scripts named in `tool_registry.json` may be invoked. Repeated unchanged repairs or tool calls trigger `loop_guard.json` and stop the run for human repair.

import { createHash } from "node:crypto";
import { existsSync, readFileSync, statSync } from "node:fs";
import { isAbsolute, relative, resolve } from "node:path";
import type { SureHookContext } from "@earendil-works/pi-coding-agent/hooks";
import { canonicalJson, verifyEvaluationContext } from "../../../runtime/evaluation/context.ts";
import { readToolRegistry, validateToolRegistry } from "../../../runtime/evaluation/tool-registry.ts";
import type { GateResult } from "./checkpoints.ts";

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function string(value: unknown): value is string {
	return typeof value === "string" && value.trim() !== "";
}

function finite(value: unknown): value is number {
	return typeof value === "number" && Number.isFinite(value);
}

function pathInside(base: string, candidate: string): boolean {
	const rel = relative(base, candidate);
	return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel));
}

function runPath(ctx: SureHookContext, value: string): string | undefined {
	const candidate = isAbsolute(value) ? resolve(value) : resolve(ctx.runDir, value);
	return pathInside(ctx.cwd, candidate) ? candidate : undefined;
}

function readJsonLines(path: string): Array<Record<string, unknown>> {
	return readFileSync(path, "utf8")
		.split(/\r?\n/)
		.map((line) => line.trim())
		.filter(Boolean)
		.map((line) => JSON.parse(line) as Record<string, unknown>);
}

function readJson(path: string): Record<string, unknown> | undefined {
	try {
		const value = JSON.parse(readFileSync(path, "utf8"));
		return isRecord(value) ? value : undefined;
	} catch {
		return undefined;
	}
}

function fileSha256(path: string): string {
	const digest = createHash("sha256");
	digest.update(readFileSync(path));
	return digest.digest("hex");
}

function taskArtifact(ctx: SureHookContext): Record<string, unknown> | undefined {
	return readJson(join(ctx.runDir, "artifacts", "task_manifest_resolved.json"));
}

function metricPlanArtifact(ctx: SureHookContext): Record<string, unknown> | undefined {
	return readJson(join(ctx.runDir, "artifacts", "objective_metric_plan.json"));
}

function audioArtifact(ctx: SureHookContext): Record<string, unknown> | undefined {
	return readJson(join(ctx.runDir, "artifacts", "audio_manifest.json"));
}

function sourceTaskManifest(ctx: SureHookContext): Record<string, unknown> | undefined {
	const context = readJson(join(ctx.runDir, "artifacts", "evaluation_context.json"));
	if (!context || typeof context.task_manifest !== "string") return undefined;
	const path = resolve(ctx.cwd, context.task_manifest);
	return pathInside(ctx.cwd, path) ? readJson(path) : undefined;
}

function sameJson(left: unknown, right: unknown): boolean {
	return canonicalJson(left) === canonicalJson(right);
}

function objectiveRowsGate(ctx: SureHookContext, artifact: Record<string, unknown>, rows: Array<Record<string, unknown>>): string | undefined {
	const task = taskArtifact(ctx);
	const plan = metricPlanArtifact(ctx);
	const audio = audioArtifact(ctx);
	if (!task || !plan || !audio) return "task manifest, metric plan and audio manifest are required before objective rows";
	const audioPolicy = isRecord(task.audio_policy) ? task.audio_policy : {};
	const samples = new Set((Array.isArray(task.sample_ids) ? task.sample_ids : []).map(String));
	const candidates = new Set((Array.isArray(task.candidate_keys) ? task.candidate_keys : []).map(String));
	const planMetrics = new Map<string, Record<string, unknown>>();
	for (const value of Array.isArray(plan.metrics) ? plan.metrics : []) {
		if (!isRecord(value) || value.participates_in_model_correlation === false) continue;
		if (typeof value.metric === "string") planMetrics.set(value.metric, value);
	}
	const audioItems = new Map<string, Record<string, unknown>>();
	for (const value of Array.isArray(audio.items) ? audio.items : []) {
		if (isRecord(value) && typeof value.sample_id === "string" && typeof value.candidate_key === "string")
			audioItems.set(`${value.sample_id}\0${value.candidate_key}`, value);
	}
	const expected = new Set<string>();
	for (const sample of samples) for (const candidate of candidates) for (const metric of planMetrics.keys()) expected.add(`${sample}\0${candidate}\0${metric}`);
	const seen = new Set<string>();
	for (const row of rows) {
		for (const key of ["task_id", "task_version", "dataset_version", "audio_version", "sample_id", "candidate_key", "metric", "direction", "reference_mode", "engine_commit", "pipeline_id", "audio_sha256", "capability_manifest_sha256"])
			if (typeof row[key] !== "string" || String(row[key]).length === 0) return `objective row missing ${key}`;
		if (row.task_id !== task.task_id || row.task_version !== task.task_version || row.dataset_version !== task.dataset_version || row.audio_version !== audioPolicy.audio_version)
			return "objective row task/dataset/audio version differs from frozen task";
		if (!samples.has(String(row.sample_id)) || !candidates.has(String(row.candidate_key))) return "objective row is outside task sample/candidate whitelist";
		const spec = planMetrics.get(String(row.metric));
		if (!spec) return `objective metric is absent from metric plan: ${String(row.metric)}`;
		if (row.direction !== spec.direction || row.reference_mode !== spec.reference_mode || row.pipeline_id !== spec.pipeline_id)
			return `objective row semantics differ from metric plan: ${String(row.metric)}`;
		if (row.engine_commit !== artifact.engine_commit || row.engine_commit !== plan.engine_commit) return "objective row engine_commit differs from locked plan";
		if (row.capability_manifest_sha256 !== plan.capability_manifest_sha256) return "objective row capability hash differs from locked plan";
		const item = audioItems.get(`${row.sample_id}\0${row.candidate_key}`);
		if (!item || row.audio_sha256 !== item.audio_sha256) return `objective row audio hash does not match audio manifest: ${String(row.sample_id)} × ${String(row.candidate_key)}`;
		if (row.reference_mode === "paired" && (typeof row.reference_audio_sha256 !== "string" || (item.reference_audio_sha256 && row.reference_audio_sha256 !== item.reference_audio_sha256))) return "paired objective row reference hash is missing or mismatched";
		if (row.reference_mode === "none" && row.reference_audio_sha256 !== undefined) return "no-reference objective row carries reference_audio_sha256";
		const key = `${row.sample_id}\0${row.candidate_key}\0${row.metric}`;
		if (seen.has(key)) return `duplicate objective condition: ${key}`;
		seen.add(key);
	}
	if (seen.size !== expected.size || [...expected].some((key) => !seen.has(key))) return "objective rows do not cover the complete task sample × candidate × metric matrix";
	return undefined;
}

function finalManifestError(ctx: SureHookContext, manifest: Record<string, unknown>): string | undefined {
	const entries = Array.isArray(manifest.artifacts) ? manifest.artifacts : [];
	if (!Number.isInteger(manifest.artifact_count) || manifest.artifact_count !== entries.length) return "final manifest artifact_count does not match artifacts";
	const required = new Set(["analysis_html", "analysis_report", "evaluation_context", "tool_registry", "task_manifest", "audio_manifest", "subjective_rows", "mos", "ci95", "objective_rows"]);
	const roles = new Set<string>();
	for (const entry of entries) {
		if (!isRecord(entry) || typeof entry.role !== "string" || typeof entry.path !== "string" || !/^[a-f0-9]{64}$/i.test(String(entry.sha256))) return "final manifest artifact entry is incomplete";
		if (roles.has(entry.role)) return `duplicate final manifest role: ${entry.role}`;
		roles.add(entry.role);
		const path = runPath(ctx, entry.path);
		if (!path || !existsSync(path) || !statSync(path).isFile()) return `final manifest artifact is missing: ${entry.path}`;
		if (fileSha256(path) !== String(entry.sha256).toLowerCase()) return `final manifest artifact hash mismatch: ${entry.path}`;
	}
	for (const role of required) if (!roles.has(role)) return `final manifest is missing required artifact role: ${role}`;
	return undefined;
}

function interfaceContractError(contract: unknown): string | undefined {
	if (!isRecord(contract)) return "interface_contract must be an object";
	if (contract.schema !== "sure.audio_eval.interface_contract.v1")
		return "interface_contract schema must be sure.audio_eval.interface_contract.v1";
	if (!["tts", "speech_enhancement", "other_audio"].includes(String(contract.evaluation_kind)))
		return "interface_contract evaluation_kind must be tts, speech_enhancement or other_audio";

	const leaderboard = contract.leaderboard;
	if (!isRecord(leaderboard)) return "interface_contract leaderboard is required";
	if (!["existing", "create"].includes(String(leaderboard.mode)))
		return "interface_contract leaderboard.mode must be existing or create";
	if (leaderboard.publish_policy !== "after_settlement")
		return "interface_contract leaderboard.publish_policy must be after_settlement";
	if (leaderboard.mode === "existing" && !string(leaderboard.leaderboard_id))
		return "existing leaderboard requires leaderboard_id";
	if (leaderboard.mode === "create" && leaderboard.leaderboard_id !== null)
		return "new leaderboard must use leaderboard_id=null before creation";

	const visibility = contract.visibility;
	if (
		!isRecord(visibility) ||
		typeof visibility.initiator_note_visible_to_rater !== "boolean" ||
		typeof visibility.rater_instruction_required !== "boolean" ||
		typeof visibility.reference_audio_explanation_visible_to_rater !== "boolean"
	)
		return "interface_contract visibility must declare note, instruction and reference explanation visibility";

	const source = contract.sample_source;
	if (
		!isRecord(source) ||
		!["auto", "upload"].includes(String(source.mode)) ||
		!["fixed_dataset", "random"].includes(String(source.selection_policy))
	)
		return "interface_contract sample_source must declare mode and selection_policy";
	if (source.mode === "auto" && !string(source.source_id)) return "auto sample_source requires source_id";
	if (source.mode === "upload" && !string(source.package_path)) return "upload sample_source requires package_path";

	const pairing = contract.audio_pairing;
	if (!isRecord(pairing)) return "interface_contract audio_pairing must be declared";
	const kind = String(contract.evaluation_kind);
	const validPairing = kind === "speech_enhancement"
		? pairing.relation === "clean_noisy_processed" && ["dataset", "task"].includes(String(pairing.processing_stage))
		: ["text_to_speech", "reference_text", "none", "clean_noisy_processed"].includes(String(pairing.relation)) && ["dataset", "task", "generation", "none"].includes(String(pairing.processing_stage));
	if (!validPairing) return `interface_contract audio_pairing is incompatible with evaluation_kind=${kind}`;

	const targets = contract.model_targets;
	if (
		!isRecord(targets) ||
		!["leaderboard_models", "custom_scene"].includes(String(targets.management)) ||
		typeof targets.allow_remove !== "boolean" ||
		typeof targets.allow_custom !== "boolean"
	)
		return "interface_contract model_targets must declare management and removal/custom flags";

	const scoring = contract.scoring;
	if (
		!isRecord(scoring) ||
		scoring.metric_configuration_source !== "task_manifest.metric_policies" ||
		scoring.scale_source !== "task_manifest.scale" ||
		scoring.aggregation !== "arithmetic_mean"
	)
		return "interface_contract scoring must bind metric policies, scale and arithmetic_mean aggregation to the task manifest";
	const referenceValidation = scoring.reference_validation;
	if (
		!isRecord(referenceValidation) ||
		typeof referenceValidation.enabled !== "boolean" ||
		!string(referenceValidation.rule_version)
	)
		return "interface_contract scoring.reference_validation must declare enabled and rule_version";

	const submission = contract.submission;
	if (
		!isRecord(submission) ||
		submission.batch_mode !== "single_task" ||
		submission.completion_policy !== "all_declared_candidates" ||
		submission.allow_partial !== false
	)
		return "interface_contract submission must require one complete task submission";

	const delivery = contract.delivery;
	if (
		!isRecord(delivery) ||
		typeof delivery.mobile_compatible !== "boolean" ||
		typeof delivery.shareable_entry !== "boolean" ||
		delivery.interruption !== "resume" ||
		!Array.isArray(delivery.progress_controls) ||
		delivery.progress_controls.length === 0 ||
		delivery.progress_controls.some((value) => !["pause", "cancel", "restart"].includes(String(value)))
	)
		return "interface_contract delivery must declare mobile, shareable, resume and progress controls";
	return undefined;
}

function requireContext(ctx: SureHookContext): {
	context: Record<string, unknown>;
	errors: string[];
} {
	const verified = verifyEvaluationContext(ctx.cwd, ctx.runDir);
	const context = verified.context as Record<string, unknown> | undefined;
	if (!context) return { context: {} as Record<string, unknown>, errors: verified.errors };
	const registry = readToolRegistry(ctx.runDir);
	const errors = registry ? validateToolRegistry(registry) : ["tool_registry.json is missing or invalid"];
	return { context, errors: [...verified.errors, ...errors] };
}

export function semanticGate(unitId: string, artifact: unknown, ctx: SureHookContext): GateResult {
	if (!isRecord(artifact))
		return { ok: false, reason: "artifact is not an object", repair: `Unit ${unitId} must produce a JSON object.` };
	const { context, errors } = requireContext(ctx);
	if (errors.length > 0)
		return { ok: false, reason: "context or registry invalid", repair: errors.join("; ") };
	if (unitId === "resolve_task") {
		const samples = artifact.sample_ids;
		const candidates = artifact.candidate_keys;
		const analysisCandidates = artifact.analysis_candidate_keys;
		if (
			!string(artifact.task_id) ||
			!string(artifact.task_version) ||
			!string(artifact.dataset_version) ||
			!string(artifact.engine_task) ||
			!Array.isArray(samples) ||
			!Array.isArray(candidates) ||
			!Array.isArray(analysisCandidates) ||
			!isRecord(artifact.candidate_roles)
		) {
			return {
				ok: false,
				reason: "task manifest fields missing",
				repair: "Declare task_id, dataset_version, sample_ids and candidate_keys in task_manifest_resolved.json.",
			};
		}
		const interfaceError = interfaceContractError(artifact.interface_contract);
		if (interfaceError) {
			return {
				ok: false,
				reason: "interface contract incomplete",
				repair: `${interfaceError}. Freeze the listening interface contract before resolving metrics.`,
			};
		}
		const candidateRoles = artifact.candidate_roles as Record<string, unknown>;
		if (
			new Set(samples).size !== samples.length ||
			new Set(candidates).size !== candidates.length ||
			new Set(analysisCandidates).size !== analysisCandidates.length ||
			candidates.some((candidate) => !string(candidateRoles[String(candidate)]))
		) {
			return {
				ok: false,
				reason: "duplicate whitelist entry",
				repair: "Remove duplicate sample_ids or candidate_keys; do not silently merge them.",
			};
		}
		const candidateSet = new Set(candidates.map(String));
		if (
			analysisCandidates.some(
				(candidate) =>
					!string(candidate) ||
					!candidateSet.has(String(candidate)) ||
					!["model", "method", "enhancement", "enhanced_model", "system"].includes(
						String(candidateRoles[String(candidate)]).toLowerCase(),
					),
			)
		) {
			return {
				ok: false,
				reason: "analysis candidate whitelist is invalid",
				repair:
					"analysis_candidate_keys must be a unique subset of candidate_keys and every entry must have a model or method role.",
			};
		}
		if (
			!isRecord(artifact.scale) ||
			!finite(artifact.scale.min) ||
			!finite(artifact.scale.max) ||
			artifact.scale.max <= artifact.scale.min
		) {
			return {
				ok: false,
				reason: "invalid scale",
				repair: "Declare a finite increasing score scale in task_manifest_resolved.json.",
			};
		}
			const source = sourceTaskManifest(ctx);
			const context = requireContext(ctx).context;
			const sourceFiles = Array.isArray(context.source_files) ? context.source_files : [];
			const sourceEntry = sourceFiles.find((entry) => isRecord(entry) && entry.kind === "task_manifest");
			if (!source || !isRecord(sourceEntry) || typeof sourceEntry.sha256 !== "string")
				return { ok: false, reason: "task source manifest is unavailable", repair: "Resolve the task manifest from the frozen source and include source_manifest_sha256." };
			if (artifact.source_manifest_sha256 !== sourceEntry.sha256)
				return { ok: false, reason: "task manifest source hash mismatch", repair: "Set source_manifest_sha256 to the hash recorded in evaluation_context.json." };
			for (const field of ["task_id", "task_version", "dataset_version", "engine_task", "language", "sample_ids", "candidate_keys", "analysis_candidate_keys", "candidate_roles", "scale", "audio_policy", "interface_contract", "analysis_unit", "excluded_candidates_from_correlation", "metric_policies", "objective_metrics", "screening_policy", "standards"]) {
				if (source[field] !== undefined && !sameJson(source[field], artifact[field]))
					return { ok: false, reason: `resolved task field differs from source: ${field}`, repair: `Copy ${field} from the frozen task manifest without changing its value.` };
			}
			return { ok: true };
	}
	if (unitId === "resolve_metrics") {
		if (
			!string(artifact.task_id) ||
			!string(artifact.task_version) ||
			!string(artifact.dataset_version) ||
			!string(artifact.audio_version) ||
			artifact.analysis_unit !== "model_condition" ||
			artifact.status !== "success" ||
			!Array.isArray(artifact.excluded_candidates) ||
			!artifact.excluded_candidates.includes("hidden_reference") ||
			!artifact.excluded_candidates.includes("unprocessed") ||
			!Array.isArray(artifact.model_candidate_keys) ||
			artifact.model_candidate_keys.length === 0 ||
			!isRecord(artifact.standards_by_stage) ||
			!Array.isArray(artifact.metrics) ||
			artifact.metrics.length === 0
		) {
			return {
				ok: false,
				reason: "objective metric plan incomplete",
				repair:
					"Declare model_condition analysis, exclude hidden_reference and unprocessed, and list the objective metrics before scoring.",
			};
		}
		const task = taskArtifact(ctx);
		if (!task || !sameJson(artifact.model_candidate_keys, task.analysis_candidate_keys))
			return { ok: false, reason: "metric plan model set differs from task manifest", repair: "Use exactly task_manifest_resolved.analysis_candidate_keys in objective_metric_plan.json." };
		for (const field of ["task_id", "task_version", "dataset_version"]) {
			if (!sameJson(artifact[field], task[field])) return { ok: false, reason: `metric plan ${field} mismatch`, repair: `Copy ${field} from task_manifest_resolved.json.` };
		}
		const expectedStages = [
			"resolve_task",
			"resolve_metrics",
			"validate_audio",
			"run_objective",
			"ingest_subjective",
			"aggregate_subjective",
			"joint_analysis",
			"screening",
			"render_report",
		];
		for (const stage of expectedStages) {
			if (!Array.isArray(artifact.standards_by_stage[stage]) || artifact.standards_by_stage[stage].length === 0)
				return {
					ok: false,
					reason: `standards missing for ${stage}`,
					repair: `Add at least one explicit reference source for stage ${stage} in objective_metric_plan.json.`,
				};
		}
		const metricKeys = new Set<string>();
		if (
			!string(artifact.engine_task) ||
			!string(artifact.engine_commit) ||
			!/^[a-f0-9]{64}$/i.test(String(artifact.capability_manifest_sha256))
		) {
			return {
				ok: false,
				reason: "engine provenance missing",
				repair:
					"Resolve routes from the locked capability manifest and include engine_task, engine_commit and capability_manifest_sha256.",
			};
		}
		if (context?.engine_commit && context.engine_commit !== artifact.engine_commit) {
			return {
				ok: false,
				reason: "metric plan engine drift",
				repair: "The metric plan engine_commit must equal the frozen evaluation context engine_commit.",
			};
		}
		if (
			context?.capability_manifest_sha256 &&
			context.capability_manifest_sha256 !== artifact.capability_manifest_sha256
		) {
			return {
				ok: false,
				reason: "capability manifest drift",
				repair: "The metric plan capability_manifest_sha256 must equal the frozen evaluation context.",
			};
		}
		for (const metric of artifact.metrics) {
			if (
				!isRecord(metric) ||
				!string(metric.metric) ||
				!["intrusive", "non_intrusive"].includes(String(metric.type)) ||
				!["higher_better", "lower_better"].includes(String(metric.direction)) ||
				!["paired", "none"].includes(String(metric.reference_mode)) ||
				!string(metric.pipeline_id) ||
				!string(metric.input_contract) ||
				!Array.isArray(metric.required_audio_inputs) ||
				typeof metric.participates_in_model_correlation !== "boolean"
			) {
				return {
					ok: false,
					reason: "objective metric definition incomplete",
					repair:
						"Each metric needs metric, type, direction and reference_mode; type must be intrusive or non_intrusive.",
				};
			}
			const required = metric.required_audio_inputs.map(String);
			const paired = metric.reference_mode === "paired";
			const optional = Array.isArray(metric.optional_audio_inputs) ? metric.optional_audio_inputs.map(String) : [];
			if (
				(paired &&
					(metric.type !== "intrusive" ||
						!required.includes("reference_audio") ||
						metric.matching_reference_required !== true)) ||
				(!paired &&
					(metric.type !== "non_intrusive" ||
						required.includes("reference_audio") ||
						optional.includes("reference_audio") ||
						metric.matching_reference_required !== false))
			) {
				return {
					ok: false,
					reason: "reference policy conflicts with engine route",
					repair:
						"Derive intrusive/paired from the locked route required_roles: reference_audio is required only for paired metrics.",
				};
			}
			if (metricKeys.has(String(metric.metric)))
				return {
					ok: false,
					reason: "duplicate objective metric",
					repair: `Metric ${String(metric.metric)} appears more than once in objective_metric_plan.json.`,
				};
			metricKeys.add(String(metric.metric));
		}
		return { ok: true };
	}
	if (unitId === "validate_audio") {
		if (
			!string(artifact.task_id) ||
			!string(artifact.audio_version) ||
			artifact.validated !== true ||
			!Array.isArray(artifact.items) ||
			artifact.items.length === 0
		) {
			return {
				ok: false,
				reason: "audio manifest incomplete",
				repair: "Validate audio_manifest.json and include audio_version, items and validated:true.",
			};
		}
		const task = taskArtifact(ctx);
		if (!task) return { ok: false, reason: "task manifest missing", repair: "Validate audio only after task_manifest_resolved.json exists." };
		const expected = new Set<string>();
		for (const sample of Array.isArray(task.sample_ids) ? task.sample_ids : [])
			for (const candidate of Array.isArray(task.candidate_keys) ? task.candidate_keys : []) expected.add(`${sample}\0${candidate}`);
		const seen = new Set<string>();
		for (const item of artifact.items) {
			if (
				!isRecord(item) ||
				!string(item.sample_id) ||
				!string(item.candidate_key) ||
				!/^[a-f0-9]{64}$/i.test(String(item.audio_sha256))
			) {
				return {
					ok: false,
					reason: "audio identity missing",
					repair: "Every audio item needs sample_id, candidate_key and a SHA-256 hash.",
				};
			}
			const key = `${item.sample_id}\0${item.candidate_key}`;
			if (seen.has(key) || !expected.has(key)) return { ok: false, reason: "audio manifest condition mismatch", repair: "Audio manifest must contain each task sample × candidate exactly once." };
			seen.add(key);
		}
		if (seen.size !== expected.size) return { ok: false, reason: "audio manifest matrix incomplete", repair: "Add every task sample × candidate audio condition to audio_manifest.json." };
		return { ok: true };
	}
	if (unitId === "run_objective") {
		if (artifact.status === "failed") {
			return Array.isArray(artifact.diagnostics) && artifact.diagnostics.length > 0
				? {
						ok: false,
						reason: "objective engine failed",
						repair:
							"Objective execution failed; inspect diagnostics and repair the locked engine/input before resuming.",
					}
				: {
						ok: false,
						reason: "failed objective run lacks diagnostics",
						repair: "Record a recoverable diagnostic for the failed objective execution.",
					};
		}
		const rowsPath = string(artifact.metric_rows_path) ? runPath(ctx, artifact.metric_rows_path) : undefined;
		if (!rowsPath || !existsSync(rowsPath) || !statSync(rowsPath).isFile())
			return {
				ok: false,
				reason: "objective rows missing",
				repair: "Write objective_scores.jsonl below the project or run directory and reference it.",
			};
		let rows: Array<Record<string, unknown>>;
		try {
			rows = readJsonLines(rowsPath);
		} catch {
			return {
				ok: false,
				reason: "objective rows are not JSONL",
				repair: "Write one valid JSON object per objective score row.",
			};
		}
		if (rows.length !== artifact.row_count || rows.length === 0)
			return {
				ok: false,
				reason: "objective row count mismatch",
				repair: "Make row_count equal the finite rows in objective_scores.jsonl.",
			};
		const objectiveError = objectiveRowsGate(ctx, artifact, rows);
		if (objectiveError) return { ok: false, reason: "objective row integrity failure", repair: objectiveError };
		const keys = new Set<string>();
		for (const row of rows) {
			if (
				!string(row.sample_id) ||
				!string(row.candidate_key) ||
				!string(row.metric) ||
				!finite(row.value) ||
				!string(row.direction) ||
				!string(row.engine_commit) ||
				!string(row.reference_mode)
			)
				return {
					ok: false,
					reason: "objective row incomplete",
					repair:
						"Each objective row needs sample_id, candidate_key, metric, finite value, direction, reference_mode and engine_commit.",
				};
			if (row.reference_mode === "paired" && !string(row.reference_audio_sha256))
				return {
					ok: false,
					reason: "paired objective row lacks reference hash",
					repair: "Full-reference metrics must carry the matching reference audio SHA-256.",
				};
			if (row.reference_mode === "none" && row.reference_audio_sha256 !== undefined)
				return {
					ok: false,
					reason: "no-reference row carries a reference",
					repair: "No-reference metrics must not receive a paired reference audio.",
				};
			const key = `${row.sample_id}\0${row.candidate_key}\0${row.metric}`;
			if (keys.has(key))
				return {
					ok: false,
					reason: "duplicate objective row",
					repair: `Duplicate objective condition ${key}; keep one engine result with provenance.`,
				};
			keys.add(key);
		}
		return { ok: true };
	}
	if (unitId === "ingest_subjective") {
		const dedup = string(artifact.dedup_path) ? runPath(ctx, artifact.dedup_path) : undefined;
		if (
			!dedup ||
			!existsSync(dedup) ||
			!statSync(dedup).isFile() ||
			!Array.isArray(artifact.raw_sources) ||
			artifact.raw_sources.length === 0
		)
			return {
				ok: false,
				reason: "subjective input incomplete",
				repair: "Keep raw sources and write a deduplicated score table.",
			};
		if (
			!String(artifact.latest_valid_rule).includes("listener") ||
			!String(artifact.latest_valid_rule).includes("sample") ||
			!String(artifact.latest_valid_rule).includes("candidate")
		)
			return {
				ok: false,
				reason: "dedup rule is not explicit",
				repair: "State the latest-valid key as task × listener × sample × candidate.",
			};
		return { ok: true };
	}
	if (unitId === "aggregate_subjective") {
		const mos = string(artifact.mos_path) ? runPath(ctx, artifact.mos_path) : undefined;
		const ci = string(artifact.ci95_path) ? runPath(ctx, artifact.ci95_path) : undefined;
		if (
			!mos ||
			!ci ||
			!existsSync(mos) ||
			!existsSync(ci) ||
			artifact.analysis_unit !== "sample_x_candidate" ||
			!finite(artifact.condition_count) ||
			artifact.condition_count < 1
		)
			return {
				ok: false,
				reason: "condition summary incomplete",
				repair: "Write MOS and CI95 tables for the sample × candidate analysis unit.",
			};
		return { ok: true };
	}
	if (unitId === "joint_analysis") {
		if (
			artifact.correlation_unit !== "model_condition" ||
			!Array.isArray(artifact.correlations) ||
			artifact.correlations.length === 0 ||
			!isRecord(artifact.screening)
		)
			return {
				ok: false,
				reason: "analysis contract incomplete",
				repair:
					"Report MOS-to-objective Pearson and Spearman rows on model-only conditions; exclude reference and unprocessed input.",
			};
		return { ok: true };
	}
	if (unitId === "render_report") {
		const html = string(artifact.analysis_html) ? runPath(ctx, artifact.analysis_html) : undefined;
		const manifestPath = string(artifact.manifest_path) ? runPath(ctx, artifact.manifest_path) : undefined;
		if (
			!html ||
			!existsSync(html) ||
			!statSync(html).isFile() ||
			artifact.report_persisted !== true ||
			!manifestPath ||
			!existsSync(manifestPath)
		)
			return {
				ok: false,
				reason: "report or final manifest not persisted",
				repair: "Write standalone HTML and final_manifest.json before marking the report persisted.",
			};
		try {
			const manifest = JSON.parse(readFileSync(manifestPath, "utf8")) as Record<string, unknown>;
			if (
				manifest.schema !== "sure.audio_eval.final_manifest.v1" ||
				!string(manifest.task_version) ||
				!string(manifest.dataset_version) ||
				!string(manifest.audio_version) ||
				!Array.isArray(manifest.artifacts) ||
				manifest.artifacts.length < 1
			)
				return {
					ok: false,
					reason: "final manifest incomplete",
					repair: "Final manifest must include task/dataset/audio versions and hashed artifacts.",
				};
			const manifestError = finalManifestError(ctx, manifest);
			if (manifestError) return { ok: false, reason: "final manifest integrity failure", repair: manifestError };
		} catch {
			return {
				ok: false,
				reason: "final manifest invalid",
				repair: "Write a parseable final_manifest.json with artifact hashes.",
			};
		}
		return { ok: true };
	}
	return { ok: false, reason: `unknown unit ${unitId}`, repair: `No semantic gate exists for unit ${unitId}.` };
}

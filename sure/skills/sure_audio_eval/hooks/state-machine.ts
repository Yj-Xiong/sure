import type { SureHookContext } from "@earendil-works/pi-coding-agent/hooks";
import type { GateResult } from "./checkpoints.ts";

export type UnitKind = "linear" | "gate";

export interface Unit {
	id: string;
	label: string;
	kind: UnitKind;
	produces: string;
	schemaRef?: string;
	requiredFields?: string[];
	allowedValues?: Record<string, unknown[]>;
	forbiddenFields?: string[];
	gateCheck?: (artifact: unknown) => GateResult;
	gateScript?: string;
	gateScriptArgs?: (ctx: SureHookContext) => string[];
	helperScripts?: string[];
	gateInputs?: string[];
}

export const MAIN_FLOW_UNITS: Unit[] = [
	{
		id: "resolve_task",
		label: "Resolve task contract",
		kind: "linear",
		produces: "task_manifest_resolved.json",
		schemaRef: "task_manifest_resolved.schema.json",
		requiredFields: [
			"schema",
			"task_id",
			"task_version",
			"dataset_version",
			"engine_task",
			"sample_ids",
			"candidate_keys",
			"analysis_candidate_keys",
			"candidate_roles",
			"scale",
			"audio_policy",
			"interface_contract",
		],
		allowedValues: { schema: ["sure.audio_eval.task_manifest.v1"] },
		forbiddenFields: ["objective_rows", "condition_points", "report_persisted"],
		helperScripts: ["check_audio_eval.py"],
	},
	{
		id: "resolve_metrics",
		label: "Resolve objective metric plan",
		kind: "gate",
		produces: "objective_metric_plan.json",
		schemaRef: "objective_metric_plan.schema.json",
		requiredFields: [
			"schema",
			"task_id",
			"task_version",
			"dataset_version",
			"audio_version",
			"metrics",
			"analysis_unit",
			"excluded_candidates",
			"model_candidate_keys",
			"standards_by_stage",
			"status",
		],
		allowedValues: {
			schema: ["sure.audio_eval.metric_plan.v1"],
			analysis_unit: ["model_condition"],
			status: ["success", "failed"],
		},
		forbiddenFields: ["objective_rows", "condition_points", "report_persisted"],
		helperScripts: ["resolve_metric_plan.py", "check_audio_eval.py"],
		gateScript: "check_audio_eval.py",
		gateScriptArgs: () => ["--unit", "resolve_metrics"],
		gateInputs: ["evaluation_context.json", "task_manifest_resolved.json"],
	},
	{
		id: "validate_audio",
		label: "Validate audio conditions",
		kind: "gate",
		produces: "audio_manifest.json",
		schemaRef: "audio_manifest.schema.json",
		requiredFields: ["schema", "task_id", "audio_version", "items", "validated"],
		allowedValues: { schema: ["sure.audio_eval.audio_manifest.v1"], validated: [true] },
		forbiddenFields: ["objective_rows", "subjective_rows", "report_persisted"],
		gateScript: "check_audio_eval.py",
		gateScriptArgs: () => ["--unit", "validate_audio"],
		helperScripts: ["check_audio_eval.py"],
		gateInputs: ["evaluation_context.json", "task_manifest_resolved.json"],
	},
	{
		id: "run_objective",
		label: "Run objective metrics",
		kind: "gate",
		produces: "objective_run_report.json",
		schemaRef: "objective_run_report.schema.json",
		requiredFields: ["schema", "task_id", "metric_rows_path", "metrics", "row_count", "engine_commit", "status"],
		allowedValues: { schema: ["sure.audio_eval.objective_run.v1"], status: ["success", "failed"] },
		forbiddenFields: ["subjective_rows", "condition_points", "report_persisted"],
		gateScript: "check_audio_eval.py",
		gateScriptArgs: () => ["--unit", "run_objective"],
		helperScripts: ["run_objective.py", "check_audio_eval.py"],
		gateInputs: ["evaluation_context.json", "objective_metric_plan.json", "audio_manifest.json"],
	},
	{
		id: "ingest_subjective",
		label: "Ingest subjective ratings",
		kind: "gate",
		produces: "subjective_ingest_report.json",
		schemaRef: "subjective_ingest_report.schema.json",
		requiredFields: [
			"schema",
			"task_id",
			"raw_sources",
			"dedup_path",
			"latest_valid_rule",
			"complete_groups",
			"status",
		],
		allowedValues: { schema: ["sure.audio_eval.subjective_ingest.v1"], status: ["success", "failed"] },
		forbiddenFields: ["condition_points", "report_persisted"],
		gateScript: "check_audio_eval.py",
		gateScriptArgs: () => ["--unit", "ingest_subjective"],
		helperScripts: ["ingest_subjective.py", "check_audio_eval.py"],
		gateInputs: ["evaluation_context.json", "task_manifest_resolved.json"],
	},
	{
		id: "aggregate_subjective",
		label: "Aggregate MOS and confidence intervals",
		kind: "gate",
		produces: "condition_summary.json",
		schemaRef: "condition_summary.schema.json",
		requiredFields: ["schema", "task_id", "analysis_unit", "mos_path", "ci95_path", "condition_count", "status"],
		allowedValues: { schema: ["sure.audio_eval.condition_summary.v1"], status: ["success", "failed"] },
		forbiddenFields: ["correlations", "report_persisted"],
		gateScript: "check_audio_eval.py",
		gateScriptArgs: () => ["--unit", "aggregate_subjective"],
		helperScripts: ["aggregate_subjective.py", "check_audio_eval.py"],
		gateInputs: ["subjective_ingest_report.json"],
	},
	{
		id: "joint_analysis",
		label: "Analyse model-condition correlations",
		kind: "gate",
		produces: "analysis_report.json",
		schemaRef: "analysis_report.schema.json",
		requiredFields: ["schema", "task_id", "correlation_unit", "correlations", "screening", "status"],
		allowedValues: { schema: ["sure.audio_eval.analysis.v1"], status: ["success", "failed"] },
		forbiddenFields: ["report_persisted"],
		gateScript: "check_audio_eval.py",
		gateScriptArgs: () => ["--unit", "joint_analysis"],
		helperScripts: ["screen_ratings.py", "analyze_conditions.py", "check_audio_eval.py"],
		gateInputs: ["objective_run_report.json", "condition_summary.json"],
	},
	{
		id: "render_report",
		label: "Render reproducible report",
		kind: "gate",
		produces: "main_agent_run_report.json",
		schemaRef: "run_report.schema.json",
		requiredFields: ["schema", "report_persisted", "analysis_html", "manifest_path", "status"],
		allowedValues: {
			schema: ["sure.audio_eval.run_report.v1"],
			report_persisted: [true],
			status: ["success", "failed"],
		},
		gateScript: "check_audio_eval.py",
		gateScriptArgs: () => ["--unit", "render_report"],
		helperScripts: ["render_report.py", "check_audio_eval.py"],
		gateInputs: ["evaluation_context.json", "tool_registry.json", "analysis_report.json", "final_manifest.json"],
	},
];

export const TOTAL_UNITS = MAIN_FLOW_UNITS.length;
export const FIRST_UNIT = MAIN_FLOW_UNITS[0];
export const LAST_UNIT = MAIN_FLOW_UNITS[MAIN_FLOW_UNITS.length - 1];

export function findUnit(unitId: string): Unit | undefined {
	return MAIN_FLOW_UNITS.find((unit) => unit.id === unitId);
}

export function nextUnit(unitId: string): Unit | undefined {
	const index = MAIN_FLOW_UNITS.findIndex((unit) => unit.id === unitId);
	return index >= 0 && index + 1 < MAIN_FLOW_UNITS.length ? MAIN_FLOW_UNITS[index + 1] : undefined;
}

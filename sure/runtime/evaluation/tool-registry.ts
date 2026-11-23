import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { canonicalJson, hashJson } from "./context.ts";

export const TOOL_REGISTRY_SCHEMA = "sure.audio_eval.tool_registry.v1";

export interface EvaluationTool {
	id: string;
	unit: string;
	script: string;
	inputs: string[];
	outputs: string[];
	mode: "deterministic" | "agent_read_only";
	requires_reference: boolean;
}

export interface ToolRegistry {
	schema: typeof TOOL_REGISTRY_SCHEMA;
	version: string;
	tools: EvaluationTool[];
	registry_fingerprint: string;
}

export const DEFAULT_EVALUATION_TOOLS: EvaluationTool[] = [
	{
		id: "validate_manifest",
		unit: "resolve_task",
		script: "scripts/check_audio_eval.py",
		inputs: ["task_manifest.json"],
		outputs: ["task_manifest_resolved.json"],
		mode: "deterministic",
		requires_reference: false,
	},
	{
		id: "resolve_metric_plan",
		unit: "resolve_metrics",
		script: "scripts/resolve_metric_plan.py",
		inputs: [
			"task_manifest_resolved.json",
			"evaluation_context.json",
			"engine-capabilities.generated.json",
			"references/STANDARDS.md",
		],
		outputs: ["objective_metric_plan.json"],
		mode: "deterministic",
		requires_reference: false,
	},
	{
		id: "validate_audio",
		unit: "validate_audio",
		script: "scripts/check_audio_eval.py",
		inputs: ["audio_manifest.json"],
		outputs: ["audio_validation.json"],
		mode: "deterministic",
		requires_reference: false,
	},
	{
		id: "score_objective",
		unit: "run_objective",
		script: "scripts/run_objective.py",
		inputs: ["audio_manifest.json", "objective_route_plan.json"],
		outputs: ["objective_scores.jsonl", "objective_run_report.json"],
		mode: "deterministic",
		requires_reference: false,
	},
	{
		id: "ingest_subjective",
		unit: "ingest_subjective",
		script: "scripts/ingest_subjective.py",
		inputs: ["task_manifest.json", "subjective_sources"],
		outputs: ["subjective_raw_manifest.json", "subjective_dedup.csv"],
		mode: "deterministic",
		requires_reference: false,
	},
	{
		id: "aggregate_mos",
		unit: "aggregate_subjective",
		script: "scripts/aggregate_subjective.py",
		inputs: ["subjective_dedup.csv"],
		outputs: ["condition_mos.csv", "condition_ci95.csv"],
		mode: "deterministic",
		requires_reference: false,
	},
	{
		id: "screen_ratings",
		unit: "joint_analysis",
		script: "scripts/screen_ratings.py",
		inputs: ["subjective_dedup.csv", "condition_mos.csv", "task_manifest_resolved.json"],
		outputs: ["screening_report.json"],
		mode: "deterministic",
		requires_reference: false,
	},
	{
		id: "analyze_conditions",
		unit: "joint_analysis",
		script: "scripts/analyze_conditions.py",
		inputs: ["condition_mos.csv", "objective_scores.jsonl"],
		outputs: ["analysis_report.json"],
		mode: "deterministic",
		requires_reference: false,
	},
	{
		id: "render_report",
		unit: "render_report",
		script: "scripts/render_report.py",
		inputs: ["evaluation_context.json", "tool_registry.json", "task_manifest_resolved.json", "audio_manifest.json", "subjective_dedup.csv", "condition_mos.csv", "condition_ci95.csv", "objective_scores.jsonl", "analysis_report.json"],
		outputs: ["analysis.html", "final_manifest.json", "main_agent_run_report.json"],
		mode: "deterministic",
		requires_reference: false,
	},
	{
		id: "publish_release",
		unit: "render_report",
		script: "scripts/check_audio_eval.py",
		inputs: ["analysis.html", "final_manifest.json"],
		outputs: ["main_agent_run_report.json"],
		mode: "deterministic",
		requires_reference: false,
	},
];

function registryPayload(tools: EvaluationTool[]): Omit<ToolRegistry, "registry_fingerprint"> {
	return {
		schema: TOOL_REGISTRY_SCHEMA,
		version: "1.0.0",
		tools: [...tools].sort((a, b) => a.id.localeCompare(b.id)),
	};
}

export function validateToolRegistry(registry: ToolRegistry): string[] {
	const errors: string[] = [];
	if (registry.schema !== TOOL_REGISTRY_SCHEMA) errors.push(`Unsupported registry schema: ${registry.schema}`);
	if (!Array.isArray(registry.tools)) return [...errors, "Tool registry tools must be an array."];
	const ids = new Set<string>();
	for (const tool of registry.tools) {
		if (!tool || typeof tool !== "object") {
			errors.push("Tool registry contains a non-object entry.");
			continue;
		}
		if (ids.has(tool.id)) errors.push(`Duplicate tool id: ${tool.id}`);
		ids.add(tool.id);
		if (!tool.unit || !tool.script || !Array.isArray(tool.outputs) || tool.outputs.length === 0)
			errors.push(`Tool ${tool.id} lacks unit, script, or outputs.`);
		if (!tool.script.startsWith("scripts/"))
			errors.push(`Tool ${tool.id} script must stay below scripts/: ${tool.script}`);
	}
	const expected = hashJson(registryPayload(registry.tools));
	if (registry.registry_fingerprint !== expected) errors.push(`Registry fingerprint mismatch: expected ${expected}`);
	return errors;
}

export function createToolRegistry(): ToolRegistry {
	const payload = registryPayload(DEFAULT_EVALUATION_TOOLS);
	return { ...payload, registry_fingerprint: hashJson(payload) };
}

export function writeToolRegistry(runDir: string): ToolRegistry {
	const registry = createToolRegistry();
	const path = join(runDir, "artifacts", "tool_registry.json");
	mkdirSync(join(runDir, "artifacts"), { recursive: true });
	const temporary = `${path}.tmp`;
	writeFileSync(temporary, `${JSON.stringify(registry, null, 2)}\n`, "utf8");
	renameSync(temporary, path);
	return registry;
}

export function readToolRegistry(runDir: string): ToolRegistry | undefined {
	const path = join(runDir, "artifacts", "tool_registry.json");
	if (!existsSync(path)) return undefined;
	try {
		const value: unknown = JSON.parse(readFileSync(path, "utf8"));
		return value && typeof value === "object" && !Array.isArray(value) ? (value as ToolRegistry) : undefined;
	} catch {
		return undefined;
	}
}

export function registeredScript(registry: ToolRegistry, script: string, unit: string): boolean {
	return registry.tools.some((tool) => tool.script === script && tool.unit === unit);
}

export function registrySummary(registry: ToolRegistry): string {
	return canonicalJson({
		schema: registry.schema,
		version: registry.version,
		tools: registry.tools.map((tool) => tool.id),
	});
}

import { existsSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import type { SureRunRecord } from "./types.ts";

const MAX_CONTEXT_CHARS = 5000;

function readJson(path: string): Record<string, unknown> | undefined {
	if (!existsSync(path)) return undefined;
	try {
		const value: unknown = JSON.parse(readFileSync(path, "utf8"));
		return typeof value === "object" && value !== null && !Array.isArray(value)
			? (value as Record<string, unknown>)
			: undefined;
	} catch {
		return undefined;
	}
}

function compact(value: unknown, limit = 900): string {
	const text = JSON.stringify(value);
	return text.length <= limit ? text : `${text.slice(0, limit - 3)}...`;
}

/**
 * Build the small, durable context summary injected into every SURE turn.
 * Large CSVs and metric tables stay on disk; the prompt carries identities,
 * current checkpoint and repair-relevant artifact paths only.
 */
export function buildSureContext(run: SureRunRecord): string {
	const context = readJson(join(run.runDir, "artifacts", "evaluation_context.json"));
	const registry = readJson(join(run.runDir, "artifacts", "tool_registry.json"));
	const loop = readJson(join(run.runDir, "artifacts", "loop_guard.json"));
	const state = readJson(join(run.runDir, "state.json"));
	const checkpoint = state?.checkpoint;
	const lines = [
		"<sure_context>",
		`run_id=${run.runId}`,
		`run_dir=${relative(run.cwd, run.runDir) || "."}`,
		`checkpoint=${compact(checkpoint ?? null)}`,
		`evaluation_context=${compact(context ?? { missing: true })}`,
		`tool_registry=${compact(registry ?? { missing: true })}`,
		`loop_guard=${compact(loop ?? { missing: true })}`,
		"Read the referenced artifacts before acting. Keep raw score tables and objective rows on disk; do not paste them into the conversation.",
		"</sure_context>",
	];
	const summary = lines.join("\n");
	return summary.length <= MAX_CONTEXT_CHARS ? summary : `${summary.slice(0, MAX_CONTEXT_CHARS - 3)}...`;
}

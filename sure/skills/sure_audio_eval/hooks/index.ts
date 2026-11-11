import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import type { SureHookContext, SureHookResult } from "@earendil-works/pi-coding-agent/hooks";
import { invokedSkillScripts } from "../../../runtime/script-guard.ts";
import {
	writeOrVerifyEvaluationContext,
} from "../../../runtime/evaluation/context.ts";
import {
	readToolRegistry,
	registrySummary,
	validateToolRegistry,
	writeToolRegistry,
} from "../../../runtime/evaluation/tool-registry.ts";
import { artifactFingerprint, observeLoop } from "../../../runtime/evaluation/loop-guard.ts";
import {
	advance,
	artifactParseError,
	artifactPath,
	bumpRetry,
	type CheckpointData,
	failure,
	readArtifact,
	readCheckpoint,
	retryExhausted,
	runBackend,
} from "./checkpoints.ts";
import { FIRST_UNIT, findUnit, LAST_UNIT, MAIN_FLOW_UNITS, TOTAL_UNITS, type Unit } from "./state-machine.ts";
import { validateProduces } from "./validate.ts";
import { semanticGate } from "./gates.ts";

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseArgs(raw: string): Record<string, string> {
	const result: Record<string, string> = {};
	const tokens = raw.trim().split(/\s+/).filter(Boolean);
	for (let index = 0; index < tokens.length; index += 1) {
		const token = tokens[index];
		const equal = token.indexOf("=");
		if (equal >= 0) {
			result[token.slice(0, equal)] = token.slice(equal + 1);
			continue;
		}
		const key = token.replace(/^--?/, "");
		const next = tokens[index + 1];
		if (next && !next.startsWith("-")) {
			result[key] = next;
			index += 1;
		} else {
			result[key] = "true";
		}
	}
	return result;
}

function listArg(value: string | undefined): string[] {
	return (value ?? "").split(",").map((entry) => entry.trim()).filter(Boolean);
}

function lockedCapabilityHash(cwd: string): string | undefined {
	const path = join(cwd, "sure/runtime/evaluation/engine-capabilities.generated.json");
	if (!existsSync(path)) return undefined;
	return createHash("sha256").update(readFileSync(path)).digest("hex");
}

function phaseFor(unit: Unit, status: "running" | "blocked" | "success") {
	return { id: unit.id, label: unit.label, status };
}

function countersFor(checkpoint: CheckpointData, gateBlocks = 0): Record<string, number> {
	const retryBlocks = Object.values(checkpoint.retries).reduce((sum, value) => sum + value, 0);
	return {
		completed_units: checkpoint.completedUnits.length,
		total_units: TOTAL_UNITS,
		gate_blocks: Math.max(checkpoint.blocks ?? 0, retryBlocks, gateBlocks),
	};
}

function currentUnit(ctx: SureHookContext): Unit | undefined {
	return findUnit(readCheckpoint(ctx).data.currentUnit);
}

function toolName(ctx: SureHookContext): string {
	const event = isRecord(ctx.event) ? ctx.event : {};
	return typeof event.toolName === "string" ? event.toolName : "unknown";
}

function toolInput(ctx: SureHookContext): unknown {
	const event = isRecord(ctx.event) ? ctx.event : {};
	return event.input ?? null;
}

export function preStart(ctx: SureHookContext): SureHookResult {
	const args = parseArgs(ctx.args);
	const missing = ["task_manifest", "subjective_source", "objective_source", "objective_metrics"].filter((key) => !args[key]);
	if (missing.length > 0) {
		return failure(
			`Missing required /sure_audio_eval parameter(s): ${missing.join(", ")}. Provide task_manifest, subjective_source, objective_source and objective_metrics.`,
			"Audio evaluation inputs are not fully specified.",
		);
	}
	try {
		const context = writeOrVerifyEvaluationContext({
			cwd: ctx.cwd,
			runId: ctx.run.runId,
			taskManifest: args.task_manifest,
			subjectiveSources: listArg(args.subjective_source),
			objectiveSources: listArg(args.objective_source),
			metrics: listArg(args.objective_metrics),
			analysisUnit: args.analysis_unit ?? "model_condition",
			standards: ["ITU-T P.1401", "ITU-R BS.1284-2", "P.808", "ITU-R BS.1770", "ETSI TS 103 281 V1.3.1"],
			standardsByStage: {
				resolve_task: ["ITU-R BS.1284-2", "P.808"],
				resolve_metrics: ["ITU-R BS.1284-2", "P.808"],
				validate_audio: ["ITU-R BS.1770"],
				run_objective: ["ITU-T P.1401", "NIST Statistical Handbook"],
				ingest_subjective: ["ITU-T P.1401"],
				aggregate_subjective: ["ITU-T P.1401", "NIST Statistical Handbook"],
				joint_analysis: ["ITU-T P.1401", "NIST Statistical Handbook"],
				screening: ["ETSI TS 103 281 V1.3.1"],
				render_report: ["ETSI TS 103 281 V1.3.1"],
			},
			codeRevision: args.code_revision ?? process.env.SURE_HARNESS_CODE_REVISION,
			engineCommit: args.engine_commit ?? process.env.SURE_EVAL_ENGINE_COMMIT,
			capabilityManifestSha256: args.capabilities_sha256 ?? process.env.SURE_EVAL_CAPABILITIES_SHA256 ?? lockedCapabilityHash(ctx.cwd),
		});
		const registry = writeToolRegistry(ctx.run.runDir);
		const checkpoint = readCheckpoint(ctx);
		return {
			ok: true,
			state_patch: {
				phase: phaseFor(findUnit(checkpoint.data.currentUnit) ?? FIRST_UNIT, "running"),
				message: `Audio evaluation context frozen (${context.input_fingerprint.slice(0, 12)}); ${registry.tools.length} registered tools.`,
				counters: countersFor(checkpoint.data),
				checkpoint,
				artifacts: [
					{ type: "evaluation_context", name: "Evaluation context", path: `.sure/runs/${ctx.run.runId}/artifacts/evaluation_context.json`, status: "ready" },
					{ type: "tool_registry", name: "Tool registry", path: `.sure/runs/${ctx.run.runId}/artifacts/tool_registry.json`, status: "ready", summary: registrySummary(registry) },
				],
			},
		};
	} catch (error) {
		return failure(error instanceof Error ? error.message : String(error), "Could not freeze audio evaluation context.");
	}
}

export function preToolCall(ctx: SureHookContext): SureHookResult {
	const registry = readToolRegistry(ctx.run.runDir);
	if (!registry) return failure("tool_registry.json is missing; restart the run to freeze the registered tools.", "Tool registry unavailable.");
	const registryErrors = validateToolRegistry(registry);
	if (registryErrors.length > 0) return failure(registryErrors.join("; "), "Tool registry validation failed.");
	const event = isRecord(ctx.event) ? ctx.event : {};
	if (event.toolName !== "bash") return { ok: true };
	const input = isRecord(event.input) ? event.input : {};
	const command = typeof input.command === "string" ? input.command : "";
	const scripts = invokedSkillScripts(command, "sure_audio_eval/scripts");
	if (scripts.length === 0) return { ok: true };
	const unit = currentUnit(ctx);
	const allowedUnits = new Set([unit?.id, ...readCheckpoint(ctx).data.completedUnits]);
	for (const script of scripts) {
		const normalized = script.startsWith("scripts/") ? script : `scripts/${script.split("/").pop()}`;
		const owner = registry.tools.find((entry) => entry.script === normalized);
		if (!owner || !allowedUnits.has(owner.unit)) {
			return failure(
				`Script ${script} is not registered for the current or completed units. Use only scripts declared in tool_registry.json.`,
				"Unregistered or out-of-order evaluation tool.",
			);
		}
	}
	return { ok: true };
}

function failOrRetry(ctx: SureHookContext, unit: Unit, checkpoint: ReturnType<typeof readCheckpoint>, repair: string, reason: string): SureHookResult {
	const next = bumpRetry(unit, checkpoint.data);
	const attempts = next.data.retries[unit.id] ?? 1;
	const terminal = retryExhausted(unit, next.data, 3);
	const message = terminal ? `Gate "${unit.id}" exhausted retries: ${reason}` : `Gate "${unit.id}" blocked (attempt ${attempts}): ${reason}`;
	return {
		ok: !terminal,
		repair: terminal ? `${repair} Repair the input or finish with status failed.` : repair,
		state_patch: {
			phase: phaseFor(unit, "blocked"),
			message,
			counters: countersFor(next.data, attempts),
			checkpoint: next,
			diagnostics: [{ severity: terminal ? "error" : "warning", message: reason, repair }],
		},
	};
}

function runGateScript(ctx: SureHookContext, unit: Unit): { ok: boolean; repair?: string; reason?: string } {
	if (!unit.gateScript) return { ok: true };
	const result = runBackend(ctx, unit.gateScript, ["--produces", artifactPath(ctx, unit.produces), ...(unit.gateScriptArgs ? unit.gateScriptArgs(ctx) : [])]);
	if (result.ok) return { ok: true };
	return { ok: false, repair: result.stderr.trim() || result.stdout.trim() || `Gate script ${unit.gateScript} failed.`, reason: `gate script ${unit.gateScript} failed` };
}

export function postToolResult(ctx: SureHookContext): SureHookResult {
	const unit = currentUnit(ctx);
	if (!unit) return failure("Checkpoint points at an unknown unit.", "Lost audio evaluation state-machine position.");
	const loop = observeLoop(ctx.run.runDir, {
		unit: unit.id,
		tool_name: toolName(ctx),
		input: toolInput(ctx),
		result_error: isRecord(ctx.event) && ctx.event.isError === true ? "tool_error" : null,
		artifact_fingerprint: artifactFingerprint(ctx.run.runDir),
	});
	if (loop.triggered) {
		return failure(
			`${loop.message} Repair the input or change the tool action before resuming.`,
			"Loop guard stopped the run.",
			countersFor(readCheckpoint(ctx).data, 1),
			readCheckpoint(ctx),
		);
	}
	if (isRecord(ctx.event) && ctx.event.isError === true) return { ok: true };
	const checkpoint = readCheckpoint(ctx);
	const artifact = readArtifact(ctx, unit.produces);
	if (artifact === undefined) {
		const parseError = artifactParseError(ctx, unit.produces);
		return parseError ? failOrRetry(ctx, unit, checkpoint, `${unit.produces} is invalid JSON: ${parseError}`, "invalid JSON") : { ok: true };
	}
	const structural = validateProduces(ctx, unit, artifact);
	if (!structural.ok) return structural.missing ? { ok: true } : failOrRetry(ctx, unit, checkpoint, structural.repair ?? "Repair the artifact.", structural.reason ?? "schema gate failed");
	const semantic = semanticGate(unit.id, artifact, ctx);
	if (!semantic.ok) return failOrRetry(ctx, unit, checkpoint, semantic.repair ?? "Repair the artifact.", semantic.reason ?? "semantic gate failed");
	const backend = runGateScript(ctx, unit);
	if (!backend.ok) return failOrRetry(ctx, unit, checkpoint, backend.repair ?? "Repair the artifact.", backend.reason ?? "backend gate failed");
	const next = advance(unit, checkpoint.data);
	if (!next) return { ok: true };
	return {
		ok: true,
		state_patch: {
			phase: phaseFor(findUnit(next.data.currentUnit) ?? LAST_UNIT, "running"),
			message: `Advanced to ${next.data.currentUnit}.`,
			counters: countersFor(next.data),
			checkpoint: next,
		},
	};
}

export function preFinish(ctx: SureHookContext): SureHookResult {
	const checkpoint = readCheckpoint(ctx);
	try {
		const loop = JSON.parse(readFileSync(join(ctx.runDir, "artifacts", "loop_guard.json"), "utf8")) as { triggered?: boolean };
		if (loop.triggered === true) return failure("loop_guard.json records a repeated no-progress action. Change the input or repair cause before finishing.", "Loop guard is active.", countersFor(checkpoint.data), checkpoint);
	} catch {
		// No loop record is the normal state.
	}
	const report = readArtifact(ctx, LAST_UNIT.produces);
	if (checkpoint.data.currentUnit !== LAST_UNIT.id || !checkpoint.data.completedUnits.includes(LAST_UNIT.id)) return failure(`Complete the audio evaluation state machine before finishing; current unit is ${checkpoint.data.currentUnit}.`, "Run finished before all gates passed.", countersFor(checkpoint.data), checkpoint);
	if (report === undefined) return failure(`Produce ${LAST_UNIT.produces} before calling sure_finish.`, "Final audio evaluation report is missing.", countersFor(checkpoint.data), checkpoint);
	const structural = validateProduces(ctx, LAST_UNIT, report);
	if (!structural.ok) return failure(structural.repair ?? "Final report is invalid.", "Final report schema gate failed.", countersFor(checkpoint.data), checkpoint);
	const semantic = semanticGate(LAST_UNIT.id, report, ctx);
	if (!semantic.ok) return failure(semantic.repair ?? "Final report is invalid.", "Final report semantic gate failed.", countersFor(checkpoint.data), checkpoint);
	const backend = runGateScript(ctx, LAST_UNIT);
	if (!backend.ok) return failure(backend.repair ?? "Final report deterministic gate failed.", "Final report deterministic gate failed.", countersFor(checkpoint.data), checkpoint);
	return {
		ok: true,
		state_patch: {
			phase: { id: LAST_UNIT.id, label: LAST_UNIT.label, status: "success", progress: 1 },
			message: "Audio evaluation artifacts validated and ready for release.",
			counters: countersFor(checkpoint.data),
			checkpoint,
		},
	};
}

export function postFinish(ctx: SureHookContext): SureHookResult {
	return { ok: true, state_patch: { phase: { id: "finish", label: "Audio evaluation finished", status: ctx.run.status, progress: 1 }, message: ctx.run.summary ?? "Audio evaluation run finished." } };
}

export function onError(ctx: SureHookContext): SureHookResult {
	return { ok: true, state_patch: { phase: { id: "error", label: "Audio evaluation interrupted", status: "failed" }, message: ctx.run.errorSummary ?? ctx.run.lastRepair ?? "Audio evaluation stopped before completion." } };
}

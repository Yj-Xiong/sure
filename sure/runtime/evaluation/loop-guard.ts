import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync, statSync, writeFileSync, mkdirSync, renameSync } from "node:fs";
import { join } from "node:path";
import { canonicalJson } from "./context.ts";

export const LOOP_GUARD_SCHEMA = "sure.audio_eval.loop_guard.v1";
const SAME_SIGNATURE_LIMIT = 3;
const NO_PROGRESS_LIMIT = 8;

export interface LoopObservation {
	unit: string;
	tool_name: string;
	input?: unknown;
	result_error?: string | null;
	artifact_fingerprint: string;
}

export interface LoopState {
	schema: typeof LOOP_GUARD_SCHEMA;
	last_signature: string | null;
	last_artifact_fingerprint?: string | null;
	last_unit?: string | null;
	consecutive_same: number;
	tool_calls_since_progress: number;
	total_tool_calls: number;
	triggered: boolean;
	trigger?: "same_signature" | "no_progress";
	last_observation?: LoopObservation;
}

function sha256(value: string): string {
	return createHash("sha256").update(value).digest("hex");
}

function statePath(runDir: string): string {
	return join(runDir, "artifacts", "loop_guard.json");
}

function readState(runDir: string): LoopState {
	try {
		const value = JSON.parse(readFileSync(statePath(runDir), "utf8")) as LoopState;
		if (value.schema === LOOP_GUARD_SCHEMA) return value;
	} catch {
		// A missing or malformed progress record starts a fresh guard; the actual
		// required artifacts remain protected by the skill gates.
	}
	return {
		schema: LOOP_GUARD_SCHEMA,
		last_signature: null,
		last_artifact_fingerprint: null,
		last_unit: null,
		consecutive_same: 0,
		tool_calls_since_progress: 0,
		total_tool_calls: 0,
		triggered: false,
	};
}

function writeState(runDir: string, state: LoopState): void {
	mkdirSync(join(runDir, "artifacts"), { recursive: true });
	const path = statePath(runDir);
	const temporary = `${path}.tmp`;
	writeFileSync(temporary, `${JSON.stringify(state, null, 2)}\n`, "utf8");
	renameSync(temporary, path);
}

export function artifactFingerprint(runDir: string): string {
	const root = join(runDir, "artifacts");
	const files: string[] = [];
	const walk = (dir: string, prefix: string): void => {
		if (!existsSync(dir)) return;
		for (const entry of readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
			const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
			const path = join(dir, entry.name);
			if (entry.isDirectory()) walk(path, relative);
			else if (entry.name !== "loop_guard.json" && statSync(path).isFile()) {
				files.push(`${relative}:${sha256(readFileSync(path).toString())}`);
			}
		}
	};
	walk(root, "");
	return sha256(files.join("\n"));
}

export interface LoopCheck {
	triggered: boolean;
	state: LoopState;
	message?: string;
}

export function observeLoop(runDir: string, observation: LoopObservation): LoopCheck {
	const state = readState(runDir);
	const signature = sha256(canonicalJson({
		unit: observation.unit,
		tool_name: observation.tool_name,
		input: observation.input ?? null,
		result_error: observation.result_error ?? null,
		artifact_fingerprint: observation.artifact_fingerprint,
	}));
	const same = state.last_signature === signature;
	const progress = state.last_artifact_fingerprint !== observation.artifact_fingerprint || state.last_unit !== observation.unit;
	const next: LoopState = {
		...state,
		last_signature: signature,
		consecutive_same: same ? state.consecutive_same + 1 : 1,
		last_artifact_fingerprint: observation.artifact_fingerprint,
		last_unit: observation.unit,
		tool_calls_since_progress: progress ? 0 : state.tool_calls_since_progress + 1,
		total_tool_calls: state.total_tool_calls + 1,
		last_observation: observation,
	};
	if (state.triggered && progress) {
		next.triggered = false;
		next.trigger = undefined;
		next.consecutive_same = 1;
	}
	if (next.consecutive_same >= SAME_SIGNATURE_LIMIT) {
		next.triggered = true;
		next.trigger = "same_signature";
	}
	if (next.tool_calls_since_progress >= NO_PROGRESS_LIMIT) {
		next.triggered = true;
		next.trigger = "no_progress";
	}
	writeState(runDir, next);
	return next.triggered
		? { triggered: true, state: next, message: `Loop guard stopped repeated ${next.trigger} in unit ${observation.unit}.` }
		: { triggered: false, state: next };
}

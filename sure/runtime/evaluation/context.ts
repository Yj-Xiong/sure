import { createHash } from "node:crypto";
import { existsSync, readFileSync, renameSync, mkdirSync, writeFileSync, readdirSync, statSync } from "node:fs";
import { isAbsolute, relative, resolve, join, dirname } from "node:path";

export const EVALUATION_CONTEXT_SCHEMA = "sure.audio_eval.context.v1";

export interface EvaluationSourceFile {
	path: string;
	kind: "task_manifest" | "subjective" | "objective" | "reference" | "other";
	sha256: string;
}

export interface EvaluationContextInput {
	cwd: string;
	runId: string;
	taskManifest: string;
	subjectiveSources: string[];
	objectiveSources: string[];
	metrics: string[];
	analysisUnit?: string;
	standards?: string[];
	standardsByStage?: Record<string, string[]>;
	codeRevision?: string;
	engineCommit?: string;
	capabilityManifestSha256?: string;
}

export interface EvaluationContext {
	schema: typeof EVALUATION_CONTEXT_SCHEMA;
	run_id: string;
	task_manifest: string;
	metrics: string[];
	analysis_unit: string;
	standards: string[];
	standards_by_stage: Record<string, string[]>;
	code_revision: string | null;
	engine_commit: string | null;
	capability_manifest_sha256: string | null;
	source_files: EvaluationSourceFile[];
	policy: {
		latest_valid_key: string;
		condition_point: string;
		correlation: string[];
		correlation_unit: string;
		excluded_candidates: string[];
		fit: string;
		screening: string;
	};
	input_fingerprint: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function canonical(value: unknown): string {
	if (Array.isArray(value)) {
		return `[${value.map(canonical).join(",")}]`;
	}
	if (isRecord(value)) {
		return `{${Object.keys(value)
			.sort()
			.map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`)
			.join(",")}}`;
	}
	return JSON.stringify(value);
}

function sha256(value: string | Buffer): string {
	return createHash("sha256").update(value).digest("hex");
}

function inside(base: string, candidate: string): boolean {
	const path = relative(base, candidate);
	return path === "" || (!path.startsWith("..") && !isAbsolute(path));
}

export function resolveEvaluationInput(cwd: string, value: string): string {
	const resolved = resolve(cwd, value);
	if (!inside(cwd, resolved)) {
		throw new Error(`Evaluation input escapes the project root: ${value}`);
	}
	if (!existsSync(resolved)) {
		throw new Error(`Evaluation input does not exist: ${value}`);
	}
	return resolved;
}

function digestPath(path: string): string {
	if (statSync(path).isFile()) return sha256(readFileSync(path));
	const entries: string[] = [];
	const walk = (directory: string, prefix: string): void => {
		for (const entry of readdirSync(directory, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
			const child = join(directory, entry.name);
			const childRelative = prefix ? `${prefix}/${entry.name}` : entry.name;
			if (entry.isDirectory()) walk(child, childRelative);
			else entries.push(`${childRelative}:${sha256(readFileSync(child))}`);
		}
	};
	walk(path, "");
	return sha256(entries.join("\n"));
}

function sourceFile(cwd: string, value: string, kind: EvaluationSourceFile["kind"]): EvaluationSourceFile {
	const path = resolveEvaluationInput(cwd, value);
	return { path: relative(cwd, path) || ".", kind, sha256: digestPath(path) };
}

function writeJsonAtomic(path: string, value: unknown): void {
	mkdirSync(dirname(path), { recursive: true });
	const temporary = `${path}.tmp`;
	writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, "utf8");
	renameSync(temporary, path);
}

export function contextPath(runDir: string): string {
	return join(runDir, "artifacts", "evaluation_context.json");
}

export function buildEvaluationContext(input: EvaluationContextInput): EvaluationContext {
	const sources: EvaluationSourceFile[] = [sourceFile(input.cwd, input.taskManifest, "task_manifest")];
	for (const path of input.subjectiveSources) sources.push(sourceFile(input.cwd, path, "subjective"));
	for (const path of input.objectiveSources) sources.push(sourceFile(input.cwd, path, "objective"));
	const contextWithoutFingerprint: Omit<EvaluationContext, "input_fingerprint"> = {
		schema: EVALUATION_CONTEXT_SCHEMA,
		run_id: input.runId,
		task_manifest: relative(input.cwd, resolveEvaluationInput(input.cwd, input.taskManifest)) || ".",
		metrics: [...new Set(input.metrics)].sort(),
		analysis_unit: input.analysisUnit ?? "model_condition",
		standards: [...new Set(input.standards ?? ["ITU-T P.1401", "ITU-R BS.1284-2", "P.808", "ITU-R BS.1770", "ETSI TS 103 281 V1.3.1"])].sort(),
		standards_by_stage: input.standardsByStage ?? {
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
		code_revision: input.codeRevision ?? null,
		engine_commit: input.engineCommit ?? null,
		capability_manifest_sha256: input.capabilityManifestSha256 ?? null,
		source_files: sources,
		policy: {
			latest_valid_key: "task_id × listener_id × sample_id × candidate_key",
			condition_point: "sample_id × candidate_key; listener scores are averaged before correlation",
			correlation: ["Pearson", "Spearman"],
			correlation_unit: "model_condition",
			excluded_candidates: ["hidden_reference", "unprocessed"],
			fit: "not used for model-condition MOS-to-metric correlations",
			screening: "versioned rating-level sensitivity analysis; never objective-guided deletion",
		},
	};
	const fingerprint = sha256(canonical(contextWithoutFingerprint));
	return { ...contextWithoutFingerprint, input_fingerprint: fingerprint };
}

export function writeOrVerifyEvaluationContext(input: EvaluationContextInput): EvaluationContext {
	const context = buildEvaluationContext(input);
	const path = contextPath(resolve(input.cwd, ".sure/runs", input.runId));
	if (existsSync(path)) {
		let existing: unknown;
		try {
			existing = JSON.parse(readFileSync(path, "utf8"));
		} catch {
			throw new Error(`Evaluation context is not valid JSON: ${path}`);
		}
		if (!isRecord(existing) || existing.input_fingerprint !== context.input_fingerprint) {
			throw new Error("Evaluation context drift detected; change the inputs explicitly before resuming.");
		}
		return existing as unknown as EvaluationContext;
	}
	writeJsonAtomic(path, context);
	return context;
}

export function readEvaluationContext(runDir: string): EvaluationContext | undefined {
	const path = contextPath(runDir);
	if (!existsSync(path)) return undefined;
	try {
		const value: unknown = JSON.parse(readFileSync(path, "utf8"));
		return isRecord(value) && value.schema === EVALUATION_CONTEXT_SCHEMA
			? (value as unknown as EvaluationContext)
			: undefined;
	} catch {
		return undefined;
	}
}

export function canonicalJson(value: unknown): string {
	return canonical(value);
}

export function hashJson(value: unknown): string {
	return sha256(canonical(value));
}

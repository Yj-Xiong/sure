#!/usr/bin/env python3
"""Independent integrity checks for the subjective audio-evaluation stages.

The stage scripts produce tables.  These checks read their sources again and
recompute the values that control release, which keeps a stale report from
turning into a successful gate.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


SCREENING_RULE_VERSION = "rating-level-3iqr-median-deviation-v1"
DEFAULT_EXCLUDED = {"hidden_reference", "unprocessed"}
REQUIRED_RAW = {
    "task_id", "task_version", "dataset_version", "audio_version", "listener_id",
    "session_id", "device_type", "round", "submission_kind", "sample_id", "candidate_key",
    "candidate_position", "score", "scale_max", "submitted_at", "audio_sha256",
}


def _fail(message: str) -> None:
    raise ValueError(message)


def _load(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _fail("cannot read JSON artifact {}: {}".format(path, exc))
    if not isinstance(value, dict):
        _fail("JSON artifact must be an object: {}".format(path))
    return value


def _resolve(value: Any, run_dir: Path) -> Path:
    if not isinstance(value, str) or not value:
        _fail("artifact path is missing")
    path = Path(value)
    if path.is_absolute():
        return path
    candidates = [run_dir / path, run_dir / "artifacts" / path, run_dir.parent / path]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return candidates[0].resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        _fail("cannot read source {}: {}".format(path, exc))
    return digest.hexdigest()


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def normalize_listener(value: Any) -> str:
    """Normalize whitespace while preserving case-sensitive listener IDs."""
    return " ".join(_text(value).split())


def _task(run_dir: Path) -> Dict[str, Any]:
    path = run_dir / "artifacts" / "task_manifest_resolved.json"
    return _load(path) if path.is_file() else {}


def _audio(run_dir: Path) -> Dict[str, Any]:
    path = run_dir / "artifacts" / "audio_manifest.json"
    return _load(path) if path.is_file() else {}


def _manifest_samples(task: Dict[str, Any]) -> Set[str]:
    values = task.get("sample_ids")
    return {str(value) for value in values} if isinstance(values, list) else set()


def _manifest_candidates(task: Dict[str, Any]) -> List[str]:
    values = task.get("candidate_keys")
    if isinstance(values, list):
        return [str(value) for value in values]
    values = task.get("candidates")
    if isinstance(values, list):
        return [str(value.get("candidate_key")) for value in values if isinstance(value, dict) and value.get("candidate_key") is not None]
    return []


def _audio_version(task: Dict[str, Any]) -> Optional[str]:
    policy = task.get("audio_policy")
    if isinstance(policy, dict) and policy.get("audio_version") is not None:
        return str(policy["audio_version"])
    return None


def _audio_hashes(audio: Dict[str, Any]) -> Dict[Tuple[str, str], str]:
    result: Dict[Tuple[str, str], str] = {}
    values = audio.get("items")
    if not isinstance(values, list):
        return result
    for item in values:
        if isinstance(item, dict) and item.get("sample_id") is not None and item.get("candidate_key") is not None and item.get("audio_sha256"):
            result[(str(item["sample_id"]), str(item["candidate_key"]))] = str(item["audio_sha256"]).lower()
    return result


def _timestamp(value: Any) -> Optional[datetime]:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _scale(task: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    value = task.get("scale")
    if not isinstance(value, dict):
        return None, None
    return _number(value.get("min")), _number(value.get("max"))


def _valid_raw(row: Dict[str, str], task: Dict[str, Any], audio: Dict[str, Any], task_id: str) -> bool:
    if not REQUIRED_RAW.issubset(row):
        return False
    if row.get("task_id") != task_id:
        return False
    for field, expected in (("task_version", task.get("task_version")), ("dataset_version", task.get("dataset_version")), ("audio_version", _audio_version(task))):
        if expected is not None and row.get(field) != str(expected):
            return False
    samples = _manifest_samples(task)
    candidates = set(_manifest_candidates(task))
    if samples and row.get("sample_id") not in samples:
        return False
    if candidates and row.get("candidate_key") not in candidates:
        return False
    try:
        position = int(row.get("candidate_position", ""))
    except (TypeError, ValueError):
        return False
    ordered = _manifest_candidates(task)
    if ordered and (position < 1 or position > len(ordered) or ordered[position - 1] != row.get("candidate_key")):
        return False
    score = _number(row.get("score"))
    if score is None:
        return False
    low, high = _scale(task)
    if low is not None and score < low - 1e-9:
        return False
    if high is not None and score > high + 1e-9:
        return False
    scale_max = _number(row.get("scale_max"))
    if high is not None and (scale_max is None or not math.isclose(scale_max, high, rel_tol=0.0, abs_tol=1e-9)):
        return False
    if not normalize_listener(row.get("listener_id")) or _timestamp(row.get("submitted_at")) is None:
        return False
    audio_hash = _text(row.get("audio_sha256")).lower()
    expected_hash = _audio_hashes(audio).get((row.get("sample_id", ""), row.get("candidate_key", "")))
    return len(audio_hash) == 64 and all(character in "0123456789abcdef" for character in audio_hash) and (expected_hash is None or audio_hash == expected_hash)


def _raw_sources(artifact: Dict[str, Any], run_dir: Path, task: Dict[str, Any], audio: Dict[str, Any]) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], List[Dict[str, str]]]:
    raw_sources = artifact.get("raw_sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        _fail("raw subjective sources are missing")
    valid: List[Dict[str, str]] = []
    invalid: List[Dict[str, str]] = []
    all_rows: List[Dict[str, str]] = []
    task_id = _text(artifact.get("task_id"))
    source_order = 0
    for source in raw_sources:
        if not isinstance(source, dict):
            _fail("raw source entry must be an object")
        path = _resolve(source.get("path"), run_dir)
        if not path.is_file():
            _fail("raw subjective source is missing: {}".format(path))
        declared_hash = _text(source.get("sha256")).lower()
        if len(declared_hash) != 64 or declared_hash != _sha256(path):
            _fail("raw subjective source hash mismatch: {}".format(path))
        try:
            with path.open(newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames:
                    _fail("raw subjective source has no header: {}".format(path))
                rows = list(reader)
        except OSError as exc:
            _fail("cannot read raw subjective source {}: {}".format(path, exc))
        if source.get("rows") is not None and int(source["rows"]) != len(rows):
            _fail("raw subjective source row count mismatch: {}".format(path))
        for raw in rows:
            row = {str(key): (_text(value)) for key, value in raw.items()}
            all_rows.append(row)
            row["_order"] = str(source_order)
            source_order += 1
            if _valid_raw(row, task, audio, task_id):
                row["listener_id"] = normalize_listener(row.get("listener_id"))
                valid.append(row)
            else:
                invalid.append(row)
    return valid, invalid, all_rows


def _dedup(valid: Iterable[Dict[str, str]]) -> Dict[Tuple[str, str, str, str], Dict[str, str]]:
    latest: Dict[Tuple[str, str, str, str], Dict[str, str]] = {}
    for row in valid:
        key = (row.get("task_id", ""), row.get("listener_id", ""), row.get("sample_id", ""), row.get("candidate_key", ""))
        previous = latest.get(key)
        current_time = _timestamp(row.get("submitted_at"))
        previous_time = _timestamp(previous.get("submitted_at")) if previous else None
        if previous is None or (current_time and previous_time and current_time > previous_time) or (current_time == previous_time and int(row.get("_order", "0")) > int(previous.get("_order", "0"))):
            latest[key] = row
    return latest


def _path_from_ingest(run_dir: Path) -> Optional[Path]:
    report_path = run_dir / "artifacts" / "subjective_ingest_report.json"
    if not report_path.is_file():
        return None
    report = _load(report_path)
    value = report.get("dedup_path")
    return _resolve(value, run_dir) if value else None


def check_subjective_ingest(artifact: Dict[str, Any], run_dir: Path) -> None:
    """Verify raw sources, latest-valid deduplication, completeness and rejection audit."""
    if not isinstance(artifact, dict):
        _fail("subjective ingest artifact must be an object")
    task = _task(run_dir)
    audio = _audio(run_dir)
    task_id = _text(artifact.get("task_id"))
    if not task_id:
        _fail("subjective ingest task_id is missing")
    if task.get("task_id") is not None and task_id != str(task["task_id"]):
        _fail("subjective ingest task_id differs from task manifest")
    for field, expected in (("task_version", task.get("task_version")), ("dataset_version", task.get("dataset_version")), ("audio_version", _audio_version(task))):
        if expected is not None and artifact.get(field) != str(expected):
            _fail("subjective ingest {} provenance differs from task manifest".format(field))
    dedup_path = _resolve(artifact.get("dedup_path"), run_dir)
    if not dedup_path.is_file():
        _fail("subjective dedup table is missing")
    valid, invalid, _ = _raw_sources(artifact, run_dir, task, audio)
    expected = _dedup(valid)
    with dedup_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            _fail("subjective dedup table has no header")
        rows = [{str(key): _text(value) for key, value in raw.items()} for raw in reader]
    seen: Set[Tuple[str, str, str, str]] = set()
    actual: Dict[Tuple[str, str, str, str], Dict[str, str]] = {}
    for row in rows:
        key = (row.get("task_id", ""), normalize_listener(row.get("listener_id")), row.get("sample_id", ""), row.get("candidate_key", ""))
        if key in seen:
            _fail("subjective dedup table contains duplicate identity: {}".format(key))
        seen.add(key)
        if not _valid_raw(row, task, audio, task_id):
            _fail("subjective dedup table contains an invalid row: {}".format(key))
        actual[key] = row
    candidates = set(_manifest_candidates(task))
    groups: Dict[Tuple[str, str], Set[str]] = {}
    for key in actual:
        groups.setdefault((key[1], key[2]), set()).add(key[3])
    if set(actual) != set(expected):
        _fail("subjective dedup table is not the latest-valid set")
    for key, row in actual.items():
        winner = expected[key]
        for field in ("submitted_at", "score", "audio_sha256", "task_version", "dataset_version", "audio_version", "session_id", "device_type", "round", "submission_kind", "candidate_position", "scale_max"):
            if row.get(field) != winner.get(field):
                _fail("subjective dedup row is not the latest valid provenance: {}".format(key))
        expected_complete = candidates and groups.get((key[1], key[2]), set()) == candidates
        if row.get("group_complete", "").lower() != ("true" if expected_complete else "false"):
            _fail("subjective dedup completeness marker is stale: {}".format(key))
    complete = sum(1 for values in groups.values() if candidates and values == candidates)
    if int(artifact.get("dedup_rows", len(rows))) != len(rows) or int(artifact.get("valid_rows", len(valid))) != len(valid):
        _fail("subjective ingest row counts are stale")
    if int(artifact.get("complete_groups", complete)) != complete:
        _fail("subjective ingest complete_groups is stale")
    invalid_path_value = artifact.get("invalid_rows_path")
    if invalid or invalid_path_value:
        if not invalid_path_value:
            _fail("rejected subjective rows need invalid_rows_path")
        invalid_path = _resolve(invalid_path_value, run_dir)
        if not invalid_path.is_file():
            _fail("invalid subjective row audit is missing")
        with invalid_path.open(newline="", encoding="utf-8-sig") as handle:
            rejected_rows = list(csv.DictReader(handle))
        if len(rejected_rows) != len(invalid) or int(artifact.get("invalid_rows", len(invalid))) != len(invalid):
            _fail("invalid subjective row audit is incomplete")
        if any(not _text(row.get("rejection_reason")) for row in rejected_rows):
            _fail("invalid subjective row audit is missing rejection reasons")
    status = _text(artifact.get("status"))
    if status in {"success", "success_with_rejections"} and not rows:
        _fail("subjective ingest cannot succeed with an empty dedup table")
    if status == "success" and invalid:
        _fail("subjective ingest with rejected rows must preserve success_with_rejections")


@lru_cache(maxsize=None)
def _t_critical_95(df: int) -> Optional[float]:
    if df < 1:
        return None
    def density(x: float) -> float:
        return math.exp(math.lgamma((df + 1) / 2.0) - math.lgamma(df / 2.0) - 0.5 * math.log(df * math.pi) - (df + 1) / 2.0 * math.log1p(x * x / df))
    def cdf(x: float) -> float:
        count = 512
        step = x / count
        total = density(0.0) + density(x)
        for index in range(1, count):
            total += (4.0 if index % 2 else 2.0) * density(index * step)
        return min(1.0, 0.5 + total * step / 3.0)
    low, high = 0.0, 16.0
    for _ in range(70):
        mid = (low + high) / 2.0
        if cdf(mid) < 0.975:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def _complete_score_groups(run_dir: Path, task: Dict[str, Any]) -> Dict[Tuple[str, str], List[float]]:
    ingest_path = _path_from_ingest(run_dir)
    if ingest_path is None or not ingest_path.is_file():
        _fail("subjective ingest report/dedup table is required")
    rows: List[Dict[str, str]] = []
    with ingest_path.open(newline="", encoding="utf-8-sig") as handle:
        rows = [{str(key): _text(value) for key, value in raw.items()} for raw in csv.DictReader(handle)]
    candidates = set(_manifest_candidates(task))
    grouped: Dict[Tuple[str, str], List[Dict[str, str]]] = {}
    for row in rows:
        listener = normalize_listener(row.get("listener_id"))
        sample = row.get("sample_id", "")
        if listener and sample:
            grouped.setdefault((listener, sample), []).append(row)
    result: Dict[Tuple[str, str], List[float]] = {}
    for (listener, sample), values in grouped.items():
        present = {row.get("candidate_key", "") for row in values}
        if not candidates or present != candidates:
            continue
        for row in values:
            score = _number(row.get("score"))
            if score is None:
                _fail("complete subjective group contains a non-numeric score")
            result.setdefault((sample, row.get("candidate_key", "")), []).append(score)
    return result


def _table_rows(path: Path) -> List[Dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            return [{str(key): _text(value) for key, value in row.items()} for row in csv.DictReader(handle)]
    except OSError as exc:
        _fail("cannot read table {}: {}".format(path, exc))
    return []


def check_condition_summary(artifact: Dict[str, Any], run_dir: Path) -> None:
    """Recompute MOS, SD and Student-t confidence intervals from dedup rows."""
    if not isinstance(artifact, dict) or artifact.get("analysis_unit") != "sample_x_candidate":
        _fail("condition summary analysis_unit is invalid")
    task = _task(run_dir)
    if task.get("task_id") is not None and artifact.get("task_id") != task.get("task_id"):
        _fail("condition summary task_id differs from task manifest")
    ingest_report_path = run_dir / "artifacts" / "subjective_ingest_report.json"
    if ingest_report_path.is_file():
        check_subjective_ingest(_load(ingest_report_path), run_dir)
    expected = _complete_score_groups(run_dir, task)
    mos_path = _resolve(artifact.get("mos_path"), run_dir)
    ci_path = _resolve(artifact.get("ci95_path"), run_dir)
    if not mos_path.is_file() or not ci_path.is_file():
        _fail("condition summary tables are missing")
    mos_rows = _table_rows(mos_path)
    ci_rows = _table_rows(ci_path)
    mos_map: Dict[Tuple[str, str], Dict[str, str]] = {}
    ci_map: Dict[Tuple[str, str], Dict[str, str]] = {}
    for row in mos_rows:
        key = (row.get("sample_id", ""), row.get("candidate_key", ""))
        if key in mos_map:
            _fail("duplicate MOS condition: {}".format(key))
        mos_map[key] = row
    for row in ci_rows:
        key = (row.get("sample_id", ""), row.get("candidate_key", ""))
        if key in ci_map:
            _fail("duplicate CI condition: {}".format(key))
        ci_map[key] = row
    if set(mos_map) != set(expected) or set(ci_map) != set(expected):
        _fail("condition summaries do not cover exactly the complete listener groups")
    low, high = _scale(task)
    for key, values in expected.items():
        mos = mos_map[key]
        ci = ci_map[key]
        n = len(values)
        mean = sum(values) / n
        sd = statistics.stdev(values) if n > 1 else None
        actual_n = int(mos.get("n", "-1"))
        if actual_n != n or not math.isclose(float(mos.get("mos", "nan")), mean, rel_tol=0.0, abs_tol=1e-8):
            _fail("MOS does not match raw complete ratings: {}".format(key))
        if low is not None and mean < low - 1e-9 or high is not None and mean > high + 1e-9:
            _fail("MOS is outside the task scale: {}".format(key))
        if sd is None:
            if _text(mos.get("sd")) not in {"", "nan", "NaN"}:
                _fail("single-rating MOS must have an unavailable SD: {}".format(key))
            continue
        actual_sd = _number(mos.get("sd"))
        if actual_sd is None or not math.isclose(actual_sd, sd, rel_tol=0.0, abs_tol=1e-8):
            _fail("MOS standard deviation does not match raw ratings: {}".format(key))
        ci_n = int(ci.get("n", "-1"))
        if ci_n != n or _text(ci.get("available")).lower() not in {"true", "1"}:
            _fail("CI metadata is invalid: {}".format(key))
        critical = _t_critical_95(n - 1)
        margin = critical * sd / math.sqrt(n) if critical is not None else float("nan")
        ci_low, ci_high = _number(ci.get("ci95_low")), _number(ci.get("ci95_high"))
        if ci_low is None or ci_high is None or not math.isclose(ci_low, mean - margin, rel_tol=0.0, abs_tol=1e-7) or not math.isclose(ci_high, mean + margin, rel_tol=0.0, abs_tol=1e-7):
            _fail("CI95 does not match raw ratings: {}".format(key))
    if int(artifact.get("condition_count", -1)) != len(expected):
        _fail("condition_count is stale")
    if artifact.get("status") == "success" and not expected:
        _fail("condition summary cannot succeed with no conditions")
    if artifact.get("status") not in {"success", "failed"}:
        _fail("condition summary status is invalid")
    dataset = str(task.get("dataset_version")) if task.get("dataset_version") is not None else None
    audio_version = _audio_version(task)
    for row in mos_rows + ci_rows:
        if task.get("task_id") is not None and row.get("task_id") != str(task.get("task_id")):
            _fail("condition summary task provenance differs from task")
        if dataset is not None and row.get("dataset_version") != dataset:
            _fail("condition summary dataset provenance differs from task")
        if audio_version is not None and row.get("audio_version") != audio_version:
            _fail("condition summary audio provenance differs from task")


def _quantile(values: List[float], probability: float) -> float:
    ordered = sorted(values)
    index = probability * (len(ordered) - 1)
    low, high = math.floor(index), math.ceil(index)
    return ordered[low] if low == high else ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def _screen(rows: List[Dict[str, str]], models: Set[str], minimum_ratings: int) -> Tuple[Set[Tuple[str, str, str]], Dict[str, Any]]:
    groups: Dict[Tuple[str, str], List[Tuple[str, float]]] = {}
    for row in rows:
        if _text(row.get("group_complete")).lower() == "false" or row.get("candidate_key") not in models:
            continue
        score = _number(row.get("score"))
        listener = normalize_listener(row.get("listener_id"))
        if score is not None and listener:
            groups.setdefault((row.get("sample_id", ""), row.get("candidate_key", "")), []).append((listener, score))
    flagged: Set[Tuple[str, str, str]] = set()
    detail: List[Dict[str, Any]] = []
    for (sample, candidate), values in sorted(groups.items()):
        if len(values) < minimum_ratings:
            continue
        scores = [score for _, score in values]
        q1, q3 = _quantile(scores, 0.25), _quantile(scores, 0.75)
        if q3 <= q1:
            continue
        median = statistics.median(scores)
        bound = 3.0 * (q3 - q1)
        for listener, score in values:
            if (score < q1 - bound or score > q3 + bound) and abs(score - median) >= 20:
                flagged.add((listener, sample, candidate))
                detail.append({"sample_id": sample, "candidate_key": candidate, "listener_id": listener, "score": score, "reason": "3×IQR and 20-point median deviation"})
    return flagged, {"rule_version": SCREENING_RULE_VERSION, "minimum_ratings": minimum_ratings, "rule": "rating-level 3×IQR + n≥8 + median deviation≥20", "objective_independent": True, "flagged_count": len(flagged), "flagged": detail, "raw_scores_unchanged": True}


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    if len(xs) < 2:
        return None
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    den_x = math.sqrt(sum((value - mean_x) ** 2 for value in xs))
    den_y = math.sqrt(sum((value - mean_y) ** 2 for value in ys))
    if den_x == 0.0 or den_y == 0.0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / (den_x * den_y)


def _spearman(xs: List[float], ys: List[float]) -> Optional[float]:
    def ranks(values: List[float]) -> List[float]:
        order = sorted(range(len(values)), key=values.__getitem__)
        result = [0.0] * len(values)
        start = 0
        while start < len(order):
            end = start
            while end + 1 < len(order) and values[order[end + 1]] == values[order[start]]:
                end += 1
            rank = (start + end) / 2.0 + 1.0
            for index in range(start, end + 1):
                result[order[index]] = rank
            start = end + 1
        return result
    return _pearson(ranks(xs), ranks(ys)) if len(xs) >= 2 else None


def _objective_contract(rows: List[Dict[str, Any]], task: Dict[str, Any], plan: Dict[str, Any], audio: Dict[str, Any]) -> None:
    try:
        from objective_contract import validate_objective_rows
    except ImportError:
        return
    validate_objective_rows(rows, task, plan, audio)


def _objective_rows(run_dir: Path, task: Dict[str, Any], plan: Dict[str, Any], audio: Dict[str, Any]) -> Tuple[Dict[str, Dict[Tuple[str, str], float]], Dict[str, Dict[str, Any]]]:
    report_path = run_dir / "artifacts" / "objective_run_report.json"
    if not report_path.is_file():
        _fail("objective run report is required for analysis")
    report = _load(report_path)
    if report.get("status") != "success":
        _fail("objective run is not successful")
    rows_path = _resolve(report.get("metric_rows_path"), run_dir)
    if not rows_path.is_file():
        _fail("objective rows are missing")
    raw: List[Dict[str, Any]] = []
    with rows_path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    _fail("objective row must be an object")
                raw.append(value)
    if report.get("row_count") is not None and int(report.get("row_count")) != len(raw):
        _fail("objective row count is stale")
    _objective_contract(raw, task, plan, audio)
    planned = {str(item.get("metric")): item for item in plan.get("metrics", []) if isinstance(item, dict) and item.get("metric") and item.get("participates_in_model_correlation", True) is not False}
    values: Dict[str, Dict[Tuple[str, str], float]] = {}
    metadata: Dict[str, Dict[str, Any]] = {}
    samples, candidates = _manifest_samples(task), set(_manifest_candidates(task))
    for row in raw:
        if row.get("task_id") != task.get("task_id"):
            _fail("objective row task identity differs from the task manifest")
        for field, expected in (("task_version", task.get("task_version")), ("dataset_version", task.get("dataset_version")), ("audio_version", _audio_version(task))):
            if expected is not None and row.get(field) != expected:
                _fail("objective row {} identity differs from the task manifest".format(field))
        metric, sample, candidate = _text(row.get("metric")), _text(row.get("sample_id")), _text(row.get("candidate_key"))
        if metric not in planned:
            continue
        if samples and sample not in samples or candidates and candidate not in candidates:
            _fail("objective row is outside the task identity")
        value = _number(row.get("value"))
        if value is None:
            _fail("objective row value must be finite")
        spec = planned[metric]
        for field in ("direction", "reference_mode", "pipeline_id"):
            if spec.get(field) is not None and row.get(field) != spec.get(field):
                _fail("objective row {} differs from the metric plan".format(field))
        expected_audio_hash = _audio_hashes(audio).get((sample, candidate))
        if expected_audio_hash is not None and _text(row.get("audio_sha256")).lower() != expected_audio_hash:
            _fail("objective row audio identity differs from the audio manifest")
        direction = str(spec.get("direction", row.get("direction", "")))
        if direction.casefold() in {"lower_better", "lower-is-better", "lower"}:
            value = -value
        key = (sample, candidate)
        if key in values.setdefault(metric, {}):
            _fail("duplicate objective condition: {}".format(key))
        values[metric][key] = value
        metadata.setdefault(metric, {"metric_type": spec.get("type", row.get("metric_type")), "direction": direction, "reference_mode": spec.get("reference_mode", row.get("reference_mode")), "pipeline_id": spec.get("pipeline_id", row.get("pipeline_id"))})
    if samples and candidates:
        expected_conditions = {(sample, candidate) for sample in samples for candidate in candidates}
        for metric in planned:
            if set(values.get(metric, {})) != expected_conditions:
                _fail("objective rows do not cover the complete task matrix for {}".format(metric))
    return values, metadata


def _subjective_rows(run_dir: Path) -> List[Dict[str, str]]:
    path = _path_from_ingest(run_dir)
    if path is None or not path.is_file():
        _fail("subjective dedup table is required for analysis")
    return _table_rows(path)


def _compare_float(actual: Any, expected: Optional[float], label: str) -> None:
    if expected is None:
        if actual is not None:
            _fail("{} should be null".format(label))
        return
    value = _number(actual)
    if value is None or not math.isclose(value, expected, rel_tol=0.0, abs_tol=1e-9):
        _fail("{} is stale".format(label))


def check_analysis(artifact: Dict[str, Any], run_dir: Path) -> None:
    """Recompute before/after model-condition correlations and screening."""
    if not isinstance(artifact, dict) or artifact.get("correlation_unit") != "model_condition":
        _fail("analysis must use model_condition")
    if artifact.get("status") != "success":
        _fail("analysis is not successful")
    task, plan, audio = _task(run_dir), _load(run_dir / "artifacts" / "objective_metric_plan.json"), _audio(run_dir)
    selected = task.get("analysis_candidate_keys")
    if not isinstance(selected, list) or not selected:
        _fail("task manifest must declare analysis_candidate_keys")
    models = {str(value) for value in selected}
    candidates = set(_manifest_candidates(task))
    if not models.issubset(candidates):
        _fail("analysis candidates are outside the task manifest")
    role_map = task.get("candidate_roles")
    if isinstance(role_map, dict):
        allowed_roles = {"model", "method", "enhancement", "enhanced_model", "system"}
        if any(str(role_map.get(candidate, "")).casefold() not in allowed_roles for candidate in models):
            _fail("analysis candidates contain a non-model role")
    excluded = set(str(value) for value in task.get("excluded_candidates_from_correlation", []) if isinstance(value, str)) | DEFAULT_EXCLUDED
    if models & excluded:
        _fail("analysis candidates contain excluded conditions")
    mos_path = _resolve(_load(run_dir / "artifacts" / "condition_summary.json").get("mos_path"), run_dir)
    mos_rows = _table_rows(mos_path)
    mos_before: Dict[Tuple[str, str], float] = {}
    for row in mos_rows:
        value = _number(row.get("mos"))
        if value is None:
            _fail("MOS table contains a non-numeric value")
        key = (row.get("sample_id", ""), row.get("candidate_key", ""))
        if key[1] in models:
            mos_before[key] = value
    subjective = _subjective_rows(run_dir)
    minimum_ratings = 8
    flagged, screening = _screen(subjective, models, minimum_ratings)
    mos_after = dict(mos_before)
    grouped: Dict[Tuple[str, str], List[float]] = {}
    for row in subjective:
        key = (normalize_listener(row.get("listener_id")), row.get("sample_id", ""), row.get("candidate_key", ""))
        if key in flagged or row.get("candidate_key") not in models or _text(row.get("group_complete")).lower() == "false":
            continue
        value = _number(row.get("score"))
        if value is not None:
            grouped.setdefault((key[1], key[2]), []).append(value)
    for key, values in grouped.items():
        if values:
            mos_after[key] = sum(values) / len(values)
    objective, metadata = _objective_rows(run_dir, task, plan, audio)
    expected_rows: List[Dict[str, Any]] = []
    for metric in sorted(objective):
        for screening_name, mos in (("before", mos_before), ("after", mos_after)):
            common = sorted(key for key in set(objective[metric]) & set(mos) if key[1] in models and key[1] not in excluded)
            xs, ys = [mos[key] for key in common], [objective[metric][key] for key in common]
            expected_rows.append({"metric": metric, "screening": screening_name, "n": len(common), "model_count": len({key[1] for key in common}), "pearson": _pearson(xs, ys), "spearman": _spearman(xs, ys)})
    rows = artifact.get("correlations")
    if not isinstance(rows, list) or not expected_rows or len(rows) != len(expected_rows):
        _fail("analysis correlations are missing or stale")
    actual_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            _fail("analysis correlation row must be an object")
        key = (_text(row.get("metric")), _text(row.get("screening")))
        if key in actual_by_key:
            _fail("duplicate analysis correlation row: {}".format(key))
        actual_by_key[key] = row
    for expected in expected_rows:
        key = (expected["metric"], expected["screening"])
        row = actual_by_key.get(key)
        if row is None or int(row.get("n", -1)) != expected["n"] or int(row.get("model_count", -1)) != expected["model_count"]:
            _fail("analysis condition counts are stale: {}".format(key))
        metric_info = metadata.get(expected["metric"], {})
        for field in ("metric_type", "direction", "reference_mode"):
            if metric_info.get(field) is not None and row.get(field) != metric_info.get(field):
                _fail("analysis {} provenance is stale: {}".format(field, key))
        _compare_float(row.get("pearson"), expected["pearson"], "Pearson {}".format(key))
        _compare_float(row.get("spearman"), expected["spearman"], "Spearman {}".format(key))
        expected_common = sorted(key_value for key_value in set(objective[expected["metric"]]) & set((mos_before if expected["screening"] == "before" else mos_after)) if key_value[1] in models and key_value[1] not in excluded)
        supplied_conditions = row.get("condition_keys")
        if not isinstance(supplied_conditions, list):
            _fail("analysis condition keys are missing: {}".format(key))
        supplied_keys = sorted((str(value.get("sample_id")), str(value.get("candidate_key"))) for value in supplied_conditions if isinstance(value, dict))
        if supplied_keys != expected_common:
            _fail("analysis condition keys are stale: {}".format(key))
        if expected["n"] < 1 or expected["model_count"] < 1:
            _fail("analysis cannot succeed with empty common conditions")
    supplied_screening = artifact.get("screening")
    if not isinstance(supplied_screening, dict) or supplied_screening.get("rule_version") != SCREENING_RULE_VERSION or supplied_screening.get("objective_independent") is not True:
        _fail("analysis screening metadata is invalid")
    supplied_flagged = supplied_screening.get("flagged")
    actual_flagged = {(normalize_listener(item.get("listener_id")), _text(item.get("sample_id")), _text(item.get("candidate_key"))) for item in supplied_flagged if isinstance(item, dict)} if isinstance(supplied_flagged, list) else set()
    if actual_flagged != flagged or int(supplied_screening.get("flagged_count", -1)) != len(flagged) or supplied_screening.get("raw_scores_unchanged") is not True:
        _fail("analysis screening report is stale")
    before_detail = supplied_screening.get("before")
    after_detail = supplied_screening.get("after")
    if isinstance(before_detail, dict) and int(before_detail.get("condition_count", -1)) != len(mos_before):
        _fail("analysis before-screening count is stale")
    if isinstance(after_detail, dict) and int(after_detail.get("condition_count", -1)) != len(mos_after):
        _fail("analysis after-screening count is stale")
    provenance = artifact.get("provenance")
    if isinstance(provenance, dict) and isinstance(provenance.get("source_files"), list):
        for source in provenance["source_files"]:
            if not isinstance(source, dict):
                _fail("analysis provenance source entry is invalid")
            path = _resolve(source.get("path"), run_dir)
            if not path.is_file() or (_text(source.get("sha256")) and _sha256(path) != _text(source.get("sha256")).lower()):
                _fail("analysis provenance source is stale: {}".format(path))


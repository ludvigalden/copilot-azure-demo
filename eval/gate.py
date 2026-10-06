"""Fail-closed quality, retrieval, provenance and complete golden-set gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MIN_GROUNDEDNESS = 4.0
MIN_MEAN_RELEVANCE = 4.0
DEFAULT_K = 3
FRESHNESS_TTL_SECONDS = 7200

# The golden dataset the gate judges every results document against.
# Pure-function callers (the tests) may pass their own dataset seam;
# the CLI and the default document validation load this one.
AUTHORITATIVE_DATASET = Path(__file__).parent / "golden.jsonl"

# Paths that must be present and non-empty in a results document before
# its metric values are read. The provenance block is what makes a run
# auditable: what answered, what judged, what data, what it cost.
REQUIRED_PROVENANCE: tuple[str, ...] = (
    "run.produced_at",
    "run.producer",
    "run.answer_endpoint",
    "run.answer_revision",
    "run.answer_image",
    "run.retrieval",
    "run.dataset.path",
    "run.dataset.sha256",
    "run.dataset.questions",
    "run.judge.endpoint_host",
    "run.judge.deployment",
    "run.judge.model",
    "run.judge.api_version",
    "run.judge.prompt",
    "run.judge.max_output_tokens",
    "run.thresholds.min_groundedness",
    "run.thresholds.min_mean_relevance",
    "run.thresholds.hit_at_k",
    "run.usage.answer_calls",
    "run.usage.answer_attempts",
    "run.usage.judge_calls",
    "run.usage.judge_attempts",
    "run.usage.judge_prompt_tokens",
    "run.usage.judge_completion_tokens",
    "run.usage.answer_path_tokens",
    "run.cost.status",
)

# The thresholds a document records must be the thresholds this module
# applies. A document that claims looser thresholds than the gate
# enforces is rejected rather than scored on its own terms.
RECORDED_THRESHOLDS: dict[str, float | int] = {
    "run.thresholds.min_groundedness": MIN_GROUNDEDNESS,
    "run.thresholds.min_mean_relevance": MIN_MEAN_RELEVANCE,
    "run.thresholds.hit_at_k": DEFAULT_K,
}


@dataclass(frozen=True)
class ItemResult:
    """Metric values for one golden question."""

    query: str
    expected_article: str
    groundedness: float
    relevance: float
    retrieved: tuple[str, ...]


@dataclass(frozen=True)
class GateResult:
    """The gate verdict and one message per failed criterion."""

    passed: bool
    failures: tuple[str, ...]


@dataclass(frozen=True)
class GoldenDataset:
    """The question-to-article pairs a results document must answer in full."""

    path: str
    sha256: str
    questions: tuple[tuple[str, str], ...]


class GoldenDatasetError(RuntimeError):
    """The golden dataset cannot be read or is not a usable question list."""


def load_golden_dataset(path: Path | None = None) -> GoldenDataset:
    """Load authoritative questions and hash their raw source bytes."""
    path = path if path is not None else AUTHORITATIVE_DATASET
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise GoldenDatasetError(f"cannot read {path}: {error}") from error
    questions: list[tuple[str, str]] = []
    for number, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise GoldenDatasetError(f"{path}:{number}: not JSON: {error}") from error
        query = row.get("query") if isinstance(row, dict) else None
        if not isinstance(query, str) or not query:
            raise GoldenDatasetError(f"{path}:{number}: no query")
        expected_article = row.get("expected_article")
        if not isinstance(expected_article, str) or not expected_article:
            raise GoldenDatasetError(f"{path}:{number}: no expected_article")
        questions.append((query, expected_article))
    if not questions:
        raise GoldenDatasetError(f"{path}: no questions")
    queries = [query for query, _ in questions]
    if len(set(queries)) != len(queries):
        raise GoldenDatasetError(f"{path}: duplicate query")
    return GoldenDataset(
        path=str(path),
        sha256=hashlib.sha256(raw).hexdigest(),
        questions=tuple(questions),
    )


def citation_filenames(citations: Any) -> tuple[str, ...] | None:
    """Derive first-seen article names from title/URL pairs; reject malformed pairs."""
    if not isinstance(citations, (list, tuple)):
        return None
    names: list[str] = []
    for item in citations:
        if (
            not isinstance(item, (list, tuple))
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
        ):
            return None
        url = item[1]
        tail = url.split("://", 1)[-1].rsplit("/", 1)[-1]
        name = tail.split("?", 1)[0].split("#", 1)[0]
        if name and name not in names:
            names.append(name)
    return tuple(names)


def hit_at_k(
    retrieved: tuple[str, ...], expected_article: str, k: int = DEFAULT_K
) -> bool:
    """True when the expected article is among the top ``k`` retrieved."""
    return expected_article in retrieved[:k]


def evaluate(results: list[ItemResult], k: int = DEFAULT_K) -> GateResult:
    """Apply the gate to a full golden-set run.

    An empty run fails: a gate that passes without evidence proves
    nothing, so absence of results is a failure, not a vacuous pass.
    """
    failures: list[str] = []
    if not results:
        return GateResult(
            passed=False,
            failures=(
                "the golden set produced no results; the gate fails closed on empty input",
            ),
        )
    relevance_values = []
    for item in results:
        relevance_values.append(item.relevance)
        if item.groundedness < MIN_GROUNDEDNESS:
            failures.append(
                f"{item.query}: groundedness {item.groundedness} below {MIN_GROUNDEDNESS}"
            )
        if not hit_at_k(item.retrieved, item.expected_article, k):
            failures.append(f"{item.query}: {item.expected_article} not in the top {k}")
    mean = sum(relevance_values) / len(relevance_values)
    if mean < MIN_MEAN_RELEVANCE:
        failures.append(f"mean relevance {mean:.2f} below {MIN_MEAN_RELEVANCE}")
    return GateResult(passed=not failures, failures=tuple(failures))


def _resolve(document: dict[str, Any], path: str) -> Any:
    """Return the value at a dotted path, or ``None`` when absent."""
    value: Any = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _present(value: Any) -> bool:
    """A provenance value counts as present unless absent, null, or blank."""
    return value is not None and value != ""


def missing_provenance(document: dict[str, Any]) -> tuple[str, ...]:
    """Return every required provenance path the document does not carry."""
    return tuple(
        path for path in REQUIRED_PROVENANCE if not _present(_resolve(document, path))
    )


def _judge_score(value: Any) -> float | None:
    """Return the score as a float when it is a usable 1-to-5 judgment."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = float(value)
    return score if 1.0 <= score <= 5.0 else None


def gate_document(
    document: dict[str, Any], golden: GoldenDataset | None = None
) -> GateResult:
    """Judge provenance and every authoritative question exactly once, then metrics."""
    failures: list[str] = []
    failures.extend(
        f"missing provenance: {path}" for path in missing_provenance(document)
    )
    for path, expected in RECORDED_THRESHOLDS.items():
        recorded = _resolve(document, path)
        if recorded is not None and recorded != expected:
            failures.append(
                f"{path} records {recorded} but the gate applies {expected}"
            )

    if golden is None:
        golden = load_golden_dataset(AUTHORITATIVE_DATASET)
    dataset = _resolve(document, "run.dataset")
    if isinstance(dataset, dict):
        if dataset.get("sha256") != golden.sha256:
            failures.append(
                f"run.dataset.sha256 records {dataset.get('sha256')} "
                f"but the golden dataset hashes {golden.sha256}"
            )
        if dataset.get("questions") != len(golden.questions):
            failures.append(
                f"run.dataset.questions records {dataset.get('questions')} "
                f"but the golden dataset carries {len(golden.questions)}"
            )

    results = document.get("results")
    if not isinstance(results, list) or not results:
        failures.append(
            "the golden set produced no results; the gate fails closed on empty input"
        )
        return GateResult(passed=False, failures=tuple(failures))

    if len(results) != len(golden.questions):
        failures.append(
            f"results carries {len(results)} rows but the golden dataset has "
            f"{len(golden.questions)} questions"
        )

    expected_by_query = dict(golden.questions)
    seen: set[str] = set()
    items: list[ItemResult] = []
    for index, row in enumerate(results):
        if not isinstance(row, dict):
            failures.append(f"results[{index}]: not a per-question record")
            continue
        query = row.get("query")
        if not isinstance(query, str) or not query:
            failures.append(f"results[{index}]: no query")
            continue
        expected_article = expected_by_query.get(query)
        if expected_article is None:
            failures.append(f"{query}: the query is not in the golden dataset")
            continue
        if expected_article != row.get("expected_article"):
            failures.append(
                f"{query}: expected_article records {row.get('expected_article')} "
                f"but the golden dataset pairs it with {expected_article}"
            )
            continue
        if query in seen:
            failures.append(f"{query}: duplicate result row")
            continue
        seen.add(query)

        groundedness = _judge_score(row.get("groundedness"))
        relevance = _judge_score(row.get("relevance"))
        if groundedness is None or relevance is None:
            failures.append(
                f"{query}: no usable judge scores; the question counts as failed"
            )
        derived = citation_filenames(row.get("citations"))
        if derived is None:
            failures.append(f"{query}: citations is not a [title, url] record")
        recorded_filenames = row.get("retrieval_filenames")
        if not isinstance(recorded_filenames, list) or any(
            not isinstance(name, str) for name in recorded_filenames
        ):
            failures.append(f"{query}: no retrieval filenames record")
        elif derived is not None and recorded_filenames != list(derived):
            failures.append(
                f"{query}: recorded retrieval filenames contradict the citations"
            )
        recorded_hit = row.get("retrieval_hit")
        if not isinstance(recorded_hit, bool):
            failures.append(f"{query}: no retrieval verdict")
        elif derived is not None and recorded_hit != hit_at_k(
            derived, expected_article, DEFAULT_K
        ):
            failures.append(
                f"{query}: recorded retrieval verdict contradicts the citations"
            )
        if groundedness is not None and relevance is not None and derived is not None:
            items.append(
                ItemResult(
                    query=query,
                    expected_article=expected_article,
                    groundedness=groundedness,
                    relevance=relevance,
                    retrieved=derived,
                )
            )
    for query, _ in golden.questions:
        if query not in seen:
            failures.append(f"{query}: no result row for this golden question")
    if items:
        failures.extend(evaluate(items, DEFAULT_K).failures)
    return GateResult(passed=not failures, failures=tuple(failures))


CANDIDATE_FIELDS = (
    "staging_prefix",
    "shared_prefix",
    "source_commit",
    "kb_tree",
    "kb_source_commit",
    "image_source_commit",
    "image",
    "revision",
    "index_name",
    "index_snapshot_sha256",
    "index_definition_sha256",
    "document_count",
    "chat_deployment",
    "embedding_deployment",
    "chat_model",
    "embedding_model",
    "answer_prompt_sha256",
    "judge_prompt_sha256",
    "judge_host",
    "judge_api_version",
    "dataset_sha256",
    "run_id",
    "endpoint",
    "components",
)


def gate_release_document(
    document: dict[str, Any],
    candidate: dict[str, Any],
    now: datetime | None = None,
) -> GateResult:
    """Validate fresh evidence against the independently captured candidate."""
    if not isinstance(document, dict) or not isinstance(candidate, dict):
        return GateResult(False, ("malformed release document or candidate",))
    verdict = gate_document(document)
    failures = list(verdict.failures)

    def record(value: Any, name: str) -> dict:
        if not isinstance(value, dict):
            failures.append(f"malformed record: {name}")
            return {}
        return value

    def integer(value: Any, lower: int, upper: int, name: str) -> int:
        if type(value) is not int or not lower <= value <= upper:
            failures.append(f"invalid budget or usage: {name}")
            return 0
        return value

    evidence = record(document.get("release_evidence"), "release_evidence")
    bound = record(evidence.get("candidate"), "candidate")
    for field in CANDIDATE_FIELDS:
        value = candidate.get(field)
        if not value or bound.get(field) != value:
            failures.append(f"missing or mismatched candidate: {field}")
    if bound != candidate:
        failures.append("candidate contains unbound or changed fields")
    if evidence.get("run_id") != candidate.get("run_id"):
        failures.append("evaluation run ID does not match this run")
    for key, path in (
        ("image", "run.answer_image"),
        ("revision", "run.answer_revision"),
        ("endpoint", "run.answer_endpoint"),
        ("dataset_sha256", "run.dataset.sha256"),
        ("judge_host", "run.judge.endpoint_host"),
        ("chat_deployment", "run.judge.deployment"),
        ("judge_api_version", "run.judge.api_version"),
    ):
        if candidate.get(key) != _resolve(document, path):
            failures.append(f"provenance does not match candidate: {key}")
    prompt = _resolve(document, "run.judge.prompt")
    prompt_hash = (
        hashlib.sha256(prompt.encode()).hexdigest() if isinstance(prompt, str) else ""
    )
    if (
        prompt_hash != candidate.get("judge_prompt_sha256")
        or evidence.get("prompt_sha256") != prompt_hash
    ):
        failures.append("judge prompt does not match candidate")
    model = candidate.get("chat_model")
    if not isinstance(model, dict) or not model.get("name") or not model.get("version"):
        failures.append("invalid model identity")
    elif _resolve(document, "run.judge.model") != f"{model['name']}-{model['version']}":
        failures.append("judge response model differs from captured deployment")
    import re

    for key in ("source_commit", "kb_tree", "kb_source_commit", "image_source_commit"):
        if not isinstance(candidate.get(key), str) or not re.fullmatch(
            r"[0-9a-f]{40}", candidate[key]
        ):
            failures.append(f"invalid immutable source: {key}")
    for key in (
        "index_snapshot_sha256",
        "index_definition_sha256",
        "answer_prompt_sha256",
        "judge_prompt_sha256",
        "dataset_sha256",
    ):
        if not isinstance(candidate.get(key), str) or not re.fullmatch(
            r"[0-9a-f]{64}", candidate[key]
        ):
            failures.append(f"invalid digest: {key}")
    if not isinstance(candidate.get("image"), str) or not re.fullmatch(
        r"[^@]+@sha256:[0-9a-f]{64}", candidate["image"]
    ):
        failures.append("invalid image digest")
    for key in ("chat_model", "embedding_model"):
        value = candidate.get(key)
        if not isinstance(value, dict) or any(
            not isinstance(value.get(part), str) or not value[part]
            for part in ("name", "version", "format")
        ):
            failures.append(f"invalid model: {key}")
    integer(candidate.get("document_count"), 1, 10000, "document count")
    components = candidate.get("components")
    if (
        not isinstance(components, list)
        or not components
        or any(
            not isinstance(part, str) or part not in ("app", "kb", "evaluation")
            for part in components
        )
        or len({part for part in components if isinstance(part, str)})
        != len(components)
    ):
        failures.append("invalid component selection")
    if isinstance(components, list):
        for component, key in (
            ("app", "image_source_commit"),
            ("kb", "kb_source_commit"),
        ):
            if component in components and candidate.get(key) != candidate.get(
                "source_commit"
            ):
                failures.append(f"selected {component} source differs")
    clock = now or datetime.now(UTC)
    try:
        started, completed, expires = (
            datetime.fromisoformat(evidence[key])
            for key in ("started_at", "completed_at", "expires_at")
        )
        if not started.tzinfo or not completed.tzinfo or not expires.tzinfo:
            raise ValueError("timestamps must be timezone-aware")
        if not started <= completed <= clock < expires:
            failures.append("stale, future or expired evaluation")
        if (clock - completed).total_seconds() > FRESHNESS_TTL_SECONDS:
            failures.append("evaluation is stale")
        duration_limit = _resolve(
            document, "release_evidence.budgets.max_duration_seconds"
        )
        if type(duration_limit) is not int or not 1 <= duration_limit <= 900:
            failures.append("invalid evaluation deadline")
        elif (completed - started).total_seconds() > duration_limit:
            failures.append("evaluation exceeded deadline")
        if not 0 < (expires - started).total_seconds() <= FRESHNESS_TTL_SECONDS:
            failures.append("invalid evaluation expiry")
    except (KeyError, ValueError, TypeError):
        failures.append("missing or invalid evaluation timestamps")
    count = len(load_golden_dataset().questions)
    rows = document.get("results")
    if not isinstance(rows, list):
        rows = []
    budgets = record(evidence.get("budgets"), "budgets")
    limits = {
        "answer_attempts_per_question": 4,
        "judge_attempts_per_question": 6,
        "total_attempt_cap": 150,
        "answer_max_output_tokens": 256,
        "answer_output_token_cap": 15360,
        "judge_max_output_tokens": 300,
        "judge_input_byte_cap": 8000,
        "judge_token_cap": 150000,
        "max_duration_seconds": 900,
    }
    for key, limit in limits.items():
        if (
            integer(budgets.get(key), 1, limit, key) != limit
            and key != "max_duration_seconds"
        ):
            failures.append(f"recorded budget differs from enforced policy: {key}")
    usage = record(_resolve(document, "run.usage"), "run.usage")
    answer_attempts = integer(
        usage.get("answer_attempts"), count, count * 4, "answer_attempts"
    )
    judge_attempts = integer(
        usage.get("judge_attempts"), count, count * 6, "judge_attempts"
    )
    if answer_attempts + judge_attempts > integer(
        budgets.get("total_attempt_cap"), 1, 150, "total attempts"
    ):
        failures.append("aggregate attempt budget exceeded")
    for key in ("answer_calls", "judge_calls"):
        integer(usage.get(key), count, count, key)
    prompt_tokens = integer(
        usage.get("judge_prompt_tokens"), 1, 150000, "judge_prompt_tokens"
    )
    completion_tokens = integer(
        usage.get("judge_completion_tokens"), 1, count * 300, "judge_completion_tokens"
    )
    reserved = record(evidence.get("usage"), "reserved usage")
    answer_reserved = integer(
        reserved.get("reserved_answer_output_tokens"), 1, 15360, "answer reservations"
    )
    judge_reserved = integer(
        reserved.get("reserved_judge_tokens"), 1, 150000, "judge reservations"
    )
    if answer_reserved != answer_attempts * 256:
        failures.append("answer reservations contradict attempts")
    if (
        prompt_tokens + completion_tokens > judge_reserved
        or judge_reserved < judge_attempts * 300
    ):
        failures.append("actual judge tokens or attempts exceed reservations")
    totals = {
        key: 0
        for key in (
            "answer_attempts",
            "judge_attempts",
            "judge_prompt_tokens",
            "judge_completion_tokens",
            "reserved_judge_tokens",
        )
    }
    for row in rows:
        if not isinstance(row, dict):
            failures.append("malformed question record")
            continue
        if (
            not isinstance(row.get("answer_text"), str)
            or not row["answer_text"].strip()
            or row.get("answer_error")
            or row.get("judgment_error")
        ):
            failures.append("evaluation contains a failed answer or judgment")
        row_usage = record(row.get("usage"), "question usage")
        for key, upper in (
            ("answer_attempts", 4),
            ("judge_attempts", 6),
            ("judge_prompt_tokens", 48000),
            ("judge_completion_tokens", 300),
            ("reserved_judge_tokens", 49800),
        ):
            totals[key] += integer(row_usage.get(key), 1, upper, f"question {key}")
        if row_usage.get("judge_model") != _resolve(document, "run.judge.model"):
            failures.append("question judge model differs")
    for key, total in totals.items():
        expected = judge_reserved if key == "reserved_judge_tokens" else usage.get(key)
        if total != expected:
            failures.append(f"question usage contradicts aggregate: {key}")
    return GateResult(not failures, tuple(failures))


def main(argv: list[str] | None = None) -> int:
    """Judge one results document; exit 0 passed, 1 failed, 2 unreadable."""
    parser = argparse.ArgumentParser(
        description="Judge a results document against the quality gate."
    )
    parser.add_argument("results", help="path to a results document from produce.py")
    arguments = parser.parse_args(argv)
    try:
        text = Path(arguments.results).read_text(encoding="utf-8")
    except OSError as error:
        print(f"gate: cannot read {arguments.results}: {error}", file=sys.stderr)
        return 2
    try:
        document = json.loads(text, strict=False)
    except json.JSONDecodeError as error:
        print(f"gate: {arguments.results} is not JSON: {error}", file=sys.stderr)
        return 2
    if not isinstance(document, dict):
        print(f"gate: {arguments.results} is not a results document", file=sys.stderr)
        return 2
    try:
        golden = load_golden_dataset(AUTHORITATIVE_DATASET)
    except GoldenDatasetError as error:
        print(f"gate: cannot read the golden dataset: {error}", file=sys.stderr)
        return 2
    verdict = gate_document(document, golden)
    for failure in verdict.failures:
        print(f"gate failure: {failure}", file=sys.stderr)
    count = len(document.get("results") or [])
    print(
        f"gate: {'passed' if verdict.passed else 'FAILED'} "
        f"over {count} questions with {len(verdict.failures)} failure(s)"
    )
    return 0 if verdict.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

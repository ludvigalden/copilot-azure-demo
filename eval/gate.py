"""The evaluation quality gate.

Pure functions over per-item metric values: every item must reach the
minimum groundedness, the mean relevance across the set must reach its
threshold, and every expected article must be retrieved within the top
``k`` results. This module judges metric values; it does not produce
them — ``produce.py`` runs the live evaluation and writes the results
document this gate judges.

The gate fails closed: an empty run is a failure, not a vacuous pass,
and a results document with incomplete provenance is rejected before
its numbers are read.

A document must answer the authoritative golden dataset in full. The
dataset a document claims (``run.dataset``) is not trusted on its own:
the recorded hash and question count are compared against the golden
dataset this module loads, every result row must carry one of the
golden questions exactly once with the golden pair's expected article,
and every golden question must have a row. A run cut short — by a
crash, a spent budget, or an edit — is missing rows and fails.

Deterministic retrieval is judged from the citations the answer path
actually returned, re-derived here the same way the producer derives
them, so the row's recorded filenames and verdict must agree with that
derivation. Groundedness and relevance are model-judged scores the
producer recorded. A recorded retrieval story that contradicts the
recorded citations is doctored data and fails the gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MIN_GROUNDEDNESS = 4.0
MIN_MEAN_RELEVANCE = 4.0
DEFAULT_K = 3

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
    """Load the authoritative golden dataset this gate judges against.

    The hash is taken over the raw bytes of the file — the same value
    the producer records — so a document and the dataset it claims can
    be compared without trusting either.
    """
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
    """Article filenames derived from a citations record, first-seen order.

    Returns ``None`` when the record is not a sequence of ``[title, url]``
    pairs: a malformed record is a schema failure the gate reports
    rather than a derivation it guesses past. The derivation matches
    the producer's — strip scheme and host, take the last path segment,
    strip query and fragment, dedupe keeping first occurrence.
    """
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
    """Judge a full results document: provenance, completeness, then metrics.

    Completeness is judged against the authoritative golden dataset, not
    the document's own account of it. A count check trusting the
    document's claimed question count is insufficient: the recorded
    hash and count are compared against the golden data, every row must
    carry a golden question exactly once with the golden expected
    article, and every golden question must have a row.
    """
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

"""The evaluation quality gate.

Pure functions over per-item metric values: every item must reach the
minimum groundedness, the mean relevance across the set must reach its
threshold, and every expected article must be retrieved within the top
``k`` results. The runner that produces the metric values belongs to the
evaluation phase; this module judges them.
"""

from dataclasses import dataclass

MIN_GROUNDEDNESS = 4.0
MIN_MEAN_RELEVANCE = 4.0
DEFAULT_K = 3


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


def hit_at_k(
    retrieved: tuple[str, ...], expected_article: str, k: int = DEFAULT_K
) -> bool:
    """True when the expected article is among the top ``k`` retrieved."""
    return expected_article in retrieved[:k]


def evaluate(results: list[ItemResult], k: int = DEFAULT_K) -> GateResult:
    """Apply the gate to a full golden-set run."""
    failures: list[str] = []
    relevance_values = []
    for item in results:
        relevance_values.append(item.relevance)
        if item.groundedness < MIN_GROUNDEDNESS:
            failures.append(
                f"{item.query}: groundedness {item.groundedness} below {MIN_GROUNDEDNESS}"
            )
        if not hit_at_k(item.retrieved, item.expected_article, k):
            failures.append(f"{item.query}: {item.expected_article} not in the top {k}")
    if relevance_values:
        mean = sum(relevance_values) / len(relevance_values)
        if mean < MIN_MEAN_RELEVANCE:
            failures.append(f"mean relevance {mean:.2f} below {MIN_MEAN_RELEVANCE}")
    return GateResult(passed=not failures, failures=tuple(failures))

"""Tests for the evaluation quality gate."""

from gate import DEFAULT_K, GateResult, ItemResult, evaluate, hit_at_k


def make(
    groundedness=5.0,
    relevance=5.0,
    retrieved=("a.md", "b.md"),
    expected="a.md",
    query="q",
):
    return ItemResult(
        query=query,
        expected_article=expected,
        groundedness=groundedness,
        relevance=relevance,
        retrieved=retrieved,
    )


def test_a_clean_run_passes():
    result = evaluate([make(), make(query="q2")])

    assert result == GateResult(passed=True, failures=())


def test_groundedness_below_the_threshold_fails_the_item():
    result = evaluate([make(groundedness=3.9)])

    assert not result.passed
    assert any("groundedness" in failure for failure in result.failures)


def test_mean_relevance_below_the_threshold_fails_the_gate():
    result = evaluate([make(relevance=5.0), make(relevance=2.0, query="q2")])

    assert not result.passed
    assert any("mean relevance" in failure for failure in result.failures)


def test_missing_expected_article_fails_hit_at_k():
    result = evaluate([make(retrieved=("b.md", "c.md"), expected="a.md")])

    assert not result.passed
    assert any("not in the top" in failure for failure in result.failures)


def test_expected_article_outside_the_top_k_fails():
    retrieved = ("b.md", "c.md", "d.md", "a.md")

    assert hit_at_k(retrieved, "a.md", k=4)
    assert not hit_at_k(retrieved, "a.md", k=DEFAULT_K)


def test_an_empty_run_passes_vacuously():
    assert evaluate([]).passed

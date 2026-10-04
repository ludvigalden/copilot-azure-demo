"""Tests for the evaluation quality gate."""

from gate import (
    DEFAULT_K,
    MIN_GROUNDEDNESS,
    MIN_MEAN_RELEVANCE,
    GateResult,
    ItemResult,
    evaluate,
    gate_document,
    hit_at_k,
    missing_provenance,
)


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


def result_row(
    query="How do I reset my password?",
    expected="reset-password.md",
    groundedness=5.0,
    relevance=5.0,
):
    return {
        "query": query,
        "expected_article": expected,
        "answer_text": "Open the portal and choose Forgot password.",
        "citations": [
            [
                "Reset password",
                "https://github.com/example/repo/blob/main/kb/reset-password.md",
            ]
        ],
        "retrieval_filenames": ["reset-password.md", "vpn.md", "mfa-setup.md"],
        "retrieval_hit": True,
        "retrieval_rank": 1,
        "groundedness": groundedness,
        "relevance": relevance,
        "judgment_error": None,
    }


def results_document(rows=None):
    return {
        "run": {
            "produced_at": "2026-10-04T12:00:00+00:00",
            "producer": "eval/produce.py",
            "answer_endpoint": "https://staging.example/api/answers",
            "answer_revision": "app--0000001",
            "answer_image": "ghcr.io/example/app:abc123",
            "retrieval": "the production retrieval path inside the deployed app",
            "dataset": {
                "path": "eval/golden.jsonl",
                "sha256": "0" * 64,
                "questions": 1,
            },
            "judge": {
                "endpoint_host": "judge.example.cognitiveservices.azure.com",
                "deployment": "chat-staging",
                "model": "gpt-4.1-mini-2025-04-14",
                "api_version": "2024-10-21",
                "prompt": "judge prompt",
                "max_output_tokens": 300,
            },
            "thresholds": {
                "min_groundedness": MIN_GROUNDEDNESS,
                "min_mean_relevance": MIN_MEAN_RELEVANCE,
                "hit_at_k": DEFAULT_K,
            },
            "usage": {
                "answer_calls": 1,
                "answer_attempts": 1,
                "judge_calls": 1,
                "judge_attempts": 1,
                "judge_prompt_tokens": 100,
                "judge_completion_tokens": 50,
                "answer_path_tokens": "not exposed by the answers endpoint",
            },
            "cost": {
                "status": "cost not computed: no verified per-token price is cited"
            },
        },
        "results": rows if rows is not None else [result_row()],
    }


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


def test_an_empty_run_fails_closed():
    result = evaluate([])

    assert not result.passed
    assert any("fails closed" in failure for failure in result.failures)


def test_a_document_without_provenance_fails():
    document = {"results": [result_row()]}

    result = gate_document(document)

    assert not result.passed
    assert missing_provenance(document)
    assert any("missing provenance" in failure for failure in result.failures)


def test_a_single_missing_provenance_field_fails():
    document = results_document()
    del document["run"]["producer"]

    result = gate_document(document)

    assert not result.passed
    assert "missing provenance: run.producer" in result.failures


def test_a_doctored_threshold_is_rejected():
    document = results_document()
    document["run"]["thresholds"]["min_groundedness"] = 1.0

    result = gate_document(document)

    assert not result.passed
    assert any(
        "records 1.0 but the gate applies 4.0" in failure for failure in result.failures
    )


def test_a_doctored_retrieval_verdict_is_rejected():
    document = results_document()
    document["results"][0]["retrieval_hit"] = True
    document["results"][0]["retrieval_filenames"] = ["vpn.md", "printer.md"]

    result = gate_document(document)

    assert not result.passed
    assert any("contradicts the citations" in failure for failure in result.failures)


def test_a_row_without_judge_scores_fails():
    document = results_document()
    document["results"][0]["groundedness"] = None
    document["results"][0]["relevance"] = None

    result = gate_document(document)

    assert not result.passed
    assert any("no usable judge scores" in failure for failure in result.failures)


def test_a_valid_document_passes():
    result = gate_document(results_document())

    assert result.passed
    assert result.failures == ()


def test_gate_cli_exits_two_on_a_missing_file(tmp_path):
    from gate import main

    assert main([str(tmp_path / "absent.json")]) == 2

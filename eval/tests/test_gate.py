"""Tests for the evaluation quality gate."""

import hashlib
import json

import pytest

import gate
from gate import (
    DEFAULT_K,
    MIN_GROUNDEDNESS,
    MIN_MEAN_RELEVANCE,
    GateResult,
    GoldenDataset,
    ItemResult,
    citation_filenames,
    evaluate,
    gate_document,
    hit_at_k,
    load_golden_dataset,
    missing_provenance,
)

GOLDEN_QUESTIONS = tuple(
    (f"question {number}", f"article-{number}.md") for number in range(15)
)


def golden_payload(questions=GOLDEN_QUESTIONS):
    """The JSONL text a golden dataset file carries for these questions."""
    return "".join(
        json.dumps(
            {
                "query": query,
                "expected_article": article,
                "ground_truth": "the truth",
            }
        )
        + "\n"
        for query, article in questions
    )


def make_golden(questions=GOLDEN_QUESTIONS):
    """A golden dataset seam whose hash matches golden_payload's bytes."""
    return GoldenDataset(
        path="eval/golden.jsonl",
        sha256=hashlib.sha256(golden_payload(questions).encode("utf-8")).hexdigest(),
        questions=tuple(questions),
    )


SINGLE = make_golden([("How do I reset my password?", "reset-password.md")])


def passing_row(query, article):
    """A results row that honestly answers one golden question."""
    return {
        "query": query,
        "expected_article": article,
        "answer_text": f"the answer to {query}",
        "citations": [
            ["Article", f"https://github.com/example/repo/blob/main/kb/{article}"]
        ],
        "retrieval_filenames": [article],
        "retrieval_hit": True,
        "retrieval_rank": 1,
        "groundedness": 5.0,
        "relevance": 5.0,
        "judgment_error": None,
    }


def results_document(golden):
    """A complete results document over the seam's questions."""
    return {
        "run": {
            "produced_at": "2026-10-04T12:00:00+00:00",
            "producer": "eval/produce.py",
            "answer_endpoint": "https://staging.example/api/answers",
            "answer_revision": "app--0000001",
            "answer_image": "ghcr.io/example/app:abc123",
            "retrieval": "the production retrieval path inside the deployed app",
            "dataset": {
                "path": golden.path,
                "sha256": golden.sha256,
                "questions": len(golden.questions),
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
                "answer_calls": len(golden.questions),
                "answer_attempts": len(golden.questions),
                "judge_calls": len(golden.questions),
                "judge_attempts": len(golden.questions),
                "judge_prompt_tokens": 100,
                "judge_completion_tokens": 50,
                "answer_path_tokens": "not exposed by the answers endpoint",
            },
            "cost": {
                "status": "cost not computed: no verified per-token price is cited"
            },
        },
        "results": [passing_row(query, article) for query, article in golden.questions],
    }


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


def test_an_empty_run_fails_closed():
    result = evaluate([])

    assert not result.passed
    assert any("fails closed" in failure for failure in result.failures)


def test_a_document_without_provenance_fails():
    document = {
        "results": [passing_row("How do I reset my password?", "reset-password.md")]
    }

    result = gate_document(document, SINGLE)

    assert not result.passed
    assert missing_provenance(document)
    assert any("missing provenance" in failure for failure in result.failures)


def test_a_single_missing_provenance_field_fails():
    document = results_document(SINGLE)
    del document["run"]["producer"]

    result = gate_document(document, SINGLE)

    assert not result.passed
    assert "missing provenance: run.producer" in result.failures


def test_a_doctored_threshold_is_rejected():
    document = results_document(SINGLE)
    document["run"]["thresholds"]["min_groundedness"] = 1.0

    result = gate_document(document, SINGLE)

    assert not result.passed
    assert any(
        "records 1.0 but the gate applies 4.0" in failure for failure in result.failures
    )


def test_a_doctored_retrieval_story_is_rejected():
    document = results_document(SINGLE)
    row = document["results"][0]
    row["citations"] = [
        ["Other", "https://github.com/example/repo/blob/main/kb/other.md"]
    ]

    result = gate_document(document, SINGLE)

    assert not result.passed
    assert any(
        "recorded retrieval filenames contradict the citations" in failure
        for failure in result.failures
    )
    assert any(
        "recorded retrieval verdict contradicts the citations" in failure
        for failure in result.failures
    )


def test_a_row_without_judge_scores_fails():
    document = results_document(SINGLE)
    document["results"][0]["groundedness"] = None
    document["results"][0]["relevance"] = None

    result = gate_document(document, SINGLE)

    assert not result.passed
    assert any("no usable judge scores" in failure for failure in result.failures)


def test_a_complete_golden_run_passes():
    golden = make_golden()

    result = gate_document(results_document(golden), golden)

    assert result.passed
    assert result.failures == ()


def test_a_truncated_document_fails_even_with_the_count_claimed():
    golden = make_golden()
    document = results_document(golden)
    document["results"] = document["results"][:10]

    result = gate_document(document, golden)

    assert not result.passed
    assert any(
        "rows but the golden dataset has 15 questions" in failure
        for failure in result.failures
    )
    assert (
        len(
            [
                failure
                for failure in result.failures
                if "no result row for this golden question" in failure
            ]
        )
        == 5
    )
    assert len(result.failures) == 6


def test_a_doctored_question_count_fails():
    golden = make_golden()
    document = results_document(golden)
    document["results"] = document["results"][:10]
    document["run"]["dataset"]["questions"] = 10

    result = gate_document(document, golden)

    assert not result.passed
    assert any(
        "records 10 but the golden dataset carries 15" in failure
        for failure in result.failures
    )
    assert len(result.failures) == 7


def test_duplicating_every_row_fails():
    golden = make_golden()
    document = results_document(golden)
    document["results"] = document["results"] * 2

    result = gate_document(document, golden)

    assert not result.passed
    assert (
        len(
            [
                failure
                for failure in result.failures
                if "duplicate result row" in failure
            ]
        )
        == 15
    )
    assert len(result.failures) == 16


def test_a_duplicate_that_hides_a_missing_question_keeps_a_consistent_count():
    golden = make_golden()
    document = results_document(golden)
    rows = document["results"]
    document["results"] = rows[1:] + [dict(rows[1])]

    result = gate_document(document, golden)

    assert not result.passed
    assert not any(
        "rows but the golden dataset has" in failure for failure in result.failures
    )
    assert any("duplicate result row" in failure for failure in result.failures)
    assert (
        f"{golden.questions[0][0]}: no result row for this golden question"
        in result.failures
    )
    assert len(result.failures) == 2


def test_a_swapped_expected_article_fails():
    golden = make_golden()
    document = results_document(golden)
    document["results"][3]["expected_article"] = "unrelated.md"

    result = gate_document(document, golden)

    assert not result.passed
    assert any(
        "expected_article records unrelated.md" in failure
        and "pairs it with article-3.md" in failure
        for failure in result.failures
    )
    assert len(result.failures) == 2


def test_a_foreign_query_fails_membership():
    golden = make_golden()
    document = results_document(golden)
    document["results"][0]["query"] = "not a golden question"

    result = gate_document(document, golden)

    assert not result.passed
    assert any(
        "not a golden question: the query is not in the golden dataset" in failure
        for failure in result.failures
    )
    assert (
        f"{golden.questions[0][0]}: no result row for this golden question"
        in result.failures
    )
    assert len(result.failures) == 2


def test_a_false_dataset_hash_fails():
    golden = make_golden()
    document = results_document(golden)
    document["run"]["dataset"]["sha256"] = "f" * 64

    result = gate_document(document, golden)

    assert not result.passed
    assert any(
        "but the golden dataset hashes" in failure for failure in result.failures
    )
    assert len(result.failures) == 1


def test_a_row_without_a_query_fails():
    golden = make_golden()
    document = results_document(golden)
    del document["results"][2]["query"]

    result = gate_document(document, golden)

    assert not result.passed
    assert "results[2]: no query" in result.failures
    assert (
        f"{golden.questions[2][0]}: no result row for this golden question"
        in result.failures
    )
    assert len(result.failures) == 2


def test_a_malformed_citations_record_fails_the_row():
    golden = make_golden()
    document = results_document(golden)
    document["results"][2]["citations"] = "reset-password.md"

    result = gate_document(document, golden)

    assert not result.passed
    assert any(
        "citations is not a [title, url] record" in failure
        for failure in result.failures
    )
    assert len(result.failures) == 1


def test_a_malformed_citation_pair_fails_the_row():
    golden = make_golden()
    document = results_document(golden)
    document["results"][2]["citations"] = [["only-a-title"]]

    result = gate_document(document, golden)

    assert not result.passed
    assert any(
        "citations is not a [title, url] record" in failure
        for failure in result.failures
    )


def test_a_row_without_retrieval_filenames_fails():
    golden = make_golden()
    document = results_document(golden)
    del document["results"][2]["retrieval_filenames"]

    result = gate_document(document, golden)

    assert not result.passed
    assert any(
        "no retrieval filenames record" in failure for failure in result.failures
    )


def test_a_row_without_a_retrieval_verdict_fails():
    golden = make_golden()
    document = results_document(golden)
    del document["results"][2]["retrieval_hit"]

    result = gate_document(document, golden)

    assert not result.passed
    assert any("no retrieval verdict" in failure for failure in result.failures)


def test_a_non_record_row_fails():
    golden = make_golden()
    document = results_document(golden)
    document["results"][2] = "not a dict"

    result = gate_document(document, golden)

    assert not result.passed
    assert "results[2]: not a per-question record" in result.failures
    assert (
        f"{golden.questions[2][0]}: no result row for this golden question"
        in result.failures
    )


def test_citation_filenames_strip_and_dedupe_in_order():
    citations = (
        ("t", "https://github.com/o/r/blob/main/kb/a.md"),
        ("t", "https://github.com/o/r/blob/main/kb/b.md?x=1"),
        ("t", "https://github.com/o/r/blob/main/kb/a.md#section"),
    )

    assert citation_filenames(citations) == ("a.md", "b.md")


def test_citation_filenames_reject_malformed_records():
    assert citation_filenames("a.md") is None
    assert citation_filenames([["only-a-title"]]) is None
    assert citation_filenames([["t", 42]]) is None


def test_load_golden_dataset_reads_the_question_list(tmp_path):
    path = tmp_path / "golden.jsonl"
    path.write_text(golden_payload(), encoding="utf-8")

    golden = load_golden_dataset(path)

    assert golden.path == str(path)
    assert golden.sha256 == make_golden().sha256
    assert golden.questions == GOLDEN_QUESTIONS


def test_load_golden_dataset_refuses_duplicate_queries(tmp_path):
    path = tmp_path / "dup.jsonl"
    row = json.dumps({"query": "q", "expected_article": "a"})
    path.write_text(row + "\n" + row + "\n", encoding="utf-8")

    with pytest.raises(gate.GoldenDatasetError):
        load_golden_dataset(path)


def test_load_golden_dataset_refuses_an_empty_file(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("\n\n", encoding="utf-8")

    with pytest.raises(gate.GoldenDatasetError):
        load_golden_dataset(path)


def test_load_golden_dataset_refuses_an_unreadable_path(tmp_path):
    with pytest.raises(gate.GoldenDatasetError):
        load_golden_dataset(tmp_path / "absent.jsonl")


def write_golden_file(tmp_path, monkeypatch, questions=GOLDEN_QUESTIONS):
    path = tmp_path / "golden.jsonl"
    path.write_text(golden_payload(questions), encoding="utf-8")
    monkeypatch.setattr(gate, "AUTHORITATIVE_DATASET", path)
    return path, make_golden(questions)


def test_the_gate_cli_passes_a_complete_document(tmp_path, monkeypatch):
    write_golden_file(tmp_path, monkeypatch)
    document_path = tmp_path / "results.json"
    document_path.write_text(
        json.dumps(results_document(make_golden())), encoding="utf-8"
    )

    assert gate.main([str(document_path)]) == 0


def test_the_gate_cli_fails_a_doctored_document(tmp_path, monkeypatch):
    _, golden = write_golden_file(tmp_path, monkeypatch)
    document = results_document(golden)
    document["results"][4]["query"] = "not a golden question"
    document_path = tmp_path / "results.json"
    document_path.write_text(json.dumps(document), encoding="utf-8")

    assert gate.main([str(document_path)]) == 1


def test_the_gate_cli_exits_two_on_a_missing_file(tmp_path):
    assert gate.main([str(tmp_path / "absent.json")]) == 2


def test_the_gate_cli_exits_two_when_the_golden_dataset_is_unreadable(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gate, "AUTHORITATIVE_DATASET", tmp_path / "absent.jsonl")
    document_path = tmp_path / "results.json"
    document_path.write_text(
        json.dumps(results_document(make_golden())), encoding="utf-8"
    )

    assert gate.main([str(document_path)]) == 2

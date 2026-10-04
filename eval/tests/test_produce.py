"""Hermetic tests for the evaluation producer."""

import json

import pytest

import produce
from produce import Budget, BudgetExceeded, ProduceError, RunConfig


def make_config(tmp_path, questions=1):
    dataset = tmp_path / "golden.jsonl"
    rows = [
        {
            "query": f"question {n}",
            "expected_article": "reset-password.md",
            "ground_truth": "the truth",
        }
        for n in range(questions)
    ]
    dataset.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return RunConfig(
        endpoint="https://staging.example/api/answers",
        answer_revision="app--0000001",
        answer_image="ghcr.io/example/app:abc",
        judge_host="judge.example",
        judge_deployment="chat-staging",
        judge_api_version="2024-10-21",
        dataset_path=dataset,
        key_env="EVAL_TEST_KEY",
        output_path=tmp_path / "results" / "run.json",
    )


def answer_body():
    return {
        "text": "the answer",
        "citations": [
            {
                "title": "Reset password",
                "url": "https://github.com/o/r/blob/main/kb/reset-password.md",
            }
        ],
        "chunks": [{"title": "t", "content": "c"}],
    }


def judge_body(scores=(5, 4), model="gpt-4.1-mini-2025-04-14"):
    content = json.dumps(
        {
            "groundedness": scores[0],
            "relevance": scores[1],
            "groundedness_rationale": "g",
            "relevance_rationale": "r",
        }
    )
    return {
        "model": model,
        "usage": {"prompt_tokens": 11, "completion_tokens": 5},
        "choices": [{"message": {"content": content}}],
    }


class FakeTransport:
    """Replays canned responses and records every request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, json.loads(body)))
        status, payload = self.responses.pop(0)
        return produce.HttpResponse(status, json.dumps(payload).encode())


def test_citation_filenames_dedupe_in_order():
    citations = (
        ("t", "https://github.com/o/r/blob/main/kb/a.md"),
        ("t", "https://github.com/o/r/blob/main/kb/b.md?x=1"),
        ("t", "https://github.com/o/r/blob/main/kb/a.md#section"),
    )

    assert produce.citation_filenames(citations) == ("a.md", "b.md")


def test_retrieval_hit_reports_the_rank():
    assert produce.retrieval_hit(("x.md", "a.md"), "a.md", 3) == (True, 2)
    assert produce.retrieval_hit(("x.md", "a.md"), "a.md", 1) == (False, None)


def test_the_budget_stops_at_the_cap():
    budget = Budget(answer_attempt_cap=1, judge_attempt_cap=1)

    budget.charge_answer_attempt()

    with pytest.raises(BudgetExceeded):
        budget.charge_answer_attempt()


def test_parse_judgment_reads_scores():
    groundedness, relevance, _, _, error = produce.parse_judgment(
        '{"groundedness": 5, "relevance": 4, "groundedness_rationale": "g",'
        ' "relevance_rationale": "r"}'
    )

    assert (groundedness, relevance, error) == (5.0, 4.0, "")


def test_parse_judgment_strips_fences():
    groundedness, relevance, _, _, error = produce.parse_judgment(
        "```json\n"
        '{"groundedness": 5, "relevance": 4, "groundedness_rationale": "g",'
        ' "relevance_rationale": "r"}\n'
        "```"
    )

    assert (groundedness, relevance, error) == (5.0, 4.0, "")


def test_parse_judgment_rejects_garbage():
    assert produce.parse_judgment("no json here")[4]
    assert produce.parse_judgment('{"groundedness": 9, "relevance": 4}')[0] is None


def test_the_judge_prompt_judges_text_not_citations():
    prompt = produce.JUDGE_PROMPT_TEMPLATE.format(
        question="q", ground_truth="t", context="c", answer="a"
    )

    assert "proves transport only" in prompt


def test_the_judge_call_caps_output_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_TEST_KEY", "sekrit-value")
    transport = FakeTransport([(200, judge_body())])

    produce.judge_answer(
        transport,
        make_config(tmp_path),
        "sekrit-value",
        "the prompt",
        Budget(answer_attempt_cap=0, judge_attempt_cap=1),
        lambda seconds: None,
    )

    assert transport.calls[0][3]["max_tokens"] == produce.JUDGE_MAX_OUTPUT_TOKENS
    assert transport.calls[0][2]["api-key"] == "sekrit-value"


def test_a_full_run_passes_and_records_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_TEST_KEY", "sekrit-value")
    config = make_config(tmp_path, questions=2)
    responses = []
    for _ in range(2):
        responses.append((200, answer_body()))
        responses.append((200, judge_body()))
    transport = FakeTransport(responses)

    document = produce.run(config, transport, lambda seconds: None)

    assert document["gate"]["passed"] is True
    assert document["run"]["usage"]["answer_calls"] == 2
    assert document["run"]["usage"]["judge_calls"] == 2
    assert document["run"]["usage"]["judge_prompt_tokens"] == 22
    assert document["run"]["judge"]["model"] == "gpt-4.1-mini-2025-04-14"
    assert "sekrit-value" not in json.dumps(document)
    assert config.output_path.exists()


def test_a_run_without_a_credential_refuses(tmp_path, monkeypatch):
    monkeypatch.delenv("EVAL_TEST_KEY", raising=False)

    with pytest.raises(ProduceError, match="missing credential"):
        produce.run(make_config(tmp_path), FakeTransport([]), lambda seconds: None)


def test_an_empty_dataset_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_TEST_KEY", "sekrit-value")
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n")
    config = make_config(tmp_path)
    config.dataset_path = empty

    with pytest.raises(ProduceError, match="empty"):
        produce.run(config, FakeTransport([]), lambda seconds: None)


def test_a_failed_answer_stays_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_TEST_KEY", "sekrit-value")
    config = make_config(tmp_path, questions=1)
    transport = FakeTransport([(503, {"e": 1})] * produce.ANSWER_ATTEMPTS_PER_QUESTION)

    document = produce.run(config, transport, lambda seconds: None)

    assert document["gate"]["passed"] is False
    assert document["run"]["usage"]["judge_calls"] == 0
    assert document["results"][0]["answer_error"]
    assert document["results"][0].get("groundedness") is None


def test_main_exits_four_when_nothing_is_answered(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_TEST_KEY", "sekrit-value")
    monkeypatch.setattr(
        produce,
        "default_transport",
        lambda *arguments: produce.HttpResponse(503, b"{}"),
    )
    monkeypatch.setattr(produce.time, "sleep", lambda seconds: None)
    config = make_config(tmp_path)

    code = produce.main(
        [
            "--endpoint",
            config.endpoint,
            "--revision",
            config.answer_revision,
            "--image",
            config.answer_image,
            "--judge-host",
            config.judge_host,
            "--deployment",
            config.judge_deployment,
            "--api-version",
            config.judge_api_version,
            "--key-env",
            config.key_env,
            "--dataset",
            str(config.dataset_path),
            "--output",
            str(config.output_path),
        ]
    )

    assert code == 4
    assert not config.output_path.exists()

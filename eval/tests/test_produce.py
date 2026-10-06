"""Hermetic tests for the evaluation producer."""

import json
import re
import urllib.error
from typing import Any

import pytest

import gate
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


def cli_arguments(config):
    return [
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


def answer_body():
    return {
        "text": "the answer",
        "citations": [
            {
                "title": "Reset password",
                "url": "https://github.com/o/r/blob/0123456789abcdef0123456789abcdef01234567/kb/reset-password.md",
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

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, json.loads(body), timeout))
        status, payload = self.responses.pop(0)
        return produce.HttpResponse(status, json.dumps(payload).encode())


class ServingThenDeadTransport:
    """Serves canned responses, then raises the run's blocking error."""

    def __init__(self, responses, error):
        self.responses = list(responses)
        self.error = error

    def __call__(self, method, url, headers, body, timeout):
        if self.responses:
            status, payload = self.responses.pop(0)
            return produce.HttpResponse(status, json.dumps(payload).encode())
        raise self.error


class TimeoutThenServingTransport:
    """Raises EndpointTimeout for the first calls, then serves one response."""

    def __init__(self, timeouts, response):
        self.timeouts = timeouts
        self.response = response
        self.calls = 0

    def __call__(self, method, url, headers, body, timeout):
        self.calls += 1
        if self.calls <= self.timeouts:
            raise produce.EndpointTimeout("endpoint timed out: fixture")
        return self.response


class ColdStartTransport:
    """Records every request, times the warm-up out, serves the measured set."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        payload = json.loads(body)
        self.calls.append((method, url, payload.get("question")))
        if payload.get("question") == "hello":
            raise produce.EndpointTimeout("endpoint timed out: fixture")
        status, served = (
            (200, answer_body()) if payload.get("question") else (200, judge_body())
        )
        return produce.HttpResponse(status, json.dumps(served).encode())


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


def test_each_leg_times_out_against_its_own_constant(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_TEST_KEY", "sekrit-value")
    config = make_config(tmp_path)
    transport = FakeTransport([(200, {}), (200, answer_body()), (200, judge_body())])

    produce.run(config, transport, lambda seconds: None)

    warm_up_call, answer_call, judge_call = transport.calls[:3]
    assert warm_up_call[3] == {"question": "hello"}
    assert answer_call[1] == config.endpoint
    assert answer_call[4] == produce.ANSWER_TIMEOUT_SECONDS
    assert judge_call[1].startswith(f"https://{config.judge_host}/")
    assert judge_call[4] == produce.JUDGE_TIMEOUT_SECONDS
    assert produce.ANSWER_TIMEOUT_SECONDS != produce.JUDGE_TIMEOUT_SECONDS


def test_default_transport_passes_the_timeout_to_urlopen(monkeypatch):
    captured = {}

    class FakeResponse:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *arguments):
            return False

    def fake_urlopen(request, timeout):
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(produce.urllib.request, "urlopen", fake_urlopen)

    response = produce.default_transport("GET", "https://example.test/x", {}, b"", 42.0)

    assert captured["timeout"] == 42.0
    assert response.status == 200


def test_a_full_run_passes_and_records_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_TEST_KEY", "sekrit-value")
    config = make_config(tmp_path, questions=2)
    responses = [(200, {})]
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
    transport = FakeTransport(
        [(503, {"e": 1})] * (produce.ANSWER_ATTEMPTS_PER_QUESTION + 1)
    )

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

    code = produce.main(cli_arguments(config))

    assert code == 4
    assert config.output_path.exists()
    partial = json.loads(config.output_path.read_text())
    assert len(partial["results"]) == 1
    assert partial["results"][0]["answer_text"] is None
    golden = gate.load_golden_dataset(config.dataset_path)
    verdict = gate.gate_document(partial, golden)
    assert not verdict.passed


def test_main_exits_three_and_keeps_the_partial_document(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_TEST_KEY", "sekrit-value")
    config = make_config(tmp_path, questions=2)
    dead = ServingThenDeadTransport(
        [(200, {}), (200, answer_body()), (200, judge_body())],
        ProduceError("endpoint unreachable: the endpoint died mid-run"),
    )
    monkeypatch.setattr(produce, "default_transport", dead)
    monkeypatch.setattr(produce.time, "sleep", lambda seconds: None)

    code = produce.main(cli_arguments(config))

    assert code == 3
    assert config.output_path.exists()
    partial = json.loads(config.output_path.read_text())
    assert len(partial["results"]) == 1
    golden = gate.load_golden_dataset(config.dataset_path)
    verdict = gate.gate_document(partial, golden)
    assert not verdict.passed
    assert any(
        "no result row for this golden question" in failure
        for failure in verdict.failures
    )


def release_fixture(tmp_path, monkeypatch):
    import hashlib
    from datetime import UTC, datetime

    monkeypatch.setenv("EVAL_TEST_KEY", "fixture-not-a-credential")
    candidate: dict[str, Any] = {field: "fixture" for field in gate.CANDIDATE_FIELDS}
    for field in (
        "source_commit",
        "kb_tree",
        "kb_source_commit",
        "image_source_commit",
    ):
        candidate[field] = "a" * 40
    for field in (
        "index_snapshot_sha256",
        "index_definition_sha256",
        "answer_prompt_sha256",
        "judge_prompt_sha256",
        "dataset_sha256",
    ):
        candidate[field] = "b" * 64
    candidate.update(
        image="ghcr.io/o/r@sha256:" + "c" * 64,
        revision="app--one",
        index_name="kb",
        document_count=15,
        endpoint="https://staging.example/api/answers",
        run_id="1:1",
        components=["app", "kb"],
        chat_deployment="chat",
        embedding_deployment="embedding",
        judge_host="judge.example",
        judge_api_version="2024-10-21",
        chat_model={
            "name": "gpt-4.1-mini",
            "version": "2025-04-14",
            "format": "OpenAI",
        },
        embedding_model={
            "name": "text-embedding-3-small",
            "version": "1",
            "format": "OpenAI",
        },
        dataset_sha256=hashlib.sha256(produce.DEFAULT_DATASET.read_bytes()).hexdigest(),
        judge_prompt_sha256=hashlib.sha256(
            produce.JUDGE_PROMPT_TEMPLATE.encode()
        ).hexdigest(),
    )
    config = RunConfig(
        endpoint=candidate["endpoint"],
        answer_revision=candidate["revision"],
        answer_image=candidate["image"],
        judge_host=candidate["judge_host"],
        judge_deployment="chat",
        judge_api_version=candidate["judge_api_version"],
        dataset_path=produce.DEFAULT_DATASET,
        key_env="EVAL_TEST_KEY",
        output_path=tmp_path / "evaluation.json",
        candidate=candidate,
        run_id="1:1",
    )
    responses = [(200, {})]
    for question in produce.load_dataset(produce.DEFAULT_DATASET):
        body = answer_body()
        body["citations"][0]["url"] = (
            "https://github.com/o/r/blob/"
            + "a" * 40
            + "/kb/"
            + question.expected_article
        )
        responses.extend([(200, body), (200, judge_body())])
    document = produce.run(config, FakeTransport(responses), lambda _: None)
    return document, candidate, datetime.now(UTC)


def test_all_golden_release_evidence_passes(tmp_path, monkeypatch):
    document, candidate, now = release_fixture(tmp_path, monkeypatch)
    verdict = gate.gate_release_document(document, candidate, now)
    assert verdict.passed, verdict.failures
    assert len(document["results"]) == 15


@pytest.mark.parametrize("field", gate.CANDIDATE_FIELDS)
def test_every_candidate_binding_fails_closed(tmp_path, monkeypatch, field):
    from copy import deepcopy

    document, candidate, now = release_fixture(tmp_path, monkeypatch)
    changed = deepcopy(candidate)
    changed[field] = None
    assert not gate.gate_release_document(document, changed, now).passed


@pytest.mark.parametrize("field", ["candidate", "budgets", "usage"])
@pytest.mark.parametrize("value", [None, [], "malformed", 7])
def test_nested_release_shapes_fail_closed(tmp_path, monkeypatch, field, value):
    document, candidate, now = release_fixture(tmp_path, monkeypatch)
    document["release_evidence"][field] = value
    assert not gate.gate_release_document(document, candidate, now).passed


@pytest.mark.parametrize(
    "mutation",
    [
        "expired",
        "future",
        "partial",
        "duplicate",
        "negative",
        "combined",
        "row-attempts",
        "deadline",
        "deadline-over-ceiling",
        "recorded-deadline",
    ],
)
def test_release_negative_controls(tmp_path, monkeypatch, mutation):
    from datetime import timedelta

    document, candidate, now = release_fixture(tmp_path, monkeypatch)
    if mutation == "expired":
        document["release_evidence"]["expires_at"] = (
            now - timedelta(seconds=1)
        ).isoformat()
    elif mutation == "future":
        document["release_evidence"]["completed_at"] = (
            now + timedelta(seconds=1)
        ).isoformat()
    elif mutation == "partial":
        document["results"].pop()
    elif mutation == "duplicate":
        document["results"][1] = document["results"][0]
    elif mutation == "negative":
        document["run"]["usage"]["judge_prompt_tokens"] = -1
    elif mutation == "combined":
        document["run"]["usage"].update(
            judge_prompt_tokens=149999, judge_completion_tokens=100
        )
    elif mutation == "deadline-over-ceiling":
        ceiling = produce.RunConfig.__dataclass_fields__[
            "max_duration_seconds"
        ].default
        document["release_evidence"]["budgets"]["max_duration_seconds"] = ceiling + 1
    elif mutation in ("deadline", "recorded-deadline"):
        limit = 2280 if mutation == "deadline" else 1
        document["release_evidence"]["budgets"]["max_duration_seconds"] = limit
        document["release_evidence"]["started_at"] = (
            now - timedelta(seconds=limit + 1)
        ).isoformat()
        document["release_evidence"]["completed_at"] = now.isoformat()
    else:
        document["results"][0]["usage"]["judge_attempts"] = 7
    assert not gate.gate_release_document(document, candidate, now).passed


@pytest.mark.parametrize(
    "payload", [None, [], "text", {"text": "ok", "citations": [None], "chunks": [None]}]
)
def test_malformed_success_response_rejected(payload):
    transport = FakeTransport([(200, payload)])
    with pytest.raises(ProduceError):
        produce.ask_answer(
            transport,
            "https://fixture/api/answers",
            "question",
            Budget(4, 6),
            lambda _: None,
        )


def test_release_recheck_captures_current_staging(tmp_path, monkeypatch):
    import release

    document, candidate, _ = release_fixture(tmp_path, monkeypatch)
    candidate.update(staging_prefix="stage", shared_prefix="shared")
    document["release_evidence"]["candidate"] = candidate
    produce.write_document(document, tmp_path / "evaluation.json")
    produce.write_document(candidate, tmp_path / "candidate.json")
    monkeypatch.setenv("EVALUATION_RUN_ID", "1:1")
    monkeypatch.setattr(release, "command", lambda *args: "a" * 40)
    observed = []

    def capture(components):
        observed.append(components)
        return candidate

    monkeypatch.setattr(release, "capture", capture)
    args = [
        "--verify-only",
        "--output",
        str(tmp_path / "evaluation.json"),
        "--candidate",
        str(tmp_path / "candidate.json"),
    ]
    assert release.main(args) == 0
    assert observed == [["app", "kb"]]
    monkeypatch.setattr(
        release, "capture", lambda components: {**candidate, "revision": "changed"}
    )
    assert release.main(args) == 1


def test_capture_metadata_deadline_prevents_subprocess(monkeypatch):
    import release

    monkeypatch.setattr(release, "DEADLINE", 10.0)
    monkeypatch.setattr(release.time, "monotonic", lambda: 11.0)

    def unexpected(*args, **kwargs):
        pytest.fail("expired metadata command reached subprocess")

    monkeypatch.setattr(release.subprocess, "run", unexpected)
    with pytest.raises(ProduceError, match="deadline"):
        release.command("az", "fixture")


def test_capture_metadata_errors_do_not_disclose_output(monkeypatch):
    import subprocess

    import release

    monkeypatch.setattr(release, "DEADLINE", None)
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 1, "private-output", "private-error"
        ),
    )
    with pytest.raises(ProduceError) as error:
        release.command("az", "fixture")
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "rows",
    [
        None,
        [],
        [None],
        [{"id": "one"}],
        [{"id": "one", "title": "title", "content": "content", "url": "url"}] * 2,
    ],
)
def test_capture_rejects_malformed_or_duplicate_index_rows(rows):
    import release

    with pytest.raises(ProduceError):
        release.normalized_rows(rows)


def canned_release(monkeypatch):
    """The canned staging deployment behind the capture tests.

    Returns the mutable ``state`` (rows, app, labels, definition,
    provider) and the metadata command seam serving it; tests mutate
    the state, then install the seam.
    """
    import release

    source = "a" * 40
    image = "ghcr.io/o/r@sha256:" + "b" * 64
    monkeypatch.setenv("NAME_PREFIX", "stage")
    monkeypatch.setenv("SHARED_PREFIX", "shared")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("EVALUATION_RUN_ID", "1:1")
    for name in ("INDEX_NAME", "CHAT_DEPLOYMENT_NAME", "EMBEDDING_DEPLOYMENT_NAME"):
        monkeypatch.delenv(name, raising=False)
    state: dict[str, Any] = {
        "rows": [
            {key: getattr(chunk, key) for key in ("id", "title", "content", "url")}
            for chunk in release.chunk_kb(release.ROOT / "kb", "o/r", source)
        ],
        "app": {
            "template": {
                "containers": [
                    {
                        "image": image,
                        "env": [
                            {
                                "name": "Search__Endpoint",
                                "value": "https://shared-srch.search.windows.net",
                            },
                            {
                                "name": "OpenAI__Endpoint",
                                "value": "https://shared-ai.cognitiveservices.azure.com",
                            },
                        ],
                    }
                ]
            },
            "latestReadyRevisionName": "ready",
            "latestRevisionName": "ready",
            "configuration": {
                "activeRevisionsMode": "Single",
                "ingress": {"fqdn": "stage.example"},
            },
        },
        "labels": {
            "org.opencontainers.image.source": "https://github.com/o/r",
            "org.opencontainers.image.revision": source,
        },
        "definition": {
            "vectorSearch": {
                "vectorizers": [
                    {
                        "azureOpenAIParameters": {
                            "deploymentId": "embedding",
                            "resourceUri": "https://shared-ai.cognitiveservices.azure.com",
                        }
                    }
                ]
            }
        },
        "provider": (
            "MaxOutputTokenCount = 256\n"
            "messages.Add(new SystemChatMessage(\n"
            '    """\n'
            "    canned answer prompt\n"
            '    """\n'
            "))\n"
            "new ChatCompletionOptions\n"
        ),
    }

    def metadata(*args):
        if args[:2] == ("git", "rev-parse"):
            return source
        if args[:2] == ("git", "show"):
            return state["provider"]
        if args[:2] == ("docker", "pull"):
            return ""
        if args[:3] == ("docker", "image", "inspect"):
            return json.dumps(state["labels"])
        if args[:3] == ("az", "containerapp", "show"):
            return json.dumps({"properties": state["app"]})
        if args[:2] == ("az", "rest"):
            url = args[args.index("--url") + 1]
            return json.dumps(
                {"value": state["rows"]} if "/docs?" in url else state["definition"]
            )
        if args[:2] == ("az", "cognitiveservices"):
            return json.dumps(
                {
                    "properties": {
                        "model": {"name": "fixture", "version": "1", "format": "OpenAI"}
                    }
                }
            )
        pytest.fail(f"unexpected metadata command {args[0]}")

    return state, metadata


@pytest.mark.parametrize(
    "mutation",
    ["none", "image", "revision", "source", "kb", "chunk", "vectorizer", "cap"],
)
def test_capture_binds_canned_deployment_and_exact_kb(monkeypatch, mutation):
    import release

    state, metadata = canned_release(monkeypatch)
    if mutation == "image":
        state["app"]["template"]["containers"][0]["image"] = "mutable:tag"
    elif mutation == "revision":
        state["app"]["latestRevisionName"] = "unready"
    elif mutation == "source":
        state["labels"]["org.opencontainers.image.source"] = (
            "https://github.com/other/repo"
        )
    elif mutation == "kb":
        state["rows"][0]["url"] = state["rows"][0]["url"].replace("a" * 40, "main")
    elif mutation == "chunk":
        state["rows"][0]["content"] += " changed"
    elif mutation == "vectorizer":
        state["definition"]["vectorSearch"]["vectorizers"][0]["azureOpenAIParameters"][
            "deploymentId"
        ] = "other"
    elif mutation == "cap":
        state["provider"] = "new ChatCompletionOptions"

    monkeypatch.setattr(release, "command", metadata)
    if mutation == "none":
        import hashlib

        candidate = release.capture(["app", "kb"])
        assert candidate["image"] == "ghcr.io/o/r@sha256:" + "b" * 64
        assert candidate["document_count"] == len(state["rows"])
        assert candidate["source_commit"] == "a" * 40
        assert candidate["kb_source_commit"] == "a" * 40
        assert (
            candidate["answer_prompt_sha256"]
            == hashlib.sha256(
                release.answer_prompt(state["provider"]).encode()
            ).hexdigest()
        )
    else:
        with pytest.raises(ProduceError):
            release.capture(["app", "kb"])


@pytest.mark.parametrize(
    ("revisions", "passes"),
    [
        pytest.param(["main"], False, id="all-main"),
        pytest.param(["release/2026"], False, id="all-branch-name"),
        pytest.param(["a" * 40, "c" * 40], False, id="alternating-shas"),
        pytest.param(["abc1234"], False, id="all-short-sha"),
        pytest.param(["a" * 40], True, id="shared-sha-passes"),
    ],
)
def test_capture_demands_one_immutable_kb_source_revision(
    monkeypatch, revisions, passes
):
    import release

    state, metadata = canned_release(monkeypatch)
    rows = state["rows"]
    assert rows
    for index, row in enumerate(rows):
        revision = revisions[index % len(revisions)]
        row["url"] = row["url"].replace(
            "/blob/" + "a" * 40 + "/", f"/blob/{revision}/", 1
        )

    monkeypatch.setattr(release, "command", metadata)
    if passes:
        candidate = release.capture(["app", "kb"])
        assert candidate["kb_source_commit"] == "a" * 40
    else:
        with pytest.raises(
            ProduceError, match="index citations lack one immutable KB source revision"
        ):
            release.capture(["app", "kb"])


def test_an_answer_timeout_is_retried_inside_the_attempt_cap():
    transport = TimeoutThenServingTransport(
        2, produce.HttpResponse(200, json.dumps(answer_body()).encode())
    )
    sleeps: list[float] = []
    budget = Budget(answer_attempt_cap=4, judge_attempt_cap=6)

    outcome = produce.ask_answer(
        transport, "https://fixture/api/answers", "q", budget, sleeps.append
    )

    assert outcome.ok
    assert outcome.status == "200"
    assert sleeps == [
        produce.ANSWER_RETRY_BACKOFF_SECONDS,
        produce.ANSWER_RETRY_BACKOFF_SECONDS * 2,
    ]
    assert budget.answer_attempts == 3


def test_a_judge_timeout_is_retried_inside_the_attempt_cap(tmp_path):
    transport = TimeoutThenServingTransport(
        2, produce.HttpResponse(200, json.dumps(judge_body()).encode())
    )
    sleeps: list[float] = []
    budget = Budget(answer_attempt_cap=0, judge_attempt_cap=6)

    outcome = produce.judge_answer(
        transport, make_config(tmp_path), "key", "prompt", budget, sleeps.append
    )

    assert outcome.groundedness == 5.0
    assert outcome.status == "200"
    assert sleeps == [
        produce.JUDGE_RETRY_BACKOFF_SECONDS,
        produce.JUDGE_RETRY_BACKOFF_SECONDS * 2,
    ]
    assert budget.judge_attempts == 3


def test_exhausted_answer_timeouts_return_a_failed_outcome():
    transport = TimeoutThenServingTransport(99, produce.HttpResponse(200, b"{}"))
    sleeps: list[float] = []
    budget = Budget(
        answer_attempt_cap=produce.ANSWER_ATTEMPTS_PER_QUESTION, judge_attempt_cap=6
    )

    outcome = produce.ask_answer(
        transport, "https://fixture/api/answers", "q", budget, sleeps.append
    )

    assert not outcome.ok
    assert "timed out" in outcome.error
    assert outcome.status == "timeout"
    assert budget.answer_attempts == produce.ANSWER_ATTEMPTS_PER_QUESTION
    assert sleeps == [
        produce.ANSWER_RETRY_BACKOFF_SECONDS * attempt
        for attempt in range(1, produce.ANSWER_ATTEMPTS_PER_QUESTION + 1)
    ]


def test_default_transport_maps_timeout_to_the_retryable_class(monkeypatch):
    def timeout_urlopen(request, timeout):
        raise TimeoutError("socket timed out")

    monkeypatch.setattr(produce.urllib.request, "urlopen", timeout_urlopen)

    with pytest.raises(produce.EndpointTimeout, match="timed out"):
        produce.default_transport("POST", "https://fixture/api/answers", {}, b"", 1.0)

    assert issubclass(produce.EndpointTimeout, produce.ProduceError)


def test_default_transport_keeps_unreachable_endpoints_fatal(monkeypatch):
    def unreachable_urlopen(request, timeout):
        raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr(produce.urllib.request, "urlopen", unreachable_urlopen)

    with pytest.raises(produce.ProduceError, match="unreachable") as error:
        produce.default_transport("POST", "https://fixture/api/answers", {}, b"", 1.0)

    assert not isinstance(error.value, produce.EndpointTimeout)


def test_run_logs_one_warm_up_and_one_line_per_question(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EVAL_TEST_KEY", "sekrit-value")
    config = make_config(tmp_path, questions=2)
    responses = [(200, {})]
    for _ in range(2):
        responses.extend([(200, answer_body()), (200, judge_body())])
    transport = FakeTransport(responses)

    produce.run(config, transport, lambda seconds: None)

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    assert re.fullmatch(r"warm-up status=200 duration=\d+\.\ds", lines[0])
    question_pattern = (
        r"question (\d+)/2 start=\d{2}:\d{2}:\d{2} end=\d{2}:\d{2}:\d{2} "
        r"duration=\d+\.\ds answer=200 judge=200"
    )
    first, second = (re.fullmatch(question_pattern, line) for line in lines[1:])
    assert first and first.group(1) == "1"
    assert second and second.group(1) == "2"


def test_the_warm_up_runs_once_and_a_timeout_does_not_fail_the_run(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("EVAL_TEST_KEY", "sekrit-value")
    config = make_config(tmp_path, questions=2)
    transport = ColdStartTransport()

    document = produce.run(config, transport, lambda seconds: None)

    assert document["gate"]["passed"] is True
    assert transport.calls[0] == ("POST", config.endpoint, "hello")
    assert [call[2] for call in transport.calls] == [
        "hello",
        "question 0",
        None,
        "question 1",
        None,
    ]
    assert document["run"]["usage"]["answer_calls"] == 2
    assert document["run"]["usage"]["answer_attempts"] == 2
    assert document["run"]["usage"]["judge_calls"] == 2
    assert document["run"]["usage"]["judge_attempts"] == 2


@pytest.mark.parametrize(
    ("response", "token"),
    [
        pytest.param((200, {"ok": 1}), "200", id="success"),
        pytest.param((503, {"e": 1}), "HTTP 503", id="http-error"),
    ],
)
def test_the_warm_up_logs_the_response_status(tmp_path, capsys, response, token):
    produce.warm_up(make_config(tmp_path), FakeTransport([response]))

    assert re.fullmatch(
        rf"warm-up status={re.escape(token)} duration=\d+\.\ds",
        capsys.readouterr().out.splitlines()[0],
    )


def test_the_warm_up_survives_timeouts_and_transport_errors(tmp_path, capsys):
    config = make_config(tmp_path)
    for error in (
        produce.EndpointTimeout("endpoint timed out: fixture"),
        ProduceError("endpoint unreachable: fixture"),
    ):
        produce.warm_up(config, ServingThenDeadTransport([], error))  # never raises

        line = capsys.readouterr().out.splitlines()[0]
        assert line.startswith("warm-up status=")
        assert "duration=" in line

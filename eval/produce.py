"""Bound staging evaluation; canned tests prove logic, citations only retrieval.

Credentials stay in the environment and never enter evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import gate

PRODUCER = "eval/produce.py"

ANSWER_PATH = "/api/answers"
DEFAULT_DATASET = Path(__file__).parent / "golden.jsonl"
DEFAULT_JUDGE_DEPLOYMENT = "chat-staging"
DEFAULT_JUDGE_API_VERSION = "2024-10-21"
DEFAULT_KEY_ENV = "SHARED_AI_ACCOUNT_KEY"
RESULTS_DIR = Path(__file__).parent / "results"

# The judge deployment runs at capacity 1 and rate-limits per minute, so
# retries back off well past one renewal period before giving up.
ANSWER_ATTEMPTS_PER_QUESTION = 4
ANSWER_RETRY_BACKOFF_SECONDS = 5.0
ANSWER_TIMEOUT_SECONDS = 120.0
JUDGE_ATTEMPTS_PER_QUESTION = 6
JUDGE_RETRY_BACKOFF_SECONDS = 20.0
JUDGE_TIMEOUT_SECONDS = 60.0
JUDGE_MAX_OUTPUT_TOKENS = 300
JUDGE_TEMPERATURE = 0
QUESTION_PACING_SECONDS = 2.0

# One chunk of retrieved context is bounded so one judge prompt stays
# small; three chunks are retrieved per answer.
CHUNK_CONTENT_LIMIT = 1200

# The deployment's model serves both the producer's provenance and any
# cost statement; rates are per one million tokens.
PRICING_INPUT_PER_1M: float | None = None
PRICING_OUTPUT_PER_1M: float | None = None
PRICING_SOURCE = ""

JUDGE_PROMPT_TEMPLATE = """You are judging one answer from a retrieval-augmented IT-support assistant.

QUESTION:
{question}

GROUND TRUTH (written by the knowledge-base author):
{ground_truth}

RETRIEVED CONTEXT (the chunks the system retrieved for this question):
{context}

ANSWER UNDER TEST:
{answer}

Score strictly on integers:
- groundedness 1-5: is every claim in the answer supported by the retrieved context? 5 means every claim is supported by the context, 1 means the answer contradicts or ignores it.
- relevance 1-5: does the answer correctly and completely answer the question, judged against the ground truth? 5 means correct and complete, 1 means wrong or unhelpful.

A citation being present proves transport only, not quality; judge the answer text itself.

Reply with only one JSON object, no prose, no code fences:
{{"groundedness": <int>, "relevance": <int>, "groundedness_rationale": "<=30 words", "relevance_rationale": "<=30 words"}}"""

Transport = Callable[[str, str, dict[str, str], bytes, float], "HttpResponse"]


@dataclass(frozen=True)
class HttpResponse:
    """One HTTP response from the transport seam."""

    status: int
    body: bytes


class ProduceError(RuntimeError):
    """The run cannot proceed honestly; nothing speculative is written."""


class BudgetExceeded(ProduceError):
    """The request budget is spent; the run stops rather than overspending."""


@dataclass
class Budget:
    """Call and attempt counters, capped before every request."""

    answer_attempt_cap: int
    judge_attempt_cap: int
    answer_attempts: int = 0
    judge_attempts: int = 0
    answer_calls: int = 0
    judge_calls: int = 0
    judge_prompt_tokens: int = 0
    judge_completion_tokens: int = 0
    total_attempt_cap: int = 150
    judge_token_cap: int = 150_000
    answer_output_token_cap: int = 15_360
    reserved_judge_tokens: int = 0
    reserved_answer_output_tokens: int = 0
    deadline: float | None = None

    def remaining_timeout(self, limit: float) -> float:
        if self.deadline is None:
            return limit
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BudgetExceeded("evaluation deadline reached")
        return min(limit, remaining)

    def check_total(self) -> None:
        self.remaining_timeout(1)
        if self.answer_attempts + self.judge_attempts >= self.total_attempt_cap:
            raise BudgetExceeded("aggregate attempt budget spent")

    def charge_answer_attempt(self) -> None:
        self.check_total()
        if self.reserved_answer_output_tokens + 256 > self.answer_output_token_cap:
            raise BudgetExceeded("answer output-token budget spent")
        if self.answer_attempts >= self.answer_attempt_cap:
            raise BudgetExceeded(
                f"answer attempt budget spent: {self.answer_attempts} attempts"
            )
        self.answer_attempts += 1
        self.reserved_answer_output_tokens += 256

    def charge_judge_attempt(self) -> None:
        self.check_total()
        if self.judge_attempts >= self.judge_attempt_cap:
            raise BudgetExceeded(
                f"judge attempt budget spent: {self.judge_attempts} attempts"
            )
        self.judge_attempts += 1

    def usage_block(self) -> dict[str, Any]:
        """What the run actually spent, from response and call counters."""
        return {
            "answer_calls": self.answer_calls,
            "answer_attempts": self.answer_attempts,
            "judge_calls": self.judge_calls,
            "judge_attempts": self.judge_attempts,
            "judge_prompt_tokens": self.judge_prompt_tokens,
            "judge_completion_tokens": self.judge_completion_tokens,
            "answer_path_tokens": "not exposed by the answers endpoint",
        }


@dataclass(frozen=True)
class AnswerOutcome:
    """What one answer request returned, or why it failed."""

    ok: bool
    text: str = ""
    citations: tuple[tuple[str, str], ...] = ()
    chunks: tuple[tuple[str, str], ...] = ()
    error: str = ""


@dataclass(frozen=True)
class JudgmentOutcome:
    """What one judge request scored, or why it failed."""

    groundedness: float | None = None
    relevance: float | None = None
    groundedness_rationale: str = ""
    relevance_rationale: str = ""
    model: str = ""
    error: str = ""


def response_document(response: HttpResponse) -> dict:
    """Malformed successful responses are failures, never coerced evidence."""
    try:
        document = json.loads(response.body.decode("utf-8"))
    except (ValueError, UnicodeError) as error:
        raise ProduceError("malformed endpoint JSON") from error
    if not isinstance(document, dict):
        raise ProduceError("endpoint response is not an object")
    return document


@dataclass
class GoldenQuestion:
    """One row of the golden dataset."""

    query: str
    expected_article: str
    ground_truth: str


@dataclass
class RunConfig:
    """Everything one live run needs, checked before the first request."""

    endpoint: str
    answer_revision: str
    answer_image: str
    judge_host: str
    judge_deployment: str
    judge_api_version: str
    dataset_path: Path
    key_env: str
    output_path: Path
    candidate: dict[str, Any] | None = None
    run_id: str = ""
    started_at: str = ""
    max_duration_seconds: int = 900
    expires_after_seconds: int = 7200


def default_transport(
    method: str, url: str, headers: dict[str, str], body: bytes, timeout: float
) -> HttpResponse:
    """Send one HTTP request; HTTP error statuses are returned, not raised."""
    request = urllib.request.Request(
        url, data=body if body else None, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(response.status, response.read())
    except urllib.error.HTTPError as error:
        return HttpResponse(error.code, error.read())
    except urllib.error.URLError as error:
        raise ProduceError(f"endpoint unreachable: {url}: {error.reason}") from error
    except TimeoutError as error:
        raise ProduceError(f"endpoint timed out: {url}") from error


def load_dataset(path: Path) -> list[GoldenQuestion]:
    """Load and validate the golden question set; empty input is refused."""
    questions: list[GoldenQuestion] = []
    for number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ProduceError(f"{path}:{number}: question is not an object")
        missing = [
            key
            for key in ("query", "expected_article", "ground_truth")
            if not isinstance(row.get(key), str) or not row[key]
        ]
        if missing:
            raise ProduceError(f"{path}:{number}: missing {', '.join(missing)}")
        questions.append(
            GoldenQuestion(
                query=row["query"],
                expected_article=row["expected_article"],
                ground_truth=row["ground_truth"],
            )
        )
    if not questions:
        raise ProduceError(f"{path}: the golden dataset is empty; refusing to run")
    if len({question.query for question in questions}) != len(questions):
        raise ProduceError("duplicate golden question")
    return questions


def retrieval_hit(
    filenames: tuple[str, ...], expected_article: str, k: int
) -> tuple[bool, int | None]:
    """Share the gate's retrieval verdict and return a one-based citation rank."""
    if not gate.hit_at_k(filenames, expected_article, k):
        return False, None
    return True, filenames.index(expected_article) + 1


def parse_judgment(text: str) -> tuple[float | None, float | None, str, str, str]:
    """Return groundedness, relevance, both rationales and a failure message."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        candidate = candidate.removeprefix("json")
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        return None, None, "", "", "the judge reply carries no JSON object"
    try:
        data = json.loads(candidate[start : end + 1], strict=False)
    except json.JSONDecodeError as error:
        return None, None, "", "", f"the judge reply is not valid JSON: {error}"
    groundedness = _score(data.get("groundedness"))
    relevance = _score(data.get("relevance"))
    if groundedness is None or relevance is None:
        return None, None, "", "", "the judge reply carries no 1-to-5 scores"
    return (
        groundedness,
        relevance,
        str(data.get("groundedness_rationale", ""))[:300],
        str(data.get("relevance_rationale", ""))[:300],
        "",
    )


def _score(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = float(value)
    return score if 1.0 <= score <= 5.0 else None


def _retryable(status: int) -> bool:
    """429 and transient 5xx are retried inside the attempt budget."""
    return status == 429 or 500 <= status <= 599


def ask_answer(
    transport: Transport,
    endpoint: str,
    question: str,
    budget: Budget,
    sleep: Callable[[float], None],
) -> AnswerOutcome:
    """Ask the deployed answer path one question; one success, capped tries."""
    body = json.dumps({"question": question}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    error = ""
    for attempt in range(ANSWER_ATTEMPTS_PER_QUESTION):
        budget.charge_answer_attempt()
        response = transport(
            "POST",
            endpoint,
            headers,
            body,
            budget.remaining_timeout(ANSWER_TIMEOUT_SECONDS),
        )
        if response.status == 200:
            document = response_document(response)
            if (
                not isinstance(document.get("text"), str)
                or not document["text"].strip()
            ):
                raise ProduceError("answer contains no text")
            for field, keys in (
                ("citations", ("title", "url")),
                ("chunks", ("title", "content")),
            ):
                items = document.get(field)
                if (
                    not isinstance(items, list)
                    or not items
                    or len(items) > gate.DEFAULT_K
                    or any(
                        not isinstance(item, dict)
                        or any(
                            not isinstance(item.get(key), str) or not item[key]
                            for key in keys
                        )
                        for item in items
                    )
                ):
                    raise ProduceError(f"malformed answer {field}")
            budget.answer_calls += 1
            return AnswerOutcome(
                ok=True,
                text=str(document.get("text", "")),
                citations=tuple(
                    (str(item.get("title", "")), str(item.get("url", "")))
                    for item in document.get("citations") or []
                ),
                chunks=tuple(
                    (str(item.get("title", "")), str(item.get("content", "")))
                    for item in document.get("chunks") or []
                ),
            )
        error = (
            f"HTTP {response.status}: {response.body[:200].decode('utf-8', 'replace')}"
        )
        if _retryable(response.status):
            sleep(ANSWER_RETRY_BACKOFF_SECONDS * (attempt + 1))
            continue
        break
    return AnswerOutcome(ok=False, error=error or "the answer attempts were exhausted")


def judge_answer(
    transport: Transport,
    config: RunConfig,
    api_key: str,
    prompt: str,
    budget: Budget,
    sleep: Callable[[float], None],
) -> JudgmentOutcome:
    """Judge one answer with one bounded completion call; capped tries."""
    url = (
        f"https://{config.judge_host}/openai/deployments/"
        f"{config.judge_deployment}/chat/completions"
        f"?api-version={config.judge_api_version}"
    )
    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": JUDGE_MAX_OUTPUT_TOKENS,
        "temperature": JUDGE_TEMPERATURE,
    }
    body = json.dumps(payload).encode("utf-8")
    if len(body) > 8000:
        raise BudgetExceeded("judge input exceeds 8000 UTF-8 bytes")
    headers = {"Content-Type": "application/json", "api-key": api_key}
    error = ""
    for attempt in range(JUDGE_ATTEMPTS_PER_QUESTION):
        budget.charge_judge_attempt()
        reservation = len(body) + JUDGE_MAX_OUTPUT_TOKENS
        if budget.reserved_judge_tokens + reservation > budget.judge_token_cap:
            raise BudgetExceeded("judge token reservation budget spent")
        budget.reserved_judge_tokens += reservation
        response = transport(
            "POST", url, headers, body, budget.remaining_timeout(JUDGE_TIMEOUT_SECONDS)
        )
        if response.status == 200:
            document = response_document(response)
            usage = document.get("usage")
            if not isinstance(usage, dict) or any(
                type(usage.get(key)) is not int or usage[key] < 0
                for key in ("prompt_tokens", "completion_tokens")
            ):
                raise ProduceError("missing or malformed judge token usage")
            if (
                usage["completion_tokens"] > JUDGE_MAX_OUTPUT_TOKENS
                or sum(usage[key] for key in ("prompt_tokens", "completion_tokens"))
                > reservation
            ):
                raise BudgetExceeded("judge response exceeds its reservation")
            budget.judge_prompt_tokens += usage["prompt_tokens"]
            budget.judge_completion_tokens += usage["completion_tokens"]
            if (
                budget.judge_prompt_tokens + budget.judge_completion_tokens
                > budget.judge_token_cap
            ):
                raise BudgetExceeded("aggregate actual judge-token budget spent")
            choices = document.get("choices")
            if (
                not isinstance(choices, list)
                or len(choices) != 1
                or not isinstance(choices[0], dict)
                or not isinstance(choices[0].get("message"), dict)
            ):
                raise ProduceError("malformed judge choices")
            content = choices[0]["message"].get("content")
            if (
                not isinstance(content, str)
                or not isinstance(document.get("model"), str)
                or not document["model"]
            ):
                raise ProduceError("missing judge content or model")
            budget.judge_calls += 1
            groundedness, relevance, why_grounded, why_relevant, parse_error = (
                parse_judgment(content)
            )
            return JudgmentOutcome(
                groundedness=groundedness,
                relevance=relevance,
                groundedness_rationale=why_grounded,
                relevance_rationale=why_relevant,
                model=str(document.get("model", "")),
                error=parse_error,
            )
        error = (
            f"HTTP {response.status}: {response.body[:200].decode('utf-8', 'replace')}"
        )
        if _retryable(response.status):
            sleep(JUDGE_RETRY_BACKOFF_SECONDS * (attempt + 1))
            continue
        break
    return JudgmentOutcome(error=error or "the judge attempts were exhausted")


def format_context(chunks: tuple[tuple[str, str], ...]) -> str:
    """Bound the retrieved context fed to the judge."""
    return "\n\n".join(
        f"[{title}]\n{content[:CHUNK_CONTENT_LIMIT]}" for title, content in chunks
    )


def cost_block(budget: Budget) -> dict[str, Any]:
    """State the cost basis only when it is derivable; never invent one."""
    if PRICING_INPUT_PER_1M is None or PRICING_OUTPUT_PER_1M is None:
        return {
            "status": "cost not computed: no verified per-token price is cited",
            "currency": "",
            "input_per_1m_tokens": None,
            "output_per_1m_tokens": None,
            "basis": "",
            "estimate": None,
        }
    estimate = (
        budget.judge_prompt_tokens / 1_000_000 * PRICING_INPUT_PER_1M
        + budget.judge_completion_tokens / 1_000_000 * PRICING_OUTPUT_PER_1M
    )
    return {
        "status": "computed",
        "currency": "USD",
        "input_per_1m_tokens": PRICING_INPUT_PER_1M,
        "output_per_1m_tokens": PRICING_OUTPUT_PER_1M,
        "basis": PRICING_SOURCE,
        "estimate": round(estimate, 6),
    }


def run(
    config: RunConfig, transport: Transport, sleep: Callable[[float], None]
) -> dict[str, Any]:
    """Run the golden set end to end and return the results document."""
    api_key = os.environ.get(config.key_env, "")
    if not api_key:
        raise ProduceError(
            f"missing credential: set {config.key_env} in the environment; "
            "the key is read from the environment only and is never written out"
        )
    questions = load_dataset(config.dataset_path)
    dataset_bytes = config.dataset_path.read_bytes()
    config.started_at = datetime.now(UTC).isoformat()
    budget = Budget(
        answer_attempt_cap=ANSWER_ATTEMPTS_PER_QUESTION * len(questions),
        judge_attempt_cap=JUDGE_ATTEMPTS_PER_QUESTION * len(questions),
        deadline=time.monotonic() + config.max_duration_seconds,
    )
    original_sleep = sleep

    def bounded_sleep(seconds: float) -> None:
        remaining = budget.remaining_timeout(seconds)
        if remaining < seconds:
            raise BudgetExceeded("evaluation deadline reached before backoff")
        original_sleep(seconds)
        budget.remaining_timeout(1)

    sleep = bounded_sleep
    rows: list[dict[str, Any]] = []
    judge_model = ""
    for question in questions:
        counters_before = {
            key: getattr(budget, key)
            for key in (
                "answer_attempts",
                "judge_attempts",
                "judge_prompt_tokens",
                "judge_completion_tokens",
                "reserved_judge_tokens",
            )
        }
        question_model = ""
        answer = ask_answer(transport, config.endpoint, question.query, budget, sleep)
        row: dict[str, Any] = {
            "query": question.query,
            "expected_article": question.expected_article,
            "answer_text": answer.text if answer.ok else None,
            "citations": [list(citation) for citation in answer.citations],
            "answer_error": answer.error if not answer.ok else None,
        }
        judgment_error = ""
        if not answer.ok:
            judgment_error = "no answer to judge"
            filenames: tuple[str, ...] = ()
            hit, rank = False, None
        else:
            filenames = gate.citation_filenames(answer.citations) or ()
            hit, rank = retrieval_hit(
                filenames, question.expected_article, gate.DEFAULT_K
            )
            prompt = JUDGE_PROMPT_TEMPLATE.format(
                question=question.query,
                ground_truth=question.ground_truth,
                context=format_context(answer.chunks),
                answer=answer.text,
            )
            judgment = judge_answer(transport, config, api_key, prompt, budget, sleep)
            judgment_error = judgment.error
            question_model = judgment.model
            if judge_model and judgment.model != judge_model:
                raise ProduceError("judge model changed during evaluation")
            if judgment.model and not judge_model:
                judge_model = judgment.model
            row.update(
                {
                    "groundedness": judgment.groundedness,
                    "relevance": judgment.relevance,
                    "groundedness_rationale": judgment.groundedness_rationale,
                    "relevance_rationale": judgment.relevance_rationale,
                }
            )
        row.update(
            {
                "retrieval_filenames": list(filenames),
                "retrieval_hit": hit,
                "retrieval_rank": rank,
                "judgment_error": judgment_error or None,
            }
        )
        row["usage"] = {
            key: getattr(budget, key) - value for key, value in counters_before.items()
        }
        row["usage"]["judge_model"] = question_model
        rows.append(row)
        write_document(
            document_so_far(
                config, questions, dataset_bytes, budget, rows, judge_model
            ),
            config.output_path,
        )
        sleep(QUESTION_PACING_SECONDS)
    document = document_so_far(
        config, questions, dataset_bytes, budget, rows, judge_model
    )
    golden = gate.GoldenDataset(
        path=str(config.dataset_path),
        sha256=hashlib.sha256(dataset_bytes).hexdigest(),
        questions=tuple(
            (question.query, question.expected_article) for question in questions
        ),
    )
    verdict = gate.gate_document(document, golden)
    document["gate"] = {
        "passed": verdict.passed,
        "failures": list(verdict.failures),
        "mean_groundedness": _mean(
            row.get("groundedness")
            for row in rows
            if row.get("groundedness") is not None
        ),
        "mean_relevance": _mean(
            row.get("relevance") for row in rows if row.get("relevance") is not None
        ),
        "retrieval_hits": sum(1 for row in rows if row["retrieval_hit"]),
        "judged": sum(1 for row in rows if row.get("groundedness") is not None),
    }
    if config.candidate is not None:
        document["release_evidence"] = {
            "run_id": config.run_id,
            "started_at": config.started_at,
            "completed_at": datetime.now(UTC).isoformat(),
            "expires_at": (
                datetime.fromisoformat(config.started_at)
                + timedelta(seconds=config.expires_after_seconds)
            ).isoformat(),
            "candidate": config.candidate,
            "prompt_sha256": hashlib.sha256(JUDGE_PROMPT_TEMPLATE.encode()).hexdigest(),
            "budgets": {
                "answer_attempts_per_question": ANSWER_ATTEMPTS_PER_QUESTION,
                "judge_attempts_per_question": JUDGE_ATTEMPTS_PER_QUESTION,
                "total_attempt_cap": budget.total_attempt_cap,
                "answer_max_output_tokens": 256,
                "answer_output_token_cap": budget.answer_output_token_cap,
                "judge_max_output_tokens": JUDGE_MAX_OUTPUT_TOKENS,
                "judge_input_byte_cap": 8000,
                "judge_token_cap": budget.judge_token_cap,
                "max_duration_seconds": config.max_duration_seconds,
            },
            "usage": {
                "reserved_answer_output_tokens": budget.reserved_answer_output_tokens,
                "reserved_judge_tokens": budget.reserved_judge_tokens,
            },
        }
    return document


def _mean(values: Any) -> float | None:
    numbers = list(values)
    return round(sum(numbers) / len(numbers), 3) if numbers else None


def document_so_far(
    config: RunConfig,
    questions: list[GoldenQuestion],
    dataset_bytes: bytes,
    budget: Budget,
    rows: list[dict[str, Any]],
    judge_model: str,
) -> dict[str, Any]:
    """Assemble the results document with its provenance block."""
    return {
        "run": {
            "produced_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "producer": PRODUCER,
            "answer_endpoint": config.endpoint,
            "answer_revision": config.answer_revision,
            "answer_image": config.answer_image,
            "retrieval": (
                "the production retrieval path inside the deployed app: hybrid "
                "full-text plus server-vectorized text query with semantic "
                "ranking over the knowledge-base index (three chunks per answer)"
            ),
            "dataset": {
                "path": str(config.dataset_path),
                "sha256": hashlib.sha256(dataset_bytes).hexdigest(),
                "questions": len(questions),
            },
            "judge": {
                "endpoint_host": config.judge_host,
                "deployment": config.judge_deployment,
                "model": judge_model or "unavailable: no judge call succeeded",
                "api_version": config.judge_api_version,
                "prompt": JUDGE_PROMPT_TEMPLATE,
                "max_output_tokens": JUDGE_MAX_OUTPUT_TOKENS,
                "temperature": JUDGE_TEMPERATURE,
            },
            "thresholds": {
                "min_groundedness": gate.MIN_GROUNDEDNESS,
                "min_mean_relevance": gate.MIN_MEAN_RELEVANCE,
                "hit_at_k": gate.DEFAULT_K,
            },
            "usage": budget.usage_block(),
            "cost": cost_block(budget),
        },
        "results": [dict(row) for row in rows],
    }


def write_document(document: dict[str, Any], output_path: Path) -> None:
    """Write the results document; the parent directory is created."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    """Write and gate evidence: exit 0 pass, 3 partial, 4 empty, 5 quality failure."""
    parser = argparse.ArgumentParser(
        description=(
            "Run the golden question set through the deployed answer path, "
            "judge every answer with a bounded model call, and write one "
            "results document for a deliberate trusted staging run."
        )
    )
    parser.add_argument("--endpoint", required=True, help="answers endpoint URL")
    parser.add_argument("--revision", required=True, help="deployed revision name")
    parser.add_argument("--image", required=True, help="deployed image reference")
    parser.add_argument("--judge-host", required=True, help="Azure OpenAI host name")
    parser.add_argument("--deployment", default=DEFAULT_JUDGE_DEPLOYMENT)
    parser.add_argument("--api-version", default=DEFAULT_JUDGE_API_VERSION)
    parser.add_argument("--key-env", default=DEFAULT_KEY_ENV)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=None)
    arguments = parser.parse_args(argv)
    output = arguments.output or (
        RESULTS_DIR / f"{datetime.now(UTC).date().isoformat()}-golden-run.json"
    )
    config = RunConfig(
        endpoint=arguments.endpoint,
        answer_revision=arguments.revision,
        answer_image=arguments.image,
        judge_host=arguments.judge_host,
        judge_deployment=arguments.deployment,
        judge_api_version=arguments.api_version,
        dataset_path=arguments.dataset,
        key_env=arguments.key_env,
        output_path=output,
    )
    try:
        document = run(config, default_transport, time.sleep)
    except ProduceError as error:
        print(f"produce: {error}", file=sys.stderr)
        return 3
    if not any(row["answer_text"] is not None for row in document["results"]):
        # The incremental writer left the partial document behind; it is
        # kept as evidence of how far the run reached, and the gate
        # rejects it rather than letting an incomplete run pass.
        print(
            "produce: no question received an answer; the partial results "
            f"document at {config.output_path} is kept as evidence and "
            "fails the gate",
            file=sys.stderr,
        )
        return 4
    write_document(document, config.output_path)
    summary = document["gate"]
    if not summary["passed"]:
        print(
            "produce: the gate rejected the completed results document "
            f"with {len(summary['failures'])} failure(s)",
            file=sys.stderr,
        )
        return 5
    print(
        f"produce: wrote {config.output_path}; "
        f"{summary['judged']}/{len(document['results'])} judged, "
        f"{summary['retrieval_hits']}/{len(document['results'])} retrieval hits, "
        f"gate {'passed' if summary['passed'] else 'FAILED'} "
        f"with {len(summary['failures'])} failure(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

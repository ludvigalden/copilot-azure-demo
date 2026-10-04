"""The real evaluation producer over the golden question set.

Runs every golden question through the deployed staging answer path —
``POST /api/answers`` on the running app, which performs the production
hybrid retrieval (full-text plus a server-vectorized text query with
semantic ranking over the knowledge-base index) and a grounded chat
completion — then judges each answer with one bounded Azure OpenAI call
against the same chat deployment the app itself uses, and writes one
results document for the gate in ``gate.py``.

This is a deliberate manual run, never wired into CI. Bounded by
construction: exactly one answer request per question and one judge
request per question, with per-request attempt caps, a total-attempt
budget enforced before every request, and a completion-token cap on the
judge call. The deterministic retrieval check needs no model at all: it
compares the article filenames in the citations the endpoint actually
returned against the expected article in the dataset. The judge scores
groundedness against the retrieved context and relevance against the
dataset's ground truth; a citation being present proves transport only
and is never treated as quality.

Credentials come from the environment only. The account key names the
judge's ``api-key`` header and is never written to the results document
or the log.
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
from datetime import UTC, datetime
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

Transport = Callable[[str, str, dict[str, str], bytes], "HttpResponse"]


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

    def charge_answer_attempt(self) -> None:
        if self.answer_attempts >= self.answer_attempt_cap:
            raise BudgetExceeded(
                f"answer attempt budget spent: {self.answer_attempts} attempts"
            )
        self.answer_attempts += 1

    def charge_judge_attempt(self) -> None:
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


def default_transport(
    method: str, url: str, headers: dict[str, str], body: bytes
) -> HttpResponse:
    """Send one HTTP request; HTTP error statuses are returned, not raised."""
    request = urllib.request.Request(
        url, data=body if body else None, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=JUDGE_TIMEOUT_SECONDS) as response:
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
    return questions


def citation_filenames(citations: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
    """Extract deduplicated article filenames from citation URLs, in order."""
    names: list[str] = []
    for _, url in citations:
        tail = url.split("://", 1)[-1].rsplit("/", 1)[-1]
        name = tail.split("?", 1)[0].split("#", 1)[0]
        if name and name not in names:
            names.append(name)
    return tuple(names)


def retrieval_hit(
    filenames: tuple[str, ...], expected_article: str, k: int
) -> tuple[bool, int | None]:
    """Deterministic retrieval verdict: the gate's arithmetic, plus a rank.

    Membership is decided by ``gate.hit_at_k`` so the producer and the
    gate share one definition of a retrieval hit; the rank is the
    one-based position of the expected article among the returned
    citations.
    """
    if not gate.hit_at_k(filenames, expected_article, k):
        return False, None
    return True, filenames.index(expected_article) + 1


def parse_judgment(text: str) -> tuple[float | None, float | None, str, str, str]:
    """Parse the judge reply into scores and rationales.

    Returns ``(groundedness, relevance, groundedness_rationale,
    relevance_rationale, error)``; the scores are ``None`` whenever the
    reply carries no usable judgment, and the error says why.
    """
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
        response = transport("POST", endpoint, headers, body)
        if response.status == 200:
            document = json.loads(response.body.decode("utf-8"), strict=False)
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
    headers = {"Content-Type": "application/json", "api-key": api_key}
    error = ""
    for attempt in range(JUDGE_ATTEMPTS_PER_QUESTION):
        budget.charge_judge_attempt()
        response = transport("POST", url, headers, body)
        if response.status == 200:
            document = json.loads(response.body.decode("utf-8"), strict=False)
            budget.judge_calls += 1
            usage = document.get("usage") or {}
            budget.judge_prompt_tokens += int(usage.get("prompt_tokens") or 0)
            budget.judge_completion_tokens += int(usage.get("completion_tokens") or 0)
            content = str(
                (document.get("choices") or [{}])[0]
                .get("message", {})
                .get("content", "")
            )
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
    budget = Budget(
        answer_attempt_cap=ANSWER_ATTEMPTS_PER_QUESTION * len(questions),
        judge_attempt_cap=JUDGE_ATTEMPTS_PER_QUESTION * len(questions),
    )
    rows: list[dict[str, Any]] = []
    judge_model = ""
    for question in questions:
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
            filenames = citation_filenames(answer.citations)
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
    verdict = gate.gate_document(document)
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
    """Produce one live results document; exit 0 completed, nonzero blocked."""
    parser = argparse.ArgumentParser(
        description=(
            "Run the golden question set through the deployed answer path, "
            "judge every answer with a bounded model call, and write one "
            "results document. A deliberate manual run; never wired into CI."
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
        # The incremental writer may have left a partial document behind;
        # a run in which nothing was answered leaves no results file.
        config.output_path.unlink(missing_ok=True)
        print(
            "produce: no question received an answer; the live run cannot "
            "complete, so no results document is written",
            file=sys.stderr,
        )
        return 4
    write_document(document, config.output_path)
    summary = document["gate"]
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

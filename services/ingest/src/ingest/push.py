"""Push the chunked knowledge base into the Azure AI Search index.

Every chunk becomes one index document carrying exactly the fields the
Terraform-owned index defines: ``id``, ``title``, ``content``, ``url``
and the vector ``embedding``. The embeddings are produced against the
``embedding`` deployment of the Azure OpenAI account, authenticated
with an Entra token (no API key anywhere).

The deployment runs at capacity 1, which Azure rate-limits to one
request and 1,000 tokens per minute, so the texts are grouped into
batches sized to stay inside that budget and the requests are paced a
renewal period apart. Documents that the index holds but the knowledge
base no longer produces are deleted, so a re-run converges the index
on ``kb/``.
"""

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from .chunking import Chunk

EMBEDDING_DEPLOYMENT = "embedding"
INDEX_NAME = "kb"
OPENAI_API_VERSION = "2024-10-21"
OPENAI_TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"

# Embedding deployment rate limits at capacity 1 (verified live): one
# request and 1,000 tokens per 60-second period. The batch budget stays
# under the token cap; the pause spans renewal periods.
EMBEDDING_BATCH_TOKENS = 700
EMBEDDING_PAUSE_SECONDS = 65.0
_CHARS_PER_TOKEN = 5


def search_endpoint(prefix: str) -> str:
    """Return the AI Search endpoint for a ``NAME_PREFIX``-named service."""
    return f"https://{prefix}-srch.search.windows.net"


def openai_endpoint(prefix: str) -> str:
    """Return the Azure OpenAI endpoint for a ``NAME_PREFIX``-named account."""
    return f"https://{prefix}-ai.cognitiveservices.azure.com/"


@dataclass(frozen=True)
class Summary:
    """What one push run did, as printed by the command line."""

    pushed: int
    deleted: int


def to_documents(
    chunks: Sequence[Chunk], embeddings: Sequence[Sequence[float]]
) -> list[dict]:
    """Pair chunks with their embeddings as index documents.

    The document shape is the index schema and nothing else — the
    chunk's ``section`` heading is deliberately not pushed.
    """
    if len(chunks) != len(embeddings):
        raise ValueError(
            f"{len(chunks)} chunks but {len(embeddings)} embeddings; "
            "the embedding call and the chunker disagree"
        )
    return [
        {
            "id": chunk.id,
            "title": chunk.title,
            "content": chunk.content,
            "url": chunk.url,
            "embedding": list(embedding),
        }
        for chunk, embedding in zip(chunks, embeddings, strict=True)
    ]


def stale_ids(existing: Iterable[str], produced: Iterable[str]) -> list[str]:
    """Return the index document ids the knowledge base no longer produces."""
    return sorted(set(existing) - set(produced))


def embed_all(
    embeddings_client,
    texts: Sequence[str],
    *,
    sleep: Callable[[float], None] = time.sleep,
    model: str = EMBEDDING_DEPLOYMENT,
) -> list[list[float]]:
    """Embed the texts in quota-sized batches, preserving the input order.

    The batches stay under the deployment's token budget and the
    requests are paced a renewal period apart; ``sleep`` is injectable
    for the tests.
    """
    embeddings: list[list[float]] = []
    for number, batch in enumerate(plan_batches(texts)):
        if number:
            sleep(EMBEDDING_PAUSE_SECONDS)
        response = embeddings_client.embeddings.create(
            model=model, input=batch
        )
        embeddings.extend(
            item.embedding
            for item in sorted(response.data, key=lambda item: item.index)
        )
    return embeddings


def plan_batches(
    texts: Sequence[str], max_batch_tokens: float = EMBEDDING_BATCH_TOKENS
) -> list[list[str]]:
    """Greedily group texts so one embedding request stays inside the budget.

    Token counts are estimated from the character length; the estimate
    keeps a margin against the deployment's 1,000-token rate limit.
    """
    batches: list[list[str]] = []
    current: list[str] = []
    used = 0.0
    for text in texts:
        cost = len(text) / _CHARS_PER_TOKEN
        if current and used + cost > max_batch_tokens:
            batches.append(current)
            current, used = [], 0.0
        current.append(text)
        used += cost
    if current:
        batches.append(current)
    return batches


def existing_ids(search_client) -> set[str]:
    """Return every document id currently in the index."""
    results = search_client.search(search_text="*", select=["id"])
    return {result["id"] for result in results}


def push_kb(
    chunks: Sequence[Chunk], search_client, embeddings_client, *, embedding_deployment: str = EMBEDDING_DEPLOYMENT
) -> Summary:
    """Upload the chunks and delete whatever the index holds beyond them."""
    documents = (
        to_documents(
            chunks,
            embed_all(
                embeddings_client,
                [c.content for c in chunks],
                model=embedding_deployment,
            ),
        )
        if chunks
        else []
    )
    results = search_client.merge_or_upload_documents(documents=documents)
    failed = [result.key for result in results if not result.succeeded]
    if failed:
        raise RuntimeError(f"indexing failed for {len(failed)} documents: {failed}")

    stale = stale_ids(existing_ids(search_client), [chunk.id for chunk in chunks])
    if stale:
        deletions = search_client.delete_documents(
            documents=[{"id": document_id} for document_id in stale]
        )
        failed = [result.key for result in deletions if not result.succeeded]
        if failed:
            raise RuntimeError(f"deletion failed for {len(failed)} documents: {failed}")

    return Summary(pushed=len(documents), deleted=len(stale))

"""Tests for the index-push logic (the pure parts)."""

import pytest

from ingest.chunking import Chunk
from ingest.push import (
    embed_all,
    openai_endpoint,
    plan_batches,
    search_endpoint,
    stale_ids,
    to_documents,
)

CHUNK = Chunk(
    id="reset-password-1",
    title="Reset your password",
    section="When to use this article",
    content="Follow these steps if you have forgotten your password.",
    url="https://github.com/acme/it-demo/blob/main/kb/reset-password.md",
)
EMBEDDING = [0.1, 0.2, 0.3]


def test_document_carries_exactly_the_index_fields():
    documents = to_documents([CHUNK], [EMBEDDING])

    assert documents == [
        {
            "id": "reset-password-1",
            "title": "Reset your password",
            "content": "Follow these steps if you have forgotten your password.",
            "url": "https://github.com/acme/it-demo/blob/main/kb/reset-password.md",
            "embedding": [0.1, 0.2, 0.3],
        }
    ]


def test_chunk_and_embedding_counts_must_agree():
    with pytest.raises(ValueError, match="disagree"):
        to_documents([CHUNK, CHUNK], [EMBEDDING])


def test_stale_ids_are_only_what_the_kb_stopped_producing():
    existing = {"vpn-1", "printer-1", "printer-2", "reset-password-1"}
    produced = {"printer-1", "printer-2", "reset-password-1"}

    assert stale_ids(existing, produced) == ["vpn-1"]


def test_endpoints_derive_from_the_name_prefix():
    assert search_endpoint("copaz") == "https://copaz-srch.search.windows.net"
    assert openai_endpoint("copaz") == "https://copaz-ai.cognitiveservices.azure.com/"


def test_batches_stay_within_the_token_budget_in_order():
    texts = ["x" * 500 for _ in range(8)]  # 100 tokens each at 5 chars/token

    batches = plan_batches(texts, max_batch_tokens=700)

    assert [text for batch in batches for text in batch] == texts
    assert all(sum(len(text) / 5 for text in batch) <= 700 for batch in batches)
    assert len(batches) == 2


def test_no_texts_means_no_batches():
    assert plan_batches([]) == []


class RecordingEmbeddings:
    """Fake embeddings endpoint that reports the batch contents back."""

    def __init__(self):
        self.batches: list[list[str]] = []
        self.embeddings = self  # the SDK nests ``create`` under ``.embeddings``

    def create(self, model, input):
        self.batches.append(input)

        class Item:
            def __init__(self, index):
                self.index = index
                self.embedding = [float(index)]

        class Response:
            def __init__(self, items):
                self.data = items

        return Response([Item(index) for index in range(len(input))])


def test_embed_all_paces_one_pause_between_batches():
    client = RecordingEmbeddings()
    pauses: list[float] = []

    embeddings = embed_all(
        client, ["x" * 500] * 8, sleep=pauses.append
    )  # two batches: 700 tokens, then 100

    assert client.batches == [["x" * 500] * 7, ["x" * 500]]
    assert pauses == [65.0]
    assert embeddings == [[float(i)] for i in range(7)] + [[0.0]]

"""Hermetic prompt-hash precision and shared source-hash helper controls."""

import hashlib

import pytest

import produce
import release
from produce import ProduceError

RUNTIME_PROMPT = (
    "You answer IT-support questions for company employees.\n"
    "Answer only from the sources provided by the user, in at most four sentences.\n"
    "If the sources do not cover the question, say that the knowledge base has no answer."
)


def deployed_source() -> str:
    return (release.ROOT / release.ANSWER_PROMPT_SOURCE).read_text()


def test_answer_prompt_extraction_reproduces_the_runtime_string():
    source = deployed_source()
    extracted = release.answer_prompt(source)
    assert extracted == RUNTIME_PROMPT
    assert (
        hashlib.sha256(extracted.encode()).hexdigest()
        != hashlib.sha256(source.encode()).hexdigest()
    )


def test_prompt_text_mutation_changes_the_hash():
    source = deployed_source().replace("four sentences", "five sentences")
    extracted = release.answer_prompt(source)
    assert extracted != RUNTIME_PROMPT
    assert (
        hashlib.sha256(extracted.encode()).hexdigest()
        != hashlib.sha256(RUNTIME_PROMPT.encode()).hexdigest()
    )


def test_comment_only_change_keeps_the_hash():
    source = deployed_source().replace(
        "new SystemChatMessage(",
        "// tuning note\n        new SystemChatMessage(",
        1,
    )
    assert release.answer_prompt(source) == RUNTIME_PROMPT


def test_missing_system_prompt_fails_closed():
    marker = "new SystemChatMessage(\n" + " " * 24 + '"""'
    assert marker in deployed_source()
    source = deployed_source().replace(marker, 'new SystemChatMessage("x"', 1)
    with pytest.raises(ProduceError, match="lacks the answer prompt"):
        release.answer_prompt(source)


def test_outdented_prompt_line_fails_closed():
    marker = " " * 24 + "You answer IT-support questions"
    assert marker in deployed_source()
    replacement = " " * 12 + "You answer IT-support questions"
    source = deployed_source().replace(marker, replacement, 1)
    with pytest.raises(ProduceError, match="indentation"):
        release.answer_prompt(source)


def test_shared_source_hashes_match_the_producer_constants():
    assert release.authoritative_source_hashes() == {
        "dataset_sha256": hashlib.sha256(
            produce.DEFAULT_DATASET.read_bytes()
        ).hexdigest(),
        "judge_prompt_sha256": hashlib.sha256(
            produce.JUDGE_PROMPT_TEMPLATE.encode()
        ).hexdigest(),
    }


def test_verify_drift_rejections_use_the_shared_hashes():
    local = release.authoritative_source_hashes()
    with pytest.raises(ProduceError, match="dataset"):
        release.verify({}, dict(local, dataset_sha256="0" * 64))
    with pytest.raises(ProduceError, match="judge prompt"):
        release.verify({}, dict(local, judge_prompt_sha256="0" * 64))

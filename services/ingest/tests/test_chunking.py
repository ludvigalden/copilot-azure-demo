"""Tests for the heading chunker."""

from ingest.chunking import chunk_article, chunk_kb

ARTICLE = """---
title: Reset your password
---

## When to use this article

Follow these steps if you have forgotten your password.

## Reset the password

Enter a new password at the reset page.
"""

EMPTY_SECTION = """---
title: Empty heading
---

## Notes

Real content follows.

## Later

More content.
"""

NO_TITLE = """## Only a heading

Body text.
"""


def test_chunks_split_on_headings_with_slugged_ids(tmp_path):
    path = tmp_path / "reset-password.md"
    path.write_text(ARTICLE, encoding="utf-8")

    chunks = chunk_article(path, "owner/repo", "abc123")

    assert [chunk.id for chunk in chunks] == ["reset-password-1", "reset-password-2"]
    assert [chunk.section for chunk in chunks] == [
        "When to use this article",
        "Reset the password",
    ]
    assert chunks[0].title == "Reset your password"
    assert (
        chunks[0].content == "Follow these steps if you have forgotten your password."
    )


def test_citation_url_points_at_the_github_blob(tmp_path):
    path = tmp_path / "reset-password.md"
    path.write_text(ARTICLE, encoding="utf-8")

    chunks = chunk_article(path, "acme/it-demo", "deadbeef")

    assert {chunk.url for chunk in chunks} == {
        "https://github.com/acme/it-demo/blob/deadbeef/kb/reset-password.md"
    }


def test_title_falls_back_to_the_file_stem(tmp_path):
    path = tmp_path / "untitled.md"
    path.write_text(NO_TITLE, encoding="utf-8")

    chunks = chunk_article(path, "owner/repo", "main")

    assert len(chunks) == 1
    assert chunks[0].title == "untitled"


def test_chunk_kb_reads_every_article_in_order(tmp_path):
    (tmp_path / "b-second.md").write_text(EMPTY_SECTION, encoding="utf-8")
    (tmp_path / "a-first.md").write_text(ARTICLE, encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not an article", encoding="utf-8")

    chunks = chunk_kb(tmp_path, "owner/repo", "main")

    assert [chunk.id for chunk in chunks] == [
        "a-first-1",
        "a-first-2",
        "b-second-1",
        "b-second-2",
    ]

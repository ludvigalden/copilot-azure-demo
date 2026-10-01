"""Heading-based chunking of the knowledge base articles.

Each article in ``kb/`` carries a ``title`` in its frontmatter and is
divided into sections by ``## `` headings. One chunk is produced per
section, with a citation URL pointing at the article on GitHub.
"""

import re
from dataclasses import dataclass
from pathlib import Path

_FRONTMATTER_TITLE = re.compile(r"^title:\s*(.+?)\s*$", re.MULTILINE)
_SECTION_HEADING = re.compile(r"^## (.+)$", re.MULTILINE)


@dataclass(frozen=True)
class Chunk:
    """One section of one article, ready to push into the index."""

    id: str
    title: str
    section: str
    content: str
    url: str


def chunk_article(path: Path, repo: str, ref: str) -> list[Chunk]:
    """Split one article into per-heading chunks.

    ``repo`` and ``ref`` name the GitHub repository and revision the
    citation URLs point at (in CD: ``GITHUB_REPOSITORY`` and
    ``GITHUB_SHA``; for a dry run: e.g. ``owner/repo`` and ``main``).
    """
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_TITLE.search(text)
    title = match.group(1) if match else path.stem
    url = f"https://github.com/{repo}/blob/{ref}/kb/{path.name}"
    slug = path.stem
    return [
        Chunk(
            id=f"{slug}-{number}",
            title=title,
            section=heading,
            content=body,
            url=url,
        )
        for number, (heading, body) in enumerate(_split_sections(text), start=1)
    ]


def chunk_kb(kb_dir: Path, repo: str, ref: str) -> list[Chunk]:
    """Chunk every Markdown article in ``kb_dir`` in file-name order."""
    return [
        chunk
        for path in sorted(kb_dir.glob("*.md"))
        for chunk in chunk_article(path, repo, ref)
    ]


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Return ``(heading, body)`` pairs for every ``## `` heading."""
    headings = list(_SECTION_HEADING.finditer(text))
    sections = []
    for index, match in enumerate(headings):
        start = match.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = text[start:end].strip()
        if body:
            sections.append((match.group(1).strip(), body))
    return sections

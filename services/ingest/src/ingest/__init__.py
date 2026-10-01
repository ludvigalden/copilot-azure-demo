"""Chunk the knowledge-base articles into citation-tagged passages."""

from .chunking import Chunk, chunk_article, chunk_kb

__all__ = ["Chunk", "chunk_article", "chunk_kb"]

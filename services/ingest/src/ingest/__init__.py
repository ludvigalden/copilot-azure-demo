"""Ingest the knowledge base into the search index."""

from .chunking import Chunk, chunk_article, chunk_kb

__all__ = ["Chunk", "chunk_article", "chunk_kb"]

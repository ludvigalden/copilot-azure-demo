"""Command-line entry point for the ingester."""

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from .chunking import chunk_kb
from .push import INDEX_NAME


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ingest",
        description=(
            "Chunk the kb articles and push them into the Azure AI Search "
            "index (embeddings included)."
        ),
    )
    parser.add_argument(
        "--kb", type=Path, default=Path("kb"), help="knowledge base directory"
    )
    parser.add_argument(
        "--repo", default="owner/repo", help="GitHub repository for citation URLs"
    )
    parser.add_argument("--ref", default="main", help="Git revision for citation URLs")
    parser.add_argument(
        "--prefix",
        default=os.environ.get("NAME_PREFIX", ""),
        help="resource name prefix; derives both endpoints "
        "(search {prefix}-srch, Azure OpenAI {prefix}-ai). "
        "Defaults to the NAME_PREFIX environment variable.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the chunks as JSON instead of pushing them",
    )
    args = parser.parse_args()

    chunks = chunk_kb(args.kb, args.repo, args.ref)

    if args.dry_run:
        json.dump(
            [asdict(chunk) for chunk in chunks],
            sys.stdout,
            indent=2,
            ensure_ascii=False,
        )
        sys.stdout.write("\n")
        return

    if not args.prefix:
        parser.error("a resource name prefix is required: --prefix or NAME_PREFIX")

    summary = push(chunks, args.prefix)
    print(
        f"pushed {summary.pushed} documents to the '{INDEX_NAME}' index, "
        f"deleted {summary.deleted}"
    )


def push(chunks, prefix: str):
    """Embed and upload the chunks, authenticated only with Entra tokens."""
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from azure.search.documents import SearchClient
    from openai import AzureOpenAI

    from .push import (
        OPENAI_API_VERSION,
        openai_endpoint,
        push_kb,
        search_endpoint,
    )

    credential = DefaultAzureCredential()
    search_client = SearchClient(
        endpoint=search_endpoint(prefix), index_name=INDEX_NAME, credential=credential
    )
    embeddings_client = AzureOpenAI(
        azure_endpoint=openai_endpoint(prefix),
        api_version=OPENAI_API_VERSION,
        azure_ad_token_provider=get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        ),
        # The embedding deployment runs at capacity 1, so an occasional
        # 429 with a retry-after window is expected; the client waits it
        # out instead of failing the whole ingestion run.
        max_retries=5,
    )
    try:
        return push_kb(chunks, search_client, embeddings_client)
    finally:
        credential.close()


if __name__ == "__main__":
    main()

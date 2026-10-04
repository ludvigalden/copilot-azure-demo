"""Command-line entry point for the ingester."""

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from .chunking import chunk_kb
from .push import EMBEDDING_DEPLOYMENT, INDEX_NAME


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
        "--search-endpoint",
        help="full search service endpoint; overrides the {prefix}-srch "
        "derivation for a run that consumes a shared service",
    )
    parser.add_argument(
        "--openai-endpoint",
        help="full Azure OpenAI endpoint; overrides the {prefix}-ai "
        "derivation for a run that consumes a shared account",
    )
    parser.add_argument(
        "--index-name",
        default=INDEX_NAME,
        help="index to push into; a run sharing the search service uses "
        "its own copy (default: %(default)s)",
    )
    parser.add_argument(
        "--embedding-deployment",
        default=EMBEDDING_DEPLOYMENT,
        help="embedding deployment to compute vectors with (default: %(default)s)",
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

    summary = push(
        chunks,
        args.prefix,
        index_name=args.index_name,
        embedding_deployment=args.embedding_deployment,
        search_endpoint_override=args.search_endpoint,
        openai_endpoint_override=args.openai_endpoint,
    )
    print(
        f"pushed {summary.pushed} documents to the '{args.index_name}' index, "
        f"deleted {summary.deleted}"
    )


def push(
    chunks,
    prefix: str,
    *,
    index_name: str = INDEX_NAME,
    embedding_deployment: str = EMBEDDING_DEPLOYMENT,
    search_endpoint_override: str | None = None,
    openai_endpoint_override: str | None = None,
):
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
        endpoint=search_endpoint_override or search_endpoint(prefix),
        index_name=index_name,
        credential=credential,
    )
    embeddings_client = AzureOpenAI(
        azure_endpoint=openai_endpoint_override or openai_endpoint(prefix),
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
        return push_kb(
            chunks,
            search_client,
            embeddings_client,
            embedding_deployment=embedding_deployment,
        )
    finally:
        credential.close()


if __name__ == "__main__":
    main()

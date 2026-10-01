"""Command-line entry point for the ingester."""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .chunking import chunk_kb


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ingest",
        description="Chunk the kb articles and push them into the search index.",
    )
    parser.add_argument(
        "--kb", type=Path, default=Path("kb"), help="knowledge base directory"
    )
    parser.add_argument(
        "--repo", default="owner/repo", help="GitHub repository for citation URLs"
    )
    parser.add_argument("--ref", default="main", help="Git revision for citation URLs")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the chunks as JSON instead of pushing them",
    )
    args = parser.parse_args()

    if not args.dry_run:
        parser.error(
            "pushing to the search index requires index configuration; use --dry-run"
        )

    chunks = chunk_kb(args.kb, args.repo, args.ref)
    json.dump(
        [asdict(chunk) for chunk in chunks], sys.stdout, indent=2, ensure_ascii=False
    )
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

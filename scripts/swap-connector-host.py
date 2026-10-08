"""Swap the connector swagger host for a literal host before packing."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

CONNECTOR_GLOB = "Connector/*_openapidefinition.json"
HOST_FIELD = re.compile(r'("host"\s*:\s*")([^"]*)(")')


def count_error(folder: Path, matches: list[Path]) -> str:
    """Explain a zero or multiple connector-definition outcome."""
    pattern = f"{folder}/{CONNECTOR_GLOB}"
    listing = "\n".join(f"  - {path}" for path in matches) or "  (no file matched)"
    if matches:
        remedy = (
            "keep exactly one connector definition in the solution source; "
            "remove or rename the stale extra files so the swap cannot "
            "rewrite the wrong one"
        )
    else:
        remedy = (
            "the solution source must carry the connector definition; check "
            "that the solution folder and the Connector/"
            "*_openapidefinition.json file exist in the checked-out tree"
        )
    return (
        f"expected exactly one connector definition matching {pattern}, "
        f"found {len(matches)}:\n{listing}\n{remedy}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--solution-folder", required=True, type=Path)
    args = parser.parse_args()
    try:
        if not args.host or "/" in args.host or any(map(str.isspace, args.host)):
            raise ValueError("host must be a bare hostname: no scheme, path, or spaces")
        matches = sorted(args.solution_folder.glob(CONNECTOR_GLOB))
        if len(matches) != 1:
            raise ValueError(count_error(args.solution_folder, matches))
        path = matches[0]
        text = path.read_text()
        doc = json.loads(text)
        if not isinstance(doc, dict) or "swagger" not in doc or "host" not in doc:
            raise ValueError(f"{path} is not a swagger document carrying a host field")
        fields = HOST_FIELD.findall(text)
        if len(fields) != 1:
            raise ValueError(
                f"{path} must carry exactly one host field, found {len(fields)}"
            )
        literal = json.dumps(args.host)[1:-1]
        swapped = HOST_FIELD.sub(
            lambda match: f"{match.group(1)}{literal}{match.group(3)}", text, count=1
        )
        path.write_text(swapped)
        print(f"host swapped in {path}: {doc['host']} -> {args.host}")
        return 0
    except (ValueError, KeyError, TypeError, OSError, AttributeError) as exc:
        print(f"host swap: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

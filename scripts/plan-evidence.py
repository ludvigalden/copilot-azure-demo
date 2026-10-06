"""Bind private Terraform plans to one candidate and approval attempt."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
from gate import FRESHNESS_TTL_SECONDS


COMPONENTS = ("app", "kb", "infra", "power_platform", "evaluation")


def canonical_components(components: list[str]) -> list[str]:
    """Reject unknown or repeated components and return them sorted."""
    unknown = [name for name in components if name not in COMPONENTS]
    if unknown:
        raise ValueError(f"unknown plan components: {', '.join(unknown)}")
    if len(set(components)) != len(components):
        raise ValueError("repeated plan components")
    return sorted(components)


def candidate_hash(source: str, run_id: str, attempt: str, components: list[str]) -> str:
    identity = {"source_commit": source, "run_id": run_id, "run_attempt": attempt,
                "components": canonical_components(components)}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate(metadata: dict, plan: Path, candidate: str, source: str, run_id: str,
             attempt: str, now: datetime) -> None:
    if metadata.get("schema_version") != 1:
        raise ValueError("invalid plan schema")
    if metadata.get("candidate_sha256") != candidate:
        raise ValueError("stale plan candidate")
    if metadata.get("source_commit") != source:
        raise ValueError("stale plan source")
    if metadata.get("run_id") != run_id:
        raise ValueError("stale plan run")
    if metadata.get("run_attempt") != attempt:
        raise ValueError("stale plan attempt; regenerate and obtain fresh approval")
    created = datetime.fromisoformat(metadata["created_at"])
    expires = datetime.fromisoformat(metadata["expires_at"])
    if created.tzinfo is None or expires.tzinfo is None:
        raise ValueError("plan timestamps require timezone")
    if not created <= now < expires or expires - created != timedelta(seconds=FRESHNESS_TTL_SECONDS):
        raise ValueError("expired or invalid plan freshness; regenerate and obtain fresh approval")
    digest = hashlib.sha256(plan.read_bytes()).hexdigest()
    if metadata.get("plan_sha256") != digest:
        raise ValueError("plan metadata/hash mismatch")
    if metadata.get("plan_id") != f"{run_id}-{attempt}-{digest}":
        raise ValueError("plan identity mismatch")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("candidate", "create", "verify"))
    parser.add_argument("--source", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--components", nargs="*", default=[])
    parser.add_argument("--candidate")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--approved", type=Path)
    args = parser.parse_args()
    try:
        if not re.fullmatch(r"[a-f0-9]{40}", args.source):
            raise ValueError("invalid plan source")
        if not args.run_id.isdecimal() or not args.attempt.isdecimal():
            raise ValueError("invalid plan run identity")
        if args.mode == "candidate":
            print(candidate_hash(args.source, args.run_id, args.attempt, args.components))
            return 0
        if not args.candidate or not re.fullmatch(r"[a-f0-9]{64}", args.candidate):
            raise ValueError("missing plan candidate")
        if args.plan is None or args.metadata is None:
            raise ValueError("missing plan or metadata")
        now = datetime.now(UTC)
        if args.mode == "create":
            digest = hashlib.sha256(args.plan.read_bytes()).hexdigest()
            metadata = {"schema_version": 1, "plan_sha256": digest,
                        "created_at": now.isoformat(),
                        "expires_at": (now + timedelta(seconds=FRESHNESS_TTL_SECONDS)).isoformat(),
                        "source_commit": args.source, "run_id": args.run_id,
                        "run_attempt": args.attempt, "plan_id": f"{args.run_id}-{args.attempt}-{digest}",
                        "candidate_sha256": args.candidate}
            args.metadata.write_text(json.dumps(metadata, sort_keys=True) + "\n")
        else:
            metadata = json.loads(args.metadata.read_text())
            if args.approved is None:
                raise ValueError("missing approved plan metadata")
            approved = json.loads(args.approved.read_text())["infrastructure_plan"]
            if metadata != approved:
                raise ValueError("plan differs from approval; regeneration requires fresh approval")
            validate(metadata, args.plan, args.candidate, args.source, args.run_id, args.attempt, now)
        print(json.dumps(metadata, sort_keys=True))
        return 0
    except (ValueError, KeyError, TypeError, OSError, AttributeError) as exc:
        print(f"plan gate: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

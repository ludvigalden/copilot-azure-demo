"""Capture and evaluate a trusted staging candidate without exposing credentials."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import gate
import produce

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "release_chunking", ROOT / "services/ingest/src/ingest/chunking.py"
)
if spec is None or spec.loader is None:
    raise ImportError("immutable KB chunker unavailable")
chunking = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = chunking
spec.loader.exec_module(chunking)
chunk_kb = chunking.chunk_kb

ANSWER_PROMPT_SOURCE = "services/api/ItSupport.Api/Answers/AzureRagAnswerProvider.cs"

DEADLINE: float | None = None


def command(*args: str) -> str:
    """Bound metadata commands and never disclose their captured output."""
    remaining = 60 if DEADLINE is None else min(60, DEADLINE - time.monotonic())
    if remaining <= 0:
        raise produce.ProduceError("candidate capture deadline reached")
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=remaining, check=False
        )
    except subprocess.TimeoutExpired as error:
        raise produce.ProduceError(f"metadata command timed out: {args[0]}") from error
    if result.returncode:
        raise produce.ProduceError(
            f"metadata command failed: {args[0]} (rc={result.returncode})"
        )
    return result.stdout.strip()


def required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise produce.ProduceError(f"missing staging binding: {name}")
    return value


def normalized_rows(rows: list[dict]) -> list[dict]:
    """Reject duplicate or malformed chunks before comparing source records."""
    if not isinstance(rows, list) or not rows:
        raise produce.ProduceError("staging index has no chunks")
    normalized = []
    for row in rows:
        if not isinstance(row, dict) or any(
            not isinstance(row.get(key), str) or not row[key]
            for key in ("id", "title", "content", "url")
        ):
            raise produce.ProduceError("malformed indexed chunk")
        normalized.append({key: row[key] for key in ("id", "title", "content", "url")})
    if len({row["id"] for row in normalized}) != len(normalized):
        raise produce.ProduceError("duplicate indexed chunk")
    return sorted(normalized, key=lambda row: row["id"])


def answer_prompt(source: str) -> str:
    """Extract exactly the system prompt the deployed source compiles."""
    match = re.search(
        r'new SystemChatMessage\(\s*"""\n(.*?)\n([ \t]*)"""', source, re.DOTALL
    )
    if match is None:
        raise produce.ProduceError("deployed answer source lacks the answer prompt")
    lines = match.group(1).split("\n")
    if any(line and not line.startswith(match.group(2)) for line in lines):
        raise produce.ProduceError(
            "deployed answer prompt indentation differs from its closing delimiter"
        )
    return "\n".join(line.removeprefix(match.group(2)) for line in lines)


def authoritative_source_hashes() -> dict[str, str]:
    """Digests of the local sources every candidate must carry."""
    return {
        "dataset_sha256": hashlib.sha256(
            produce.DEFAULT_DATASET.read_bytes()
        ).hexdigest(),
        "judge_prompt_sha256": hashlib.sha256(
            produce.JUDGE_PROMPT_TEMPLATE.encode()
        ).hexdigest(),
    }


def capture(components: list[str]) -> dict:
    """Bind deployed image provenance, app settings and the full KB source."""
    prefix, shared = required("NAME_PREFIX"), required("SHARED_PREFIX")
    source = command("git", "rev-parse", "HEAD")
    app = json.loads(
        command(
            "az",
            "containerapp",
            "show",
            "--name",
            f"{prefix}-app",
            "--resource-group",
            f"{prefix}-rg",
            "--output",
            "json",
        )
    )["properties"]
    template = app["template"]["containers"][0]
    settings = {item["name"]: item.get("value", "") for item in template.get("env", [])}
    index = os.environ.get("INDEX_NAME") or "kb"
    chat = os.environ.get("CHAT_DEPLOYMENT_NAME") or "chat"
    embedding = os.environ.get("EMBEDDING_DEPLOYMENT_NAME") or "embedding"
    search_endpoint = f"https://{shared}-srch.search.windows.net"
    openai_endpoint = f"https://{shared}-ai.cognitiveservices.azure.com"
    for name, expected, default in (
        ("Search__IndexName", index, "kb"),
        ("OpenAI__DeploymentName", chat, "chat"),
        ("Search__Endpoint", search_endpoint, ""),
        ("OpenAI__Endpoint", openai_endpoint, ""),
    ):
        if settings.get(name, default).rstrip("/") != expected:
            raise produce.ProduceError(f"deployed app binding differs: {name}")
    image = template["image"]
    if not re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", image):
        raise produce.ProduceError("staging image is not a canonical immutable digest")
    revision = app["latestReadyRevisionName"]
    if revision != app["latestRevisionName"]:
        raise produce.ProduceError("candidate revision is not ready")
    if app["configuration"].get("activeRevisionsMode", "Single") != "Single":
        raise produce.ProduceError("staging must route only the ready candidate")
    command("docker", "pull", image)
    labels = (
        json.loads(
            command(
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .Config.Labels}}",
                image,
            )
        )
        or {}
    )
    if (
        labels.get("org.opencontainers.image.source")
        != f"https://github.com/{required('GITHUB_REPOSITORY')}"
    ):
        raise produce.ProduceError("deployed image source repository differs")
    image_source = labels.get("org.opencontainers.image.revision", "")
    if not re.fullmatch(r"[0-9a-f]{40}", image_source):
        raise produce.ProduceError(
            "deployed image lacks immutable source revision label"
        )
    if "app" in components and image_source != source:
        raise produce.ProduceError("deployed app source differs from selected source")
    provider = command(
        "git",
        "show",
        f"{image_source}:services/api/ItSupport.Api/Answers/AzureRagAnswerProvider.cs",
    )
    if (
        "MaxOutputTokenCount = 256" not in provider
        or "new ChatCompletionOptions" not in provider
    ):
        raise produce.ProduceError(
            "deployed answer source lacks the enforced token cap"
        )
    rows = []
    for skip in range(0, 10000, 1000):
        url = (
            f"{search_endpoint}/indexes/{index}/docs"
            f"?api-version=2024-07-01&search=*&$select=id,title,content,url&$top=1000&$skip={skip}"
        )
        page = json.loads(
            command(
                "az",
                "rest",
                "--method",
                "GET",
                "--url",
                url,
                "--resource",
                "https://search.azure.com",
                "--output",
                "json",
            )
        )["value"]
        if not isinstance(page, list) or len(page) > 1000:
            raise produce.ProduceError("malformed index page")
        rows.extend(page)
        if len(page) < 1000:
            break
    else:
        raise produce.ProduceError("index snapshot exceeds 10000 chunks")
    rows = normalized_rows(rows)
    refs = {row["url"].split("/blob/")[-1].split("/kb/")[0] for row in rows}
    if len(refs) != 1 or not re.fullmatch(r"[0-9a-f]{40}", next(iter(refs))):
        raise produce.ProduceError(
            "index citations lack one immutable KB source revision"
        )
    kb_source = next(iter(refs))
    if "kb" in components and kb_source != source:
        raise produce.ProduceError("indexed KB source differs from selected source")
    if command("git", "rev-parse", f"{kb_source}:kb") != command(
        "git", "rev-parse", "HEAD:kb"
    ):
        raise produce.ProduceError("indexed KB tree differs from checked-out KB")
    expected_rows = [
        {key: getattr(chunk, key) for key in ("id", "title", "content", "url")}
        for chunk in chunk_kb(ROOT / "kb", required("GITHUB_REPOSITORY"), kb_source)
    ]
    if rows != normalized_rows(expected_rows):
        raise produce.ProduceError("indexed chunks differ from immutable KB source")
    definition = json.loads(
        command(
            "az",
            "rest",
            "--method",
            "GET",
            "--url",
            f"{search_endpoint}/indexes/{index}?api-version=2024-07-01",
            "--resource",
            "https://search.azure.com",
            "--output",
            "json",
        )
    )
    vectorizers = definition["vectorSearch"]["vectorizers"]
    if not vectorizers or any(
        item.get("azureOpenAIParameters", {}).get("deploymentId") != embedding
        or item.get("azureOpenAIParameters", {}).get("resourceUri", "").rstrip("/")
        != openai_endpoint
        for item in vectorizers
    ):
        raise produce.ProduceError("index vectorizer binding differs")
    model = json.loads(
        command(
            "az",
            "cognitiveservices",
            "account",
            "deployment",
            "show",
            "--name",
            f"{shared}-ai",
            "--resource-group",
            f"{shared}-rg",
            "--deployment-name",
            chat,
            "--output",
            "json",
        )
    )["properties"]["model"]
    embedding_model = json.loads(
        command(
            "az",
            "cognitiveservices",
            "account",
            "deployment",
            "show",
            "--name",
            f"{shared}-ai",
            "--resource-group",
            f"{shared}-rg",
            "--deployment-name",
            embedding,
            "--output",
            "json",
        )
    )["properties"]["model"]
    return {
        "staging_prefix": prefix,
        "shared_prefix": shared,
        "index_definition_sha256": hashlib.sha256(
            json.dumps(definition, sort_keys=True).encode()
        ).hexdigest(),
        "document_count": len(rows),
        "embedding_model": embedding_model,
        **authoritative_source_hashes(),
        "source_commit": source,
        "kb_tree": command("git", "rev-parse", "HEAD:kb"),
        "kb_source_commit": kb_source,
        "image_source_commit": image_source,
        "image": image,
        "revision": revision,
        "index_name": index,
        "index_snapshot_sha256": hashlib.sha256(
            json.dumps(rows, sort_keys=True).encode()
        ).hexdigest(),
        "chat_deployment": chat,
        "embedding_deployment": embedding,
        "chat_model": model,
        "answer_prompt_sha256": hashlib.sha256(
            answer_prompt(provider).encode()
        ).hexdigest(),
        "judge_host": f"{shared}-ai.cognitiveservices.azure.com",
        "judge_api_version": produce.DEFAULT_JUDGE_API_VERSION,
        "run_id": required("EVALUATION_RUN_ID"),
        "endpoint": f"https://{app['configuration']['ingress']['fqdn']}/api/answers",
        "components": components,
    }


def verify(document: dict, candidate: dict) -> None:
    local = authoritative_source_hashes()
    if (
        not isinstance(candidate, dict)
        or candidate.get("dataset_sha256") != local["dataset_sha256"]
    ):
        raise produce.ProduceError(
            "candidate dataset differs from authoritative source"
        )
    if candidate.get("judge_prompt_sha256") != local["judge_prompt_sha256"]:
        raise produce.ProduceError(
            "candidate judge prompt differs from authoritative source"
        )
    verdict = gate.gate_release_document(document, candidate)
    if not verdict.passed:
        raise produce.ProduceError(
            "release evaluation rejected: " + "; ".join(verdict.failures)
        )


def main(argv: list[str] | None = None) -> int:
    global DEADLINE
    parser = argparse.ArgumentParser(
        description="Capture, evaluate and recheck staging"
    )
    parser.add_argument(
        "--components", nargs="*", choices=("app", "kb", "evaluation"), default=[]
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    DEADLINE = time.monotonic() + 900
    try:
        if args.verify_only:
            candidate = json.loads(args.candidate.read_text())
            if not args.components:
                args.components = (
                    candidate.get("components") if isinstance(candidate, dict) else None
                )
            if not isinstance(args.components, list) or not args.components:
                raise produce.ProduceError("candidate selection missing")
            if (
                candidate["run_id"] != required("EVALUATION_RUN_ID")
                or candidate["components"] != args.components
            ):
                raise produce.ProduceError(
                    "candidate is from a different workflow attempt or selection"
                )
            if candidate["source_commit"] != command("git", "rev-parse", "HEAD"):
                raise produce.ProduceError(
                    "candidate source differs from current release"
                )
            expected_digest = os.environ.get("EXPECTED_APP_DIGEST", "")
            if expected_digest and not candidate["image"].endswith(
                "@" + expected_digest
            ):
                raise produce.ProduceError(
                    "approved image differs from selected digest"
                )
            verify(json.loads(args.output.read_text()), candidate)
            for key, env_name in (
                ("staging_prefix", "NAME_PREFIX"),
                ("shared_prefix", "SHARED_PREFIX"),
                ("index_name", "INDEX_NAME"),
                ("chat_deployment", "CHAT_DEPLOYMENT_NAME"),
                ("embedding_deployment", "EMBEDDING_DEPLOYMENT_NAME"),
            ):
                value = candidate.get(key)
                if not isinstance(value, str) or not value:
                    raise produce.ProduceError("candidate staging binding missing")
                os.environ[env_name] = value
            if capture(args.components) != candidate:
                raise produce.ProduceError(
                    "approved staging candidate changed before promotion"
                )
        else:
            required("SHARED_AI_ACCOUNT_KEY")
            if not args.components:
                args.components = ["evaluation"]
            before = capture(args.components)
            expected_digest = os.environ.get("EXPECTED_APP_DIGEST", "")
            if "app" in args.components and not expected_digest:
                raise produce.ProduceError(
                    "missing staging binding: EXPECTED_APP_DIGEST"
                )
            if expected_digest and not before["image"].endswith("@" + expected_digest):
                raise produce.ProduceError(
                    "staging image differs from selected build digest"
                )
            produce.write_document(before, args.candidate)
            config = produce.RunConfig(
                endpoint=before["endpoint"],
                answer_revision=before["revision"],
                answer_image=before["image"],
                judge_host=before["judge_host"],
                judge_deployment=before["chat_deployment"],
                judge_api_version=before["judge_api_version"],
                dataset_path=produce.DEFAULT_DATASET,
                key_env=produce.DEFAULT_KEY_ENV,
                output_path=args.output,
                candidate=before,
                run_id=before["run_id"],
                max_duration_seconds=max(1, int(DEADLINE - time.monotonic() - 120)),
            )
            document = produce.run(config, produce.default_transport, time.sleep)
            produce.write_document(document, args.output)
            if capture(args.components) != before:
                raise produce.ProduceError(
                    "staging candidate changed during evaluation"
                )
            verify(document, before)
        print("release evaluation: all 15 questions passed retrieval and quality gates")
        return 0
    except produce.ProduceError as error:
        print(f"release evaluation: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print(
            "release evaluation: malformed or unavailable candidate evidence",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

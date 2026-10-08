"""One identity-bound freshness policy for release promotion."""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import approval_proof
import dependency_baseline as baseline
import freshness
import produce
import release

POLICY = "conditional_same_candidate_v1"
COMPONENTS = {"app", "kb", "infra", "power_platform", "evaluation"}


def route(claim: dict[str, Any] | None, selected: list[str]) -> str:
    """No alternate policy; missing or unsupported input rejects before staging."""
    if (
        not isinstance(selected, list)
        or any(part not in COMPONENTS for part in selected)
        or len(set(selected)) != len(selected)
    ):
        raise baseline.BaselineError("COMPONENT_SELECTION")
    if claim is None:
        raise baseline.BaselineError("RELEASE_POLICY_REQUIRED")
    if not isinstance(claim, dict):
        raise baseline.BaselineError("CONDITIONAL_POLICY")
    if any(
        key
        not in {
            "schema",
            "freshness_policy",
            "authorize_refresh",
            "power_platform_mode",
        }
        for key in claim
    ):
        raise baseline.BaselineError("CONDITIONAL_POLICY")
    if (
        claim.get("schema") != "ReleaseEnvelopeV2"
        or claim.get("freshness_policy") != POLICY
        or claim.get("authorize_refresh") is not True
    ):
        raise baseline.BaselineError("CONDITIONAL_POLICY")
    if "app" not in selected or {"infra", "kb"} & set(selected):
        raise baseline.BaselineError("CONDITIONAL_COMPONENTS")
    if "power_platform" in selected:
        raise baseline.BaselineError("CONDITIONAL_POWER_PLATFORM")
    return "conditional"


def validate_originals(
    envelope_raw: bytes,
    candidate_raw: bytes,
    evaluation_raw: bytes,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    envelope = baseline.object_json(envelope_raw)
    baseline.exact(
        envelope,
        {
            "schema",
            "freshness_policy",
            "authorize_refresh",
            "power_platform_mode",
            "selected_components",
            "source_commit",
            "image",
            "component_status",
            "staging_verification",
            "candidate_sha256",
            "evaluation_sha256",
            "baseline_sha256",
            "baseline_provenance",
            "construction_sha256",
            "repository",
            "run_id",
            "run_attempt",
        },
    )
    baseline.sha(envelope["construction_sha256"])
    if not isinstance(envelope["repository"], str) or not re.fullmatch(
        r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+", envelope["repository"]
    ):
        raise baseline.BaselineError("REPOSITORY_BINDING")
    if envelope["component_status"] != {"power_platform": "skipped_not_selected"}:
        raise baseline.BaselineError("ACTIVE_POWER_PLATFORM")
    if envelope["staging_verification"] != {
        "READINESS": "true",
        "SMOKE": "pass",
        "BOT": "pass",
        "ASSETS": "pass",
        "THEME": "pass",
    }:
        raise baseline.BaselineError("STAGING_VERIFICATION")
    candidate = baseline.object_json(candidate_raw)
    baseline.validate_candidate(candidate)
    evaluation = baseline.object_json(evaluation_raw)
    policy_claim = {
        key: envelope[key]
        for key in (
            "schema",
            "freshness_policy",
            "authorize_refresh",
            "power_platform_mode",
        )
        if key in envelope
    }
    if route(policy_claim, envelope["selected_components"]) != "conditional":
        raise baseline.BaselineError("CONDITIONAL_POLICY")
    if envelope.get("candidate_sha256") != baseline.digest(
        candidate_raw
    ) or envelope.get("evaluation_sha256") != baseline.digest(evaluation_raw):
        raise baseline.BaselineError("ORIGINAL_HASH")
    if envelope.get("source_commit") != candidate.get("source_commit") or envelope.get(
        "image"
    ) != candidate.get("image"):
        raise baseline.BaselineError("ORIGINAL_CANDIDATE")
    if (
        candidate.get("run_id")
        != f"{envelope.get('run_id')}:{envelope.get('run_attempt')}"
    ):
        raise baseline.BaselineError("ORIGINAL_RUN")
    provider = (release.ROOT / release.ANSWER_PROMPT_SOURCE).read_text()
    if candidate.get("image_source_commit") != candidate.get(
        "source_commit"
    ) or candidate.get("answer_prompt_sha256") != baseline.digest(
        release.answer_prompt(provider).encode()
    ):
        raise baseline.BaselineError("ANSWER_SOURCE_DRIFT")
    expected_components = ["app"]
    if sorted(candidate.get("components", [])) != expected_components:
        raise baseline.BaselineError("ORIGINAL_COMPONENTS")
    for key, value in release.authoritative_source_hashes().items():
        if candidate.get(key) != value:
            raise baseline.BaselineError("SOURCE_DRIFT")
    try:
        freshness.validate_non_age(evaluation, candidate)
    except (KeyError, ValueError, TypeError, AttributeError):
        raise baseline.BaselineError("NON_AGE_EVIDENCE") from None
    return envelope, candidate, evaluation


def bind_candidate(document: dict[str, Any], candidate: dict[str, Any]) -> None:
    baseline.validate_baseline(document)
    if document.get("candidate") != candidate:
        raise baseline.BaselineError("BASELINE_CANDIDATE_IDENTITY")
    staging = document["planes"]["staging"]
    if staging["documents"].get("index_snapshot_sha256") != candidate.get(
        "index_snapshot_sha256"
    ):
        raise baseline.BaselineError("BASELINE_CONTENT")
    if (
        staging["target"]["chat_deployment"] != candidate["chat_deployment"]
        or staging["target"]["embedding_deployment"]
        != candidate["embedding_deployment"]
        or staging["ai_endpoint"].removeprefix("https://").rstrip("/")
        != candidate["judge_host"]
    ):
        raise baseline.BaselineError("BASELINE_JUDGE")
    if (
        staging["app"]["image"] != candidate["image"]
        or staging["app"]["revision"] != candidate["revision"]
        or staging["documents"]["count"] != candidate["document_count"]
        or staging["target"]["index_name"] != candidate["index_name"]
    ):
        raise baseline.BaselineError("BASELINE_CANDIDATE")
    if (
        "https://" + staging["app"]["ingress"]["fqdn"] + "/api/answers"
        != candidate["endpoint"]
    ):
        raise baseline.BaselineError("BASELINE_ENDPOINT")
    for role in ("chat", "embedding"):
        if staging["models"][role]["properties"]["model"] != candidate[f"{role}_model"]:
            raise baseline.BaselineError("BASELINE_MODEL")


def retrieve_key(document: dict[str, Any], deadline: baseline.Deadline) -> str:
    """Existing shared account-key fallback, protected job only, process local."""
    resource = document["planes"]["staging"]["target"]["ai_resource_id"]
    if resource != document["planes"]["production"]["target"]["ai_resource_id"]:
        raise baseline.BaselineError("SHARED_KEY_BINDING")
    try:
        result = subprocess.run(
            [
                "az",
                "rest",
                "--method",
                "post",
                "--url",
                f"https://management.azure.com{resource}/listKeys?api-version=2023-05-01",
                "--query",
                "key1",
                "-o",
                "tsv",
            ],
            capture_output=True,
            check=False,
            timeout=deadline.remaining(),
        )
        if result.returncode or len(result.stdout) > 16384:
            raise ValueError()
        return baseline.text(result.stdout.decode().strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        raise baseline.BaselineError("KEY_DENIED") from None


def evaluate(
    candidate: dict[str, Any],
    key: str,
    duration: int,
    deadline: baseline.Deadline,
    output: Path,
) -> bytes:
    duration = min(duration, 2280, int(deadline.end - deadline.clock() - 120))
    if duration < 1:
        raise baseline.BaselineError("POSTCAPTURE_RESERVE")
    started = deadline.clock()
    producer_end = min(deadline.end - 120, started + duration)
    producer_deadline = baseline.Deadline(producer_end, deadline.clock)
    config = produce.RunConfig(
        endpoint=candidate["endpoint"],
        answer_revision=candidate["revision"],
        answer_image=candidate["image"],
        judge_host=candidate["judge_host"],
        judge_deployment=candidate["chat_deployment"],
        judge_api_version=candidate["judge_api_version"],
        dataset_path=produce.DEFAULT_DATASET,
        key_env=produce.DEFAULT_KEY_ENV,
        output_path=output,
        candidate=candidate,
        run_id=candidate["run_id"],
        max_duration_seconds=duration,
    )

    def transport(
        method: str, url: str, headers: dict[str, str], body: bytes, timeout: float
    ) -> produce.HttpResponse:
        request = urllib.request.Request(url, headers=headers, data=body, method=method)
        try:
            with urllib.request.build_opener(baseline.SafeRedirect()).open(
                request, timeout=producer_deadline.remaining(timeout)
            ) as response:
                return produce.HttpResponse(
                    response.status,
                    baseline.bounded_read(
                        response, baseline.PAGE_BYTES, producer_deadline
                    ),
                )
        except urllib.error.HTTPError as error:
            producer_deadline.remaining()
            return produce.HttpResponse(error.code, b"")
        except (OSError, ValueError, urllib.error.URLError):
            raise produce.ProduceError("conditional endpoint unavailable") from None

    def bounded_sleep(seconds: float) -> None:
        if producer_deadline.remaining(seconds) < seconds:
            raise produce.BudgetExceeded("conditional deadline")
        time.sleep(seconds)
        producer_deadline.remaining()

    previous = os.environ.get(produce.DEFAULT_KEY_ENV)
    try:
        os.environ[produce.DEFAULT_KEY_ENV] = key
        with contextlib.redirect_stdout(io.StringIO()):
            result = produce.run(config, transport, bounded_sleep)
        producer_deadline.remaining()
        return baseline.canonical(result)
    except (produce.ProduceError, OSError, ValueError, TypeError, KeyError):
        raise baseline.BaselineError("REFRESH_FAILED") from None
    finally:
        if previous is None:
            os.environ.pop(produce.DEFAULT_KEY_ENV, None)
        else:
            os.environ[produce.DEFAULT_KEY_ENV] = previous


def promote(
    originals: tuple[bytes, bytes, bytes],
    imported: bytes,
    provenance: dict[str, Any],
    capture: Callable[[dict[str, Any]], dict[str, Any]],
    producer: Callable[[dict[str, Any], int], bytes],
    mutate: Callable[[str], None],
    deadline: baseline.Deadline,
    invocation: dict[str, Any],
    persist: Callable[[dict[str, Any], bytes], None],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    reviewed_originals: tuple[str, str, str] | None = None,
) -> dict[str, Any]:
    started_at = now().isoformat()
    if reviewed_originals is None or reviewed_originals != tuple(
        baseline.digest(raw) for raw in originals
    ):
        raise baseline.BaselineError("REVIEWED_ORIGINAL_ASSOCIATION_REQUIRED")
    if (
        invocation.get("attempt") != 1
        or invocation.get("job") != "conditional-production"
    ):
        raise baseline.BaselineError("REFRESH_RERUN_REFUSED")
    envelope, candidate, evaluation = validate_originals(*originals)
    if envelope.get("run_id") != invocation.get("run_id") or envelope.get(
        "run_attempt"
    ) != invocation.get("attempt"):
        raise baseline.BaselineError("INVOCATION_BINDING")
    if (
        envelope.get("baseline_sha256") != baseline.digest(imported)
        or envelope.get("baseline_provenance") != provenance
    ):
        raise baseline.BaselineError("BASELINE_ORIGIN_CHANGED")
    document = baseline.object_json(imported)
    bind_candidate(document, candidate)
    comparison_before = baseline.compare_planes(document, capture)
    deadline.remaining()
    classification = freshness.classify(evaluation, candidate, now())
    if classification.state not in (
        freshness.State.CURRENT,
        freshness.State.EXPIRED_VALID,
    ):
        raise baseline.BaselineError("EVALUATION_REJECTED")
    result_raw = originals[2]
    refreshed = classification.state == freshness.State.EXPIRED_VALID
    if refreshed:
        duration = min(2280, max(1, int(deadline.end - deadline.clock() - 120)))
        if deadline.end - deadline.clock() <= 120:
            raise baseline.BaselineError("POSTCAPTURE_RESERVE")
        result_raw = producer(candidate, duration)
    result = baseline.object_json(result_raw)
    if freshness.classify(result, candidate, now()).state != freshness.State.CURRENT:
        raise baseline.BaselineError("REFRESH_EVIDENCE_REJECTED")
    comparison_after = baseline.compare_planes(document, capture)
    supplement = {
        "schema": "ConditionalPromotionSupplementV1",
        "policy": POLICY,
        "original_envelope_sha256": baseline.digest(originals[0]),
        "original_candidate_sha256": baseline.digest(originals[1]),
        "original_evaluation_sha256": baseline.digest(originals[2]),
        "construction_sha256": provenance["construction_sha256"],
        "invocation": invocation,
        "invocation_sha256": baseline.digest(baseline.canonical(invocation)),
        "started_at": started_at,
        "completed_at": now().isoformat(),
        "refresh_count": int(refreshed),
        "result_sha256": baseline.digest(result_raw),
        "comparison_before": comparison_before,
        "comparison_after": comparison_after,
    }
    comparison_immediate = baseline.compare_planes(document, capture)
    supplement["comparison_before_persistence"] = comparison_immediate
    supplement["deployment_outcome"] = "not_attempted"
    supplement["final_write_boundary_comparison"] = "pending"
    deadline.remaining()
    if freshness.classify(result, candidate, now()).state != freshness.State.CURRENT:
        raise baseline.BaselineError("EVIDENCE_EXPIRED_BEFORE_WRITE")
    persist(supplement, result_raw)
    baseline.compare_planes(document, capture)
    deadline.remaining()
    if freshness.classify(result, candidate, now()).state != freshness.State.CURRENT:
        raise baseline.BaselineError("EVIDENCE_EXPIRED_BEFORE_WRITE")
    mutate(candidate["image"])
    return supplement


ARTIFACT_MEMBERS = {
    "release-identity.json",
    "evidence/candidate.json",
    "evidence/evaluation.json",
    "dependency-baseline.json",
}


def prepare(
    originals: tuple[bytes, bytes, bytes],
    claim: dict[str, Any] | None,
    selected: list[str],
    capture: Callable[[dict[str, Any]], dict[str, Any]],
    targets: dict[str, Any],
    api: baseline.VerificationAPI,
    deadline: baseline.Deadline,
    repository: str,
    run_id: str,
    attempt: int,
) -> dict[str, bytes]:
    """One preapproval producer owns the envelope and complete two-plane capture."""
    route(claim, selected)
    if attempt != 1:
        raise baseline.BaselineError("REFRESH_RERUN_REFUSED")
    envelope = baseline.object_json(originals[0])
    candidate = baseline.object_json(originals[1])
    baseline.exact(targets, {"staging", "production"})
    baseline.exact(
        envelope, {"source_commit", "image", "component_status", "staging_verification"}
    )
    if (
        envelope["source_commit"] != candidate["source_commit"]
        or envelope["image"] != candidate["image"]
    ):
        raise baseline.BaselineError("ORIGINAL_CANDIDATE")
    for target in targets.values():
        baseline.validate_target(target)
    if (
        targets["staging"]["app_resource_id"]
        == targets["production"]["app_resource_id"]
    ):
        raise baseline.BaselineError("TARGET_PLANES")
    document = {
        "schema": "DependencyBaselineV1",
        "candidate": candidate,
        "captured_at": datetime.now(UTC).isoformat(),
        "planes": {name: capture(targets[name]) for name in ("staging", "production")},
    }
    bind_candidate(document, candidate)
    imported = baseline.canonical(document)
    construction = approval_proof.source_proof(api, candidate["source_commit"])
    provenance = {
        "construction_sha256": construction,
        "capture_job": approval_proof.OWNER_JOB,
    }
    if claim is None:
        raise baseline.BaselineError("RELEASE_POLICY_REQUIRED")
    envelope.update(claim)
    envelope.update(
        {
            "selected_components": selected,
            "candidate_sha256": baseline.digest(originals[1]),
            "evaluation_sha256": baseline.digest(originals[2]),
            "baseline_sha256": baseline.digest(imported),
            "baseline_provenance": provenance,
            "construction_sha256": construction,
            "repository": repository,
            "run_id": run_id,
            "run_attempt": attempt,
        }
    )
    prepared = baseline.canonical(envelope) + b"\n"
    validate_originals(prepared, originals[1], originals[2])
    deadline.remaining()
    return {
        "release-identity.json": prepared,
        "evidence/candidate.json": originals[1],
        "evidence/evaluation.json": originals[2],
        "dependency-baseline.json": imported,
    }


def authenticate_reviewed_originals(
    api: baseline.VerificationAPI,
    originals: tuple[bytes, bytes, bytes],
    invocation: dict[str, Any],
    deadline: baseline.Deadline,
) -> tuple[str, str, str]:
    """Prospective workflow construction plus returned user review, not config consent."""
    envelope = baseline.object_json(originals[0])
    approval_proof.authenticate(api, envelope, invocation, deadline)
    if envelope["candidate_sha256"] != baseline.digest(originals[1]) or envelope[
        "evaluation_sha256"
    ] != baseline.digest(originals[2]):
        raise baseline.BaselineError("ORIGINAL_HASH")
    return (
        baseline.digest(originals[0]),
        envelope["candidate_sha256"],
        envelope["evaluation_sha256"],
    )


def consume(
    originals: tuple[bytes, bytes, bytes],
    local_baseline: bytes,
    api: baseline.VerificationAPI,
    artifact_id: int,
    archive_sha256: str,
    invocation: dict[str, Any],
    deadline: baseline.Deadline,
) -> tuple[bytes, dict[str, Any], tuple[str, str, str]]:
    """Exact same-run members plus source construction precede returned review proof."""
    envelope, candidate, _ = validate_originals(*originals)
    if (
        invocation.get("attempt") != 1
        or invocation.get("job") != approval_proof.PRODUCTION_JOB
    ):
        raise baseline.BaselineError("REFRESH_RERUN_REFUSED")
    if (
        envelope["run_id"] != invocation["run_id"]
        or envelope["run_attempt"] != invocation["attempt"]
    ):
        raise baseline.BaselineError("INVOCATION_BINDING")
    if "baseline_origin" in envelope or "approved_originals_sha256" in envelope:
        raise baseline.BaselineError("UNSUPPORTED_APPROVAL_ORIGIN")
    if envelope["baseline_sha256"] != baseline.digest(local_baseline):
        raise baseline.BaselineError("BASELINE_ORIGIN_CHANGED")
    bind_candidate(baseline.object_json(local_baseline), candidate)
    files = baseline.artifact_files(
        api,
        artifact_id,
        approval_proof.ARTIFACT,
        int(invocation["run_id"]),
        envelope["source_commit"],
        archive_sha256,
        ARTIFACT_MEMBERS,
        deadline,
    )
    expected: dict[str, bytes] = dict(
        zip(
            (
                "release-identity.json",
                "evidence/candidate.json",
                "evidence/evaluation.json",
            ),
            originals,
        )
    )
    expected["dependency-baseline.json"] = local_baseline
    if files != expected:
        raise baseline.BaselineError("SAME_RUN_BYTES_CHANGED")
    proof = approval_proof.authenticate(api, envelope, invocation, deadline)
    metadata = api.get(f"actions/artifacts/{artifact_id}")
    jobs = api.get(f"actions/runs/{invocation['run_id']}/attempts/1/jobs?per_page=100")[
        "jobs"
    ]
    owner = next(job for job in jobs if job["id"] == proof["capture_job_id"])
    created = baseline.origin_time(metadata["created_at"])
    if (
        not baseline.origin_time(owner["started_at"])
        <= created
        <= baseline.origin_time(owner["completed_at"])
    ):
        raise baseline.BaselineError("ARTIFACT_CAPTURE_TIME")
    if envelope["baseline_provenance"] != {
        "construction_sha256": proof["construction_sha256"],
        "capture_job": approval_proof.OWNER_JOB,
    }:
        raise baseline.BaselineError("BASELINE_ORIGIN_CHANGED")
    reviewed = (
        baseline.digest(originals[0]),
        envelope["candidate_sha256"],
        envelope["evaluation_sha256"],
    )
    return local_baseline, envelope["baseline_provenance"], reviewed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "consume", "promote", "route"))
    parser.add_argument("--envelope", type=Path, default=Path("release-identity.json"))
    parser.add_argument(
        "--candidate", type=Path, default=Path("evidence/candidate.json")
    )
    parser.add_argument(
        "--evaluation", type=Path, default=Path("evidence/evaluation.json")
    )
    parser.add_argument("--claim", default="")
    parser.add_argument("--components", nargs="*", default=[])
    parser.add_argument("--targets", type=Path)
    parser.add_argument(
        "--baseline", type=Path, default=Path("dependency-baseline.json")
    )
    parser.add_argument("--artifact-id", type=int)
    parser.add_argument("--artifact-digest", default="")
    parser.add_argument("--output", type=Path, default=Path("conditional-evidence"))
    args = parser.parse_args(argv)
    deadline = baseline.Deadline(time.monotonic() + 2400)
    try:
        claim = baseline.object_json(args.claim.encode()) if args.claim else None
        if args.mode == "route":
            print(route(claim, args.components))
            return 0
        if (
            os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_EVENT_NAME") == "pull_request"
            or os.environ.get("ACT")
        ):
            raise baseline.BaselineError("TRUSTED_MAIN_ONLY")
        repository = baseline.text(os.environ.get("GITHUB_REPOSITORY"))
        source = baseline.sha(os.environ.get("GITHUB_SHA"), 40)
        api = baseline.GitHubAPI(
            repository, baseline.text(os.environ.get("GH_TOKEN")), deadline
        )
        envelope_raw = args.envelope.read_bytes()
        envelope = baseline.object_json(envelope_raw)
        candidate_raw, evaluation_raw = (
            args.candidate.read_bytes(),
            args.evaluation.read_bytes(),
        )
        originals = (envelope_raw, candidate_raw, evaluation_raw)
        if (
            baseline.object_json(candidate_raw).get("source_commit") != source
            or envelope.get("source_commit") != source
        ):
            raise baseline.BaselineError("CHECKOUT_SOURCE_BINDING")
        if args.mode == "prepare":
            if args.targets is None:
                raise baseline.BaselineError("PREPARE_TARGETS_REQUIRED")
            files = prepare(
                originals,
                claim,
                args.components,
                baseline.AzureCapture(deadline).capture,
                baseline.object_json(args.targets.read_bytes()),
                api,
                deadline,
                repository,
                os.environ["GITHUB_RUN_ID"],
                int(os.environ["GITHUB_RUN_ATTEMPT"]),
            )
            if args.output.exists():
                raise baseline.BaselineError("ARTIFACT_ALREADY_PREPARED")
            for name, raw in files.items():
                destination = args.output / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(raw)
            return 0
        invocation = {
            "run_id": os.environ["GITHUB_RUN_ID"],
            "attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
            "job": os.environ["GITHUB_JOB"],
        }
        imported, provenance, reviewed = consume(
            originals,
            args.baseline.read_bytes(),
            api,
            args.artifact_id,
            args.artifact_digest.removeprefix("sha256:"),
            invocation,
            deadline,
        )
        if args.mode == "consume":
            return 0
        document = baseline.object_json(imported)
        if os.environ.get("PROMOTION_ENVIRONMENT") != "demo":
            raise baseline.BaselineError("PROTECTED_DEMO_ONLY")
        if args.output.exists():
            raise baseline.BaselineError("REFRESH_ALREADY_ATTEMPTED")
        args.output.mkdir()
        originals = (envelope_raw, candidate_raw, evaluation_raw)

        def producer(bound: dict[str, Any], duration: int) -> bytes:
            key = retrieve_key(document, deadline)
            return evaluate(
                bound, key, duration, deadline, args.output / "partial.json"
            )

        def persist(supplement: dict[str, Any], result: bytes) -> None:
            if originals != (
                args.envelope.read_bytes(),
                args.candidate.read_bytes(),
                args.evaluation.read_bytes(),
            ):
                raise baseline.BaselineError("ORIGINAL_BYTES_CHANGED")
            (args.output / "result.json").write_bytes(result)
            (args.output / "supplement.json").write_bytes(
                baseline.canonical(supplement)
            )

        def mutate(image: str) -> None:
            result = subprocess.run(
                [
                    "az",
                    "containerapp",
                    "update",
                    "--ids",
                    document["planes"]["production"]["target"]["app_resource_id"],
                    "--image",
                    image,
                    "--output",
                    "none",
                ],
                capture_output=True,
                check=False,
                timeout=deadline.remaining(),
            )
            if result.returncode:
                raise baseline.BaselineError("APP_UPDATE_FAILED")

        promote(
            originals,
            imported,
            provenance,
            baseline.AzureCapture(deadline).capture,
            producer,
            mutate,
            deadline,
            invocation,
            persist,
            reviewed_originals=reviewed,
        )
        return 0
    except baseline.BaselineError as error:
        print(
            "release promotion: rejected "
            + str(error)
            + "; prepare a new observable same-run bundle",
            file=sys.stderr,
        )
        return 1
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        urllib.error.URLError,
    ):
        print(
            "conditional promotion: rejected; no authorization inferred",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Prospective same-run approval proof, never a historical-origin inference."""

from __future__ import annotations

import base64
import re
from collections.abc import Hashable
from pathlib import Path
from typing import Any

import yaml

import dependency_baseline as b

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ".github/workflows/release.yml"
BODY = ".github/workflows/release-candidate.yml"
OWNER_JOB = "approval-capture"
PRODUCTION_JOB = "conditional-production"
ARTIFACT = "release-approval-bundle"


class WorkflowLoader(yaml.SafeLoader):
    """Reject duplicate mapping keys instead of losing owner evidence."""

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Hashable, Any]:
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if len(set(keys)) != len(keys):
            raise b.BaselineError("WORKFLOW_CONSTRUCTION")
        return super().construct_mapping(node, deep=deep)


def _semantic_document(raw: bytes, path: str) -> dict[str, Any]:
    """Semantic workflow model: reject any shape the proof cannot attribute."""
    try:
        document = yaml.load(raw, Loader=WorkflowLoader)
    except yaml.YAMLError:
        raise b.BaselineError("WORKFLOW_SOURCE") from None
    jobs = document.get("jobs") if isinstance(document, dict) else None
    if not isinstance(jobs, dict) or not jobs:
        raise b.BaselineError("WORKFLOW_CONSTRUCTION")
    for name, job in jobs.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", name):
            raise b.BaselineError("WORKFLOW_CONSTRUCTION")
        if not isinstance(job, dict) or "uses" not in job and "steps" not in job:
            raise b.BaselineError("WORKFLOW_CONSTRUCTION")
    return document


def _semantic_uses(document: dict[str, Any]) -> list[str]:
    local = []
    for job in document["jobs"].values():
        entries = [job, *(job.get("steps") or [])]
        for step in entries:
            uses = step.get("uses") if isinstance(step, dict) else None
            if not isinstance(uses, str):
                continue
            match = re.fullmatch(r"\./(\.github/workflows/[a-z-]+\.yml)", uses)
            if match:
                local.append(match[1])
            elif uses.startswith("./"):
                raise b.BaselineError("WORKFLOW_CLOSURE")
    return local


def _semantic_uploads(document: dict[str, Any], path: str) -> list[tuple[str, str]]:
    uploads = []
    for job, definition in document["jobs"].items():
        for step in definition.get("steps") or []:
            if not isinstance(step, dict):
                raise b.BaselineError("WORKFLOW_CONSTRUCTION")
            uses = step.get("uses")
            if not isinstance(uses, str) or not uses.startswith(
                "actions/upload-artifact@"
            ):
                continue
            with_block = step.get("with")
            if not isinstance(with_block, dict):
                raise b.BaselineError("AMBIGUOUS_UPLOAD_OWNER")
            name = with_block.get("name")
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"[a-z][a-z0-9-]*", name)
                or "overwrite" in with_block
                or "${{" in name
            ):
                raise b.BaselineError("AMBIGUOUS_UPLOAD_OWNER")
            if name == ARTIFACT:
                uploads.append((path, job))
    return uploads


def _semantic_environment(jobs: dict[str, Any]) -> list[str]:
    names = []
    for definition in jobs.values():
        value = definition.get("environment")
        if value is None:
            continue
        if isinstance(value, str):
            names.append(value)
        elif isinstance(value, dict):
            if not isinstance(value.get("name"), str):
                raise b.BaselineError("AMBIGUOUS_REVIEW_ENVIRONMENT")
            names.append(value["name"])
        else:
            raise b.BaselineError("AMBIGUOUS_REVIEW_ENVIRONMENT")
    return names


def source_proof(api: b.VerificationAPI, source: str) -> str:
    """Authenticate the audited workflow closure against immutable source bytes."""
    pending = [ENTRY]
    seen: dict[str, str] = {}
    uploads = []
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        if len(seen) >= 20 or not re.fullmatch(
            r"\.github/workflows/[a-z-]+\.yml", path
        ):
            raise b.BaselineError("WORKFLOW_CLOSURE")
        response = api.get(f"contents/{path}?ref={source}")
        if response.get("encoding") != "base64":
            raise b.BaselineError("WORKFLOW_SOURCE")
        raw = base64.b64decode(response["content"], validate=False)
        if raw != (ROOT / path).read_bytes():
            raise b.BaselineError("WORKFLOW_SOURCE")
        seen[path] = b.digest(raw)
        document = _semantic_document(raw, path)
        pending.extend(_semantic_uses(document))
        uploads.extend(_semantic_uploads(document, path))
    if uploads != [(BODY, OWNER_JOB)]:
        raise b.BaselineError("AMBIGUOUS_UPLOAD_OWNER")
    body_jobs = _semantic_document((ROOT / BODY).read_bytes(), BODY)["jobs"]
    if (
        list(body_jobs).count(OWNER_JOB) != 1
        or list(body_jobs).count(PRODUCTION_JOB) != 1
    ):
        raise b.BaselineError("WORKFLOW_CONSTRUCTION")
    names = _semantic_environment(body_jobs)
    production = _semantic_environment({PRODUCTION_JOB: body_jobs[PRODUCTION_JOB]})
    if (
        names.count("demo") != 1
        or production != ["demo"]
        or any("${{" in name for name in names)
    ):
        raise b.BaselineError("AMBIGUOUS_REVIEW_ENVIRONMENT")
    return b.digest(b.canonical(seen))


def authenticate(
    api: b.VerificationAPI,
    envelope: dict[str, Any],
    invocation: dict[str, Any],
    deadline: b.Deadline,
) -> dict[str, Any]:
    deadline.remaining()
    if invocation["attempt"] != 1 or invocation["job"] != PRODUCTION_JOB:
        raise b.BaselineError("REFRESH_RERUN_REFUSED")
    source = b.sha(envelope["source_commit"], 40)
    run_id = int(invocation["run_id"])
    run = api.get(f"actions/runs/{run_id}")
    if (
        run.get("id") != run_id
        or run.get("head_sha") != source
        or run.get("head_branch") != "main"
        or run.get("run_attempt") != 1
        or run.get("event") not in ("push", "workflow_dispatch")
        or run.get("path") != ENTRY
        or run.get("repository", {}).get("full_name") != envelope["repository"]
    ):
        raise b.BaselineError("APPROVAL_RUN_BINDING")
    construction = source_proof(api, source)
    if construction != envelope["construction_sha256"]:
        raise b.BaselineError("WORKFLOW_CONSTRUCTION")
    response = api.get(f"actions/runs/{run_id}/attempts/1/jobs?per_page=100")
    jobs = response.get("jobs")
    if (
        not isinstance(jobs, list)
        or response.get("total_count") != len(jobs)
        or len(jobs) >= 100
    ):
        raise b.BaselineError("APPROVAL_JOB_PAGINATION")
    owners = [job for job in jobs if job.get("name") == "release / " + OWNER_JOB]
    consumers = [
        job for job in jobs if job.get("name") == "release / " + PRODUCTION_JOB
    ]
    if (
        len(owners) != 1
        or len(consumers) != 1
        or owners[0].get("conclusion") != "success"
        or owners[0].get("run_id") != run_id
        or consumers[0].get("run_id") != run_id
        or b.origin_time(owners[0]["completed_at"])
        > b.origin_time(consumers[0]["started_at"])
    ):
        raise b.BaselineError("APPROVAL_JOB_BINDING")
    reviews = api.listing(f"actions/runs/{run_id}/approvals")
    decisions = [
        review
        for review in reviews
        if any(env.get("name") == "demo" for env in review.get("environments", []))
    ]
    if len(decisions) != 1:
        raise b.BaselineError("APPROVAL_REVIEW_AMBIGUOUS")
    review = decisions[0]
    user = review.get("user", {})
    if (
        review.get("state") != "approved"
        or user.get("type") != "User"
        or user.get("login") != "ludvigalden"
        or type(user.get("id")) is not int
        or user["id"] <= 0
    ):
        raise b.BaselineError("APPROVAL_REVIEWER")
    deadline.remaining()
    return {
        "construction_sha256": construction,
        "capture_job_id": owners[0]["id"],
        "production_job_id": consumers[0]["id"],
        "reviewer_id": user["id"],
        "reviewer": user["login"],
        "review_environment": "demo",
        "run_id": run_id,
        "attempt": 1,
        "source_commit": source,
    }

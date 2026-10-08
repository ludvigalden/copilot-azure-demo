"""Hermetic conditional refresh routing, identity, budgets and provenance controls."""

import base64
import copy
import http.client
import io
import itertools
import json
import re
import traceback
import types
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import timedelta
from http.client import HTTPMessage
from pathlib import Path

import authority_fixture as af
import pytest
from test_produce import release_fixture

import approval_proof
import authority_state as authority
import dependency_baseline as b
import external_state
import freshness
import promote
import release

CLAIM = {
    "schema": "ReleaseEnvelopeV2",
    "freshness_policy": promote.POLICY,
    "authorize_refresh": True,
    "power_platform_mode": "dormant_non_writing",
}


@pytest.mark.parametrize("bits", list(itertools.product((False, True), repeat=5)))
def test_all_component_routes(bits):
    selected = [
        name
        for name, enabled in zip(sorted(promote.COMPONENTS), bits, strict=True)
        if enabled
    ]
    with pytest.raises(b.BaselineError, match="RELEASE_POLICY_REQUIRED"):
        promote.route(None, selected)
    with pytest.raises(b.BaselineError, match="CONDITIONAL_POLICY"):
        promote.route({}, selected)
    supported = "app" in selected and not {"infra", "kb", "power_platform"} & set(
        selected
    )
    if supported:
        assert promote.route(CLAIM, selected) == "conditional"
    else:
        with pytest.raises(b.BaselineError):
            promote.route(CLAIM, selected)


@pytest.mark.parametrize(
    "change",
    [
        {"authorize_refresh": False},
        {"schema": "unknown"},
        {"freshness_policy": "unknown"},
        {"power_platform_mode": "active"},
        {"extra": True},
    ],
)
def test_invalid_claim_never_falls_back(change):
    with pytest.raises(b.BaselineError):
        promote.route(CLAIM | change, ["app", "power_platform"])


def observable_external(client):
    return {
        "auth_config": {
            "platform": {"enabled": False},
            "globalValidation": {},
            "identityProviders": {},
        },
        "application": {
            "id": af.API_OBJECT,
            "appId": client,
            "signInAudience": "AzureADMyOrg",
            "identifierUris": [],
            "requiredResourceAccess": [],
            "appRoles": [],
            "api": {
                "acceptMappedClaims": False,
                "knownClientApplications": [],
                "oauth2PermissionScopes": [],
                "preAuthorizedApplications": [],
                "requestedAccessTokenVersion": 2,
            },
            "web": {"redirectUris": []},
            "spa": {"redirectUris": []},
            "publicClient": {"redirectUris": []},
            "isFallbackPublicClient": False,
        },
        "service_principal": {
            "id": af.API,
            "appId": client,
            "accountEnabled": True,
            "appRoleAssignmentRequired": False,
            "servicePrincipalType": "Application",
            "appRoles": [],
            "oauth2PermissionScopes": [],
        },
        "grants": [],
        "assignments": [],
        "rai": {
            role: {
                "name": "default",
                "properties": {
                    "mode": "Blocking",
                    "basePolicyName": "Microsoft.Default",
                    "type": "SystemManaged",
                    "contentFilters": [],
                    "customBlocklists": [],
                },
            }
            for role in ("chat", "embedding")
        },
    }


def unit(tmp_path, monkeypatch):
    document, candidate, now = release_fixture(tmp_path, monkeypatch)
    candidate["components"] = ["app"]
    candidate["answer_prompt_sha256"] = b.digest(
        release.answer_prompt(
            (release.ROOT / release.ANSWER_PROMPT_SOURCE).read_text()
        ).encode()
    )
    root = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/demo-rg/providers/"
    tenant = "22222222-2222-2222-2222-222222222222"
    identity_id = af.WORKLOAD_CLIENT
    app_id = root + "Microsoft.App/containerApps/demo-app"
    ai_id = root + "Microsoft.CognitiveServices/accounts/demo-ai"
    settings = {
        "AZURE_CLIENT_ID": identity_id,
        "Search__Endpoint": "https://search.example",
        "Search__IndexName": "kb",
        "OpenAI__Endpoint": "https://judge.example",
        "OpenAI__DeploymentName": "chat",
        "AzureAd__TenantId": tenant,
        "AzureAd__ClientId": af.API_CLIENT,
        "AzureAd__Scope": "api://demo-api/access_as_user",
        "AzureAd__ClientCredentials__0__SourceType": "SignedAssertionFromManagedIdentity",
        "AzureAd__ClientCredentials__0__ManagedIdentityClientId": identity_id,
        "Tickets__ServiceUri": "https://demotickets.table.core.windows.net/",
    }
    plane = {
        "target": {
            "tenant_id": tenant,
            "subscription_id": root.split("/")[2],
            "app_resource_id": app_id,
            "search_resource_id": root + "Microsoft.Search/searchServices/demo-search",
            "ai_resource_id": ai_id,
            "tickets_resource_id": root
            + "Microsoft.Storage/storageAccounts/demotickets",
            "index_name": "kb",
            "chat_deployment": "chat",
            "embedding_deployment": "embedding",
        },
        "app": {
            "resource_id": app_id,
            "image": candidate["image"],
            "revision": candidate["revision"],
            "effective_environment": settings,
            "managed_identity": {
                "type": "UserAssigned",
                "userAssignedIdentities": {
                    root + "Microsoft.ManagedIdentity/userAssignedIdentities/demo-id": {
                        "clientId": identity_id,
                        "principalId": af.WORKLOAD,
                    }
                },
            },
            "ingress": {
                "additionalPortMappings": None,
                "allowInsecure": False,
                "clientCertificateMode": None,
                "corsPolicy": None,
                "customDomains": None,
                "exposedPort": 0,
                "external": True,
                "fqdn": "staging.example",
                "ipSecurityRestrictions": [],
                "stickySessions": None,
                "targetPort": 8080,
                "traffic": [{"latestRevision": True, "weight": 100}],
                "transport": "auto",
            },
            "secret_references": {},
            "container_configuration": {
                "name": "api",
                "resources": {"cpu": 0.25, "memory": "0.5Gi"},
                "probes": [],
            },
            "scale": {
                "minReplicas": 1,
                "maxReplicas": 2,
                "rules": None,
                "cooldownPeriod": 300,
                "pollingInterval": 30,
            },
            "environment_id": root + "Microsoft.App/managedEnvironments/demo-env",
            "workload_profile": "Consumption",
            "revision_suffix": "",
            "identity_settings": [],
            "runtime": None,
        },
        "models": {
            role: {
                "resource_id": ai_id
                + "/deployments/"
                + candidate[f"{role}_deployment"],
                "properties": {"model": candidate[f"{role}_model"]},
                "sku": {"name": "Standard", "capacity": 1},
            }
            for role in ("chat", "embedding")
        },
        "documents": {
            "count": 15,
            "content_vector_sha256": "d" * 64,
            "index_snapshot_sha256": candidate["index_snapshot_sha256"],
        },
        "index_schema_sha256": "d" * 64,
        "search_configuration": {
            "authOptions": {
                "aadOrApiKey": {"aadAuthFailureMode": "http401WithBearerChallenge"}
            },
            "disableLocalAuth": False,
            "publicNetworkAccess": "Enabled",
            "networkRuleSet": {"ipRules": [], "bypass": "None"},
        },
        "ai_configuration": {
            "disableLocalAuth": False,
            "publicNetworkAccess": "Enabled",
            "networkAcls": {"defaultAction": "Allow", "ipRules": []},
            "customSubDomainName": "demo-ai",
        },
        "ai_endpoint": "https://judge.example",
        "search_endpoint": "https://search.example",
    }
    plane["external_state"] = observable_external(af.API_CLIENT)
    plane["authority"], _ = af.fixture(plane)
    for model in plane["models"].values():
        model["properties"]["raiPolicyName"] = "default"
    candidate.update(release.authoritative_source_hashes())
    baseline = {
        "schema": "DependencyBaselineV1",
        "candidate": candidate,
        "captured_at": "2020-01-01T00:00:00Z",
        "planes": {"staging": plane, "production": copy.deepcopy(plane)},
    }
    imported = b.canonical(baseline)
    provenance = {
        "construction_sha256": "e" * 64,
        "capture_job": approval_proof.OWNER_JOB,
    }
    candidate_raw = b.canonical(candidate)
    evaluation_raw = b.canonical(document)
    envelope = CLAIM | {
        "run_id": "1",
        "run_attempt": 1,
        "repository": "o/r",
        "construction_sha256": "e" * 64,
        "component_status": {"power_platform": "skipped_not_selected"},
        "staging_verification": {
            "READINESS": "true",
            "SMOKE": "pass",
            "BOT": "pass",
            "ASSETS": "pass",
            "THEME": "pass",
        },
        "selected_components": ["app"],
        "source_commit": candidate["source_commit"],
        "image": candidate["image"],
        "candidate_sha256": b.digest(candidate_raw),
        "evaluation_sha256": b.digest(evaluation_raw),
        "baseline_sha256": b.digest(imported),
        "baseline_provenance": provenance,
    }
    return document, candidate, now, baseline, imported, provenance, envelope


def execute(
    values,
    capture=None,
    producer=None,
    clock=None,
    attempt=1,
    events=None,
    persist=None,
    deadline=None,
):
    document, candidate, now, baseline, imported, provenance, envelope = values
    originals = (b.canonical(envelope), b.canonical(candidate), b.canonical(document))
    events = [] if events is None else events

    def read(target):
        events.append("capture")
        return copy.deepcopy(baseline["planes"]["staging"])

    def refresh(bound, duration):
        events.append("producer")
        assert bound == candidate and 1 <= duration <= 2280
        result = copy.deepcopy(document)
        for key in ("started_at", "completed_at", "expires_at"):
            result["release_evidence"][key] = (
                now
                + timedelta(
                    seconds={"started_at": -1, "completed_at": 0, "expires_at": 7190}[
                        key
                    ]
                )
            ).isoformat()
        return b.canonical(result)

    result = promote.promote(
        originals,
        imported,
        provenance,
        capture or read,
        producer or refresh,
        lambda image: events.append(("mutate", image)),
        deadline or b.Deadline(2400, lambda: 0),
        {"run_id": "1", "attempt": attempt, "job": "conditional-production"},
        persist or (lambda supplement, raw: events.append("persist")),
        clock or (lambda: now),
        reviewed_originals=(
            b.digest(originals[0]),
            b.digest(originals[1]),
            b.digest(originals[2]),
        ),
    )
    return result, events


def test_dependency_drift_during_persistence_stops_write(tmp_path, monkeypatch):
    """Dependency change introduced at persistence is rejected before mutation."""
    values = unit(tmp_path, monkeypatch)
    mutated: list[str] = []

    def persist(supplement, raw):
        values[3]["planes"]["staging"]["documents"]["content_vector_sha256"] = "0" * 64

    with pytest.raises(b.BaselineError, match="DEPENDENCY_DRIFT"):
        promote.promote(
            (
                b.canonical(values[6]),
                b.canonical(values[1]),
                b.canonical(values[0]),
            ),
            values[4],
            values[5],
            lambda target: copy.deepcopy(values[3]["planes"]["staging"]),
            lambda bound, duration: pytest.fail("current evidence must not produce"),
            lambda image: mutated.append(image),
            b.Deadline(2400, lambda: 0),
            {"run_id": "1", "attempt": 1, "job": "conditional-production"},
            persist,
            lambda: values[2],
            reviewed_originals=(
                b.digest(b.canonical(values[6])),
                b.digest(b.canonical(values[1])),
                b.digest(b.canonical(values[0])),
            ),
        )
    assert not mutated


def test_current_reuses_and_expired_refreshes_once(tmp_path, monkeypatch):
    values = unit(tmp_path, monkeypatch)
    result, events = execute(values)
    assert events == ["capture"] * 6 + ["persist"] + ["capture"] * 2 + [
        ("mutate", values[1]["image"])
    ]
    assert result["refresh_count"] == 0
    assert result["original_evaluation_sha256"] == b.digest(b.canonical(values[0]))
    for key in ("started_at", "completed_at", "expires_at"):
        original = values[0]["release_evidence"][key]
        from datetime import datetime

        values[0]["release_evidence"][key] = (
            datetime.fromisoformat(original) - timedelta(hours=3)
        ).isoformat()
    values[6]["evaluation_sha256"] = b.digest(b.canonical(values[0]))
    result, events = execute(values)
    assert events == ["capture"] * 2 + ["producer"] + ["capture"] * 4 + [
        "persist",
    ] + ["capture"] * 2 + [
        ("mutate", values[1]["image"]),
    ]
    assert result["refresh_count"] == 1


@pytest.mark.parametrize("drift_at", [1, 2, 3, 4, 5, 6, 7, 8])
def test_drift_at_every_comparison_stops_write(tmp_path, monkeypatch, drift_at):
    values = unit(tmp_path, monkeypatch)
    calls = []

    def capture(target):
        calls.append(target)
        plane = copy.deepcopy(values[3]["planes"]["staging"])
        if len(calls) == drift_at:
            plane["documents"]["content_vector_sha256"] = "f" * 64
        return plane

    with pytest.raises(b.BaselineError, match="DEPENDENCY_DRIFT"):
        execute(values, capture=capture)
    assert len(calls) == drift_at


@pytest.mark.parametrize(
    "bad", ["future", "malformed", "failed", "budget", "rerun", "origin", "candidate"]
)
def test_non_age_or_origin_failure_cannot_refresh(tmp_path, monkeypatch, bad):
    values = unit(tmp_path, monkeypatch)
    if bad == "future":
        values[0]["release_evidence"]["completed_at"] = (
            values[2] + timedelta(minutes=1)
        ).isoformat()
    elif bad == "malformed":
        values[0]["release_evidence"]["expires_at"] = "bad"
    elif bad == "failed":
        values[0]["results"].pop()
    elif bad == "budget":
        values[0]["release_evidence"]["budgets"]["total_attempt_cap"] = 151
    elif bad == "origin":
        values[6]["baseline_sha256"] = "f" * 64
    elif bad == "candidate":
        values[1]["image"] = "other"
    values[6]["evaluation_sha256"] = b.digest(b.canonical(values[0]))
    with pytest.raises(b.BaselineError):
        execute(
            values,
            producer=lambda *_: pytest.fail("must not refresh"),
            attempt=2 if bad == "rerun" else 1,
        )


def test_drift_after_refresh_cannot_cure(tmp_path, monkeypatch):
    values = unit(tmp_path, monkeypatch)
    from datetime import datetime

    for key in ("started_at", "completed_at", "expires_at"):
        values[0]["release_evidence"][key] = (
            datetime.fromisoformat(values[0]["release_evidence"][key])
            - timedelta(hours=3)
        ).isoformat()
    values[6]["evaluation_sha256"] = b.digest(b.canonical(values[0]))
    calls = []

    def capture(target):
        calls.append(1)
        plane = copy.deepcopy(values[3]["planes"]["staging"])
        if len(calls) == 5:
            plane["documents"]["content_vector_sha256"] = "f" * 64
        return plane

    events = []
    with pytest.raises(b.BaselineError, match="DEPENDENCY_DRIFT"):
        execute(values, capture=capture, events=events)
    assert "producer" in events and "persist" not in events
    assert all(
        event != ("mutate", values[1]["image"])
        for event in events
        if isinstance(event, tuple)
    )


def test_expired_evidence_during_persistence_blocks_write(tmp_path, monkeypatch):
    values = unit(tmp_path, monkeypatch)
    from datetime import datetime

    for key in ("started_at", "completed_at", "expires_at"):
        values[0]["release_evidence"][key] = (
            datetime.fromisoformat(values[0]["release_evidence"][key])
            - timedelta(hours=3)
        ).isoformat()
    values[6]["evaluation_sha256"] = b.digest(b.canonical(values[0]))
    events = []
    with pytest.raises(b.BaselineError, match="EVIDENCE_EXPIRED_BEFORE_WRITE"):
        execute(
            values,
            events=events,
            persist=lambda supplement, raw: events.append("persist"),
            clock=lambda: (
                values[2] + (timedelta(hours=3) if "persist" in events else timedelta())
            ),
        )
    assert "persist" in events and not any(isinstance(event, tuple) for event in events)


def test_postcapture_reserve_before_producer(tmp_path, monkeypatch):
    values = unit(tmp_path, monkeypatch)
    from datetime import datetime

    for key in ("started_at", "completed_at", "expires_at"):
        values[0]["release_evidence"][key] = (
            datetime.fromisoformat(values[0]["release_evidence"][key])
            - timedelta(hours=3)
        ).isoformat()
    values[6]["evaluation_sha256"] = b.digest(b.canonical(values[0]))
    with pytest.raises(b.BaselineError):
        execute(
            values,
            producer=lambda *_: pytest.fail("must not refresh"),
            deadline=b.Deadline(100, lambda: 0),
        )


def test_optional_evaluation_selection_keeps_app_candidate(tmp_path, monkeypatch):
    values = unit(tmp_path, monkeypatch)
    values[6]["selected_components"] = ["app", "evaluation"]
    result, events = execute(values)
    assert result["refresh_count"] == 0 and ("mutate", values[1]["image"]) in events


def test_malformed_or_incomplete_baseline_rejects(tmp_path, monkeypatch):
    values = unit(tmp_path, monkeypatch)
    for bad in ("schema", "planes", "candidate"):
        document = copy.deepcopy(values[3])
        if bad == "planes":
            document["planes"].pop("production")
        else:
            document[bad] = "wrong" if bad == "schema" else {"changed": True}
        with pytest.raises(b.BaselineError):
            execute(
                (
                    values[0],
                    values[1] if bad != "candidate" else {"image": "other"},
                    values[2],
                    document,
                    b.canonical(document),
                    values[5],
                    values[6] | {"baseline_sha256": b.digest(b.canonical(document))},
                )
            )


def test_strict_evidence_validator_rejects_age_only(tmp_path, monkeypatch):
    import gate

    values = unit(tmp_path, monkeypatch)
    later = values[2] + timedelta(hours=3)
    assert (
        freshness.classify(values[0], values[1], later).state
        == freshness.State.EXPIRED_VALID
    )
    assert not gate.gate_release_document(values[0], values[1], later).passed


def row(identifier="a", vector=None):
    return {
        "id": identifier,
        "title": "title",
        "content": "content",
        "url": "url",
        "embedding": [1.0, 2.0] if vector is None else vector,
    }


def test_content_and_vectors_and_terminal_probe():
    rows = [row()]
    calls = []

    def fetch(skip):
        calls.append(skip)
        return b.canonical({"value": rows if skip == 0 else []})

    result = b.fingerprint_documents(fetch, 1, 2, b.Deadline(1000, lambda: 0))
    assert calls == [0, 1000]
    content = [{key: value for key, value in rows[0].items() if key != "embedding"}]
    assert result["index_snapshot_sha256"] == b.digest(
        json.dumps(content, sort_keys=True).encode()
    )
    rows[0]["embedding"] = [2.0, 3.0]
    assert (
        b.fingerprint_documents(fetch, 1, 2, b.Deadline(1000, lambda: 0))[
            "content_vector_sha256"
        ]
        != result["content_vector_sha256"]
    )


@pytest.mark.parametrize(
    "values,count",
    [
        ([row(), row()], 2),
        ([row("b"), row("a")], 2),
        ([row(vector=[])], 1),
        ([row(vector=[True, 2])], 1),
        ([row(vector=[float("inf"), 2])], 1),
        ([row()], 2),
        ([], 1),
    ],
)
def test_malformed_fingerprints(values, count):
    def fetch(skip):
        return json.dumps({"value": values if skip == 0 else []}).encode()

    with pytest.raises(b.BaselineError):
        b.fingerprint_documents(fetch, count, 2, b.Deadline(1000, lambda: 0))


def test_byte_fence_before_parse(monkeypatch):
    monkeypatch.setattr(b, "PAGE_BYTES", 10)
    stream = io.BytesIO(b"x" * 100)
    with pytest.raises(b.BaselineError, match="RESPONSE_BYTES"):
        b.bounded_read(stream, 10, b.Deadline(1000, lambda: 0))
    assert stream.tell() == 11
    with pytest.raises(b.BaselineError, match="RESPONSE_BYTES"):
        b.fingerprint_documents(
            lambda _: b"invalid-json" * 2, 1, 2, b.Deadline(1000, lambda: 0)
        )


def test_total_byte_and_duration_fences(monkeypatch):
    monkeypatch.setattr(b, "TOTAL_BYTES", 1)
    with pytest.raises(b.BaselineError, match="TOTAL_BYTES"):
        b.fingerprint_documents(
            lambda _: b.canonical({"value": [row()]}), 1, 2, b.Deadline(1000, lambda: 0)
        )
    clock = [0]

    def fetch(_):
        clock[0] = 181
        return b.canonical({"value": []})

    with pytest.raises(b.BaselineError, match="DEADLINE"):
        b.fingerprint_documents(fetch, 1, 2, b.Deadline(1000, lambda: clock[0]))


def test_historical_routes_are_absent():
    # Removed origin proof is not an alternative authorization surface.
    for name in ("TrustedPolicy", "authenticate_origin", "import_existing"):
        assert not hasattr(b, name)


def test_model_unknown_configuration_rejects():
    model = {
        "id": "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/demo-rg/providers/Microsoft.CognitiveServices/accounts/demo-ai/deployments/chat",
        "properties": {"model": {"name": "chat", "version": "1", "format": "OpenAI"}},
        "sku": {"name": "Standard", "capacity": 1},
    }
    assert b.model_projection(model) == {
        "resource_id": model["id"],
        "properties": model["properties"],
        "sku": model["sku"],
    }
    model["properties"]["unknown"] = "sensitive remote response"
    with pytest.raises(b.BaselineError, match="^UNSUPPORTED_SCHEMA$"):
        b.model_projection(model)


def test_redirect_strips_credentials():
    request = urllib.request.Request(
        "https://api.github.com/a", headers={"Authorization": "secret"}
    )
    redirected = b.SafeRedirect().redirect_request(
        request,
        io.BytesIO(),
        302,
        "",
        HTTPMessage(),
        "https://storage.example/artifact",
    )
    assert redirected is not None
    assert not redirected.has_header("Authorization")


def test_key_denial_is_redacted(monkeypatch):
    def denied(*args, **kwargs):
        raise OSError("sensitive remote response")

    monkeypatch.setattr(promote.subprocess, "run", denied)
    document = {
        "planes": {
            name: {"target": {"ai_resource_id": "same"}}
            for name in ("staging", "production")
        }
    }
    with pytest.raises(b.BaselineError, match="^KEY_DENIED$"):
        promote.retrieve_key(document, b.Deadline(1000, lambda: 0))


def test_workflow_single_policy_and_capture_owner():
    import yaml

    text = (approval_proof.ROOT / approval_proof.BODY).read_text()
    jobs = yaml.safe_load(text)["jobs"]
    for name in (
        "deploy-production",
        "infra-apply",
        "ingest-production",
        "pp-production",
        "conditional-import",
    ):
        assert name not in jobs
    conditional = jobs[approval_proof.PRODUCTION_JOB]
    assert conditional["environment"] == "demo"
    assert conditional["timeout-minutes"] == 45
    assert conditional["concurrency"] == {
        "group": "deploy-demo",
        "cancel-in-progress": False,
    }
    assert "approval-capture" in conditional["needs"]
    steps = conditional["steps"]
    ids = [step.get("id") for step in steps]
    assert (
        ids.index("promotion")
        < ids.index("readiness")
        < ids.index("smoke")
        < ids.index("bot")
    )
    scripts = "\n".join(step.get("run", "") for step in steps)
    assert "scripts/verify-bot-conversation.sh" in scripts
    assert "failure does not prove it stayed unchanged" in scripts
    assert "--verify-only" not in text and "promotion_route" not in text
    consume = next(
        i
        for i, step in enumerate(steps)
        if "eval/promote.py consume" in step.get("run", "")
    )
    login = next(
        i
        for i, step in enumerate(steps)
        if step.get("uses", "").startswith("azure/login@")
    )
    assert consume < login
    assert conditional["permissions"]["actions"] == "read"
    capture = jobs[approval_proof.OWNER_JOB]
    assert capture["environment"] == "staging"
    assert any(
        step.get("with", {}).get("name") == approval_proof.ARTIFACT
        for step in capture["steps"]
    )
    for guard in (
        "CURRENT evidence is reused without refresh",
        "at most one bounded refresh after this approval",
        "must remain unchanged; drift rejects",
        "refreshed evidence is supplementary",
        "never renews an infrastructure plan",
    ):
        assert guard in text


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.pop("captured_at"),
        lambda value: value.update(captured_at="2099-01-01T00:00:00Z"),
        lambda value: value.update(captured_at="2020-01-01T00:00:00"),
        lambda value: value["candidate"].update(unknown="canary"),
        lambda value: value["candidate"].update(source_commit="invalid"),
        lambda value: value["planes"]["staging"]["documents"].update(count=True),
        lambda value: value["planes"]["staging"]["target"].update(credential="canary"),
        lambda value: value["planes"]["staging"]["app"]["ingress"].update(
            credential="canary"
        ),
        lambda value: value["planes"]["staging"]["app"]["managed_identity"][
            "userAssignedIdentities"
        ].update(arbitrary={"credential": "canary"}),
        lambda value: value["planes"]["staging"]["app"]["container_configuration"][
            "resources"
        ].update(credential="canary"),
        lambda value: value["planes"]["staging"]["models"]["chat"]["sku"].update(
            credential="canary"
        ),
        lambda value: value["planes"]["staging"]["ai_configuration"].update(
            credential="canary"
        ),
        lambda value: value["planes"]["staging"]["search_configuration"]["authOptions"][
            "aadOrApiKey"
        ].update(credential="canary"),
        lambda value: value["planes"]["staging"]["app"]["ingress"]["traffic"][0].update(
            weight=True
        ),
    ],
)
def test_recursive_schema_and_nested_credentials_reject(tmp_path, monkeypatch, change):
    values = unit(tmp_path, monkeypatch)
    b.validate_baseline(values[3])
    change(values[3])
    with pytest.raises(b.BaselineError) as error:
        b.validate_baseline(values[3])
    assert "canary" not in str(error.value)


def test_unobservable_external_state_is_containment(tmp_path, monkeypatch):
    values = unit(tmp_path, monkeypatch)
    state = values[3]["planes"]["staging"]["external_state"]
    state["auth_config"]["platform"]["enabled"] = True
    with pytest.raises(b.BaselineError, match="^AUTH_CONFIG_UNOBSERVABLE$"):
        external_state.validate(state)


def test_reviewed_originals_cannot_be_inferred_from_envelope(tmp_path, monkeypatch):
    values = unit(tmp_path, monkeypatch)
    originals = (b.canonical(values[6]), b.canonical(values[1]), b.canonical(values[0]))
    for anchor in (None, ("0" * 64, "0" * 64, "0" * 64)):
        with pytest.raises(
            b.BaselineError, match="^REVIEWED_ORIGINAL_ASSOCIATION_REQUIRED$"
        ):
            promote.promote(
                originals,
                values[4],
                values[5],
                lambda *_: pytest.fail("unreviewed must not capture"),
                lambda *_: pytest.fail("unreviewed must not produce"),
                lambda *_: pytest.fail("unreviewed must not write"),
                b.Deadline(2400, lambda: 0),
                {"run_id": "1", "attempt": 1, "job": "conditional-production"},
                lambda *_: pytest.fail("unreviewed must not persist"),
                lambda: values[2],
                reviewed_originals=anchor,
            )


def artifact_archive(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as bundle:
        for name, raw in files.items():
            bundle.writestr(name, raw)
    return stream.getvalue()


@pytest.fixture
def prospective_route(tmp_path, monkeypatch):
    """Real capture and proof adapters; only HTTP and CLI transports are canned."""
    import subprocess
    import urllib.parse

    real_run = subprocess.run
    real_request_bytes = b.request_bytes
    values = unit(tmp_path, monkeypatch)
    document, candidate, now, bound, _, _, _ = values
    plane = bound["planes"]["staging"]
    endpoint = "https://demo-srch.search.windows.net"
    plane["search_endpoint"] = endpoint
    plane["target"]["search_resource_id"] = plane["target"][
        "search_resource_id"
    ].replace("demo-search", "demo-srch")
    plane["app"]["effective_environment"]["Search__Endpoint"] = endpoint
    targets = {"staging": plane["target"], "production": copy.deepcopy(plane["target"])}
    targets["production"]["app_resource_id"] = targets["production"][
        "app_resource_id"
    ].replace("demo-app", "prod-app")
    definition = safe_index()
    definition["vectorSearch"]["vectorizers"] = [
        {
            "name": "vectorizer",
            "kind": "azureOpenAI",
            "azureOpenAIParameters": {
                "deploymentId": "embedding",
                "resourceUri": plane["ai_endpoint"],
            },
        }
    ]
    definition["vectorSearch"]["profiles"][0]["vectorizer"] = "vectorizer"
    rows = [row(f"{i:02d}") for i in range(15)]
    candidate["index_snapshot_sha256"] = b.fingerprint_documents(
        lambda skip: b.canonical({"value": rows if skip == 0 else []}),
        15,
        2,
        b.Deadline(1000, lambda: 0),
    )["index_snapshot_sha256"]
    plane["authority"], authority_responses = af.fixture(plane)
    responses = dict(authority_responses)
    for target in targets.values():
        app = plane["app"]
        identity = app["managed_identity"]
        # ARM GET answers the workload identity dictionary keys with the
        # resourceGroups segment lowercased; mirror that raw wire shape so
        # the projection's canonicalization is exercised end to end.
        raw_identity = {
            "type": identity["type"],
            "userAssignedIdentities": {
                resource.replace("/resourceGroups/", "/resourcegroups/"): binding
                for resource, binding in identity["userAssignedIdentities"].items()
            },
        }
        responses[target["app_resource_id"]] = {
            "id": target["app_resource_id"],
            "identity": raw_identity,
            "properties": {
                "configuration": {
                    "activeRevisionsMode": "Single",
                    "dapr": None,
                    "identitySettings": app["identity_settings"],
                    "ingress": dict(app["ingress"], transport="Auto"),
                    "maxInactiveRevisions": 0,
                    "registries": None,
                    "runtime": app["runtime"],
                    "secrets": None,
                    "service": None,
                },
                "template": {
                    "containers": [
                        {
                            "name": "api",
                            "image": app["image"],
                            "env": [
                                {"name": key, "value": value}
                                for key, value in app["effective_environment"].items()
                            ],
                            "probes": app["container_configuration"]["probes"],
                            "resources": app["container_configuration"]["resources"],
                        }
                    ],
                    "initContainers": None,
                    "revisionSuffix": app["revision_suffix"],
                    "scale": app["scale"],
                    "serviceBinds": None,
                    "terminationGracePeriodSeconds": None,
                    "volumes": [],
                },
                "latestReadyRevisionName": app["revision"],
                "latestRevisionName": app["revision"],
                "managedEnvironmentId": app["environment_id"],
                "workloadProfileName": app["workload_profile"],
            },
        }
        responses[target["app_resource_id"] + "/authConfigs/current"] = {
            "properties": plane["external_state"]["auth_config"]
        }
    responses[plane["target"]["search_resource_id"]] = {
        "id": plane["target"]["search_resource_id"],
        "identity": {
            "type": "SystemAssigned",
            "principalId": af.SEARCH,
            "tenantId": plane["target"]["tenant_id"],
        },
        "properties": plane["search_configuration"],
    }
    responses[plane["target"]["ai_resource_id"]] = {
        "id": plane["target"]["ai_resource_id"],
        "properties": plane["ai_configuration"] | {"endpoint": plane["ai_endpoint"]},
    }
    for model in plane["models"].values():
        responses[model["resource_id"]] = {
            "id": model["resource_id"],
            "properties": model["properties"],
            "sku": model["sku"],
        }
    responses[plane["target"]["ai_resource_id"] + "/raiPolicies/default"] = plane[
        "external_state"
    ]["rai"]["chat"]
    events, commands, archives, metadata = [], [], {}, {}
    source = candidate["source_commit"]
    start, created, end = (
        "2020-01-01T00:00:00Z",
        "2020-01-01T00:01:00Z",
        "2020-01-01T00:02:00Z",
    )
    github = {
        "actions/runs/1": {
            "id": 1,
            "head_sha": source,
            "head_branch": "main",
            "run_attempt": 1,
            "event": "workflow_dispatch",
            "path": approval_proof.ENTRY,
            "repository": {"full_name": "o/r"},
        },
        "actions/runs/1/attempts/1/jobs?per_page=100": {
            "total_count": 2,
            "jobs": [
                {
                    "id": 2,
                    "name": "release / " + approval_proof.OWNER_JOB,
                    "run_id": 1,
                    "conclusion": "success",
                    "started_at": start,
                    "completed_at": end,
                },
                {
                    "id": 4,
                    "name": "release / " + approval_proof.PRODUCTION_JOB,
                    "run_id": 1,
                    "started_at": end,
                },
            ],
        },
        "actions/runs/1/approvals?per_page=100": [
            {
                "state": "approved",
                "environments": [{"name": "demo"}],
                "user": {"type": "User", "login": "ludvigalden", "id": 8},
            }
        ],
    }

    def transport(url, headers, deadline, method="GET", body=None, limit=b.PAGE_BYTES):
        events.append(url)
        deadline.remaining()
        parsed = urllib.parse.urlsplit(url)
        if parsed.netloc == "api.github.com":
            path = url.split("/repos/o/r/", 1)[1]
            if path.startswith("contents/"):
                local = path.removeprefix("contents/").split("?", 1)[0]
                return b.canonical(
                    {
                        "encoding": "base64",
                        "content": base64.b64encode(
                            (approval_proof.ROOT / local).read_bytes()
                        ).decode(),
                    }
                )
            if path.startswith("actions/artifacts/"):
                artifact_id = int(path.split("/")[2])
                if path.endswith("/zip"):
                    return archives[artifact_id]
                return b.canonical(metadata[artifact_id])
            return b.canonical(github[path])
        if parsed.netloc == "management.azure.com":
            assert method == ("POST" if parsed.path.endswith("/getEntities") else "GET")
            return b.canonical(responses[parsed.path])
        if parsed.netloc == "graph.microsoft.com":
            if parsed.path in responses:
                return b.canonical(responses[parsed.path])
            kind = parsed.path.rsplit("/", 1)[1]
            state = plane["external_state"]
            items = {
                "applications": [state["application"]],
                "servicePrincipals": [state["service_principal"]],
                "oauth2PermissionGrants": state["grants"],
                "appRoleAssignedTo": state["assignments"],
            }
            return b.canonical({"value": items[kind]})
        assert parsed.netloc == "demo-srch.search.windows.net", url
        if parsed.path.endswith("/$count"):
            return b"15"
        if parsed.path.endswith("/search"):
            assert method == "POST" and body is not None
            return b.canonical({"value": rows if json.loads(body)["skip"] == 0 else []})
        return b.canonical(definition)

    def command(args, **kwargs):
        commands.append(args)
        if args[:3] == ["az", "account", "get-access-token"]:
            return subprocess.CompletedProcess(args, 0, b"fixture-token", b"")
        if args[:3] == ["az", "rest", "--method"]:
            return subprocess.CompletedProcess(args, 0, b"fixture-key", b"")
        assert args[:3] == ["az", "containerapp", "update"]
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(b, "request_bytes", transport)
    monkeypatch.setattr(promote.subprocess, "run", command)
    for key, value in {
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": "o/r",
        "GITHUB_SHA": source,
        "GITHUB_RUN_ID": "1",
        "GITHUB_RUN_ATTEMPT": "1",
        "GH_TOKEN": "fixture",
        "GITHUB_JOB": "conditional-production",
        "PROMOTION_ENVIRONMENT": "demo",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("ACT", raising=False)
    api = b.GitHubAPI("o/r", "fixture", b.Deadline(2400, lambda: 0))
    return locals()


def prepare_fixture_route(h):
    envelope = {
        key: h["values"][6][key]
        for key in (
            "source_commit",
            "image",
            "component_status",
            "staging_verification",
        )
    }
    paths = [
        h["tmp_path"] / name
        for name in ("input.json", "candidate.json", "evaluation.json", "targets.json")
    ]
    for path, value in zip(
        paths, (envelope, h["candidate"], h["document"], h["targets"]), strict=True
    ):
        path.write_bytes(b.canonical(value))
    originals = [path.read_bytes() for path in paths]
    output = h["tmp_path"] / "approved"
    assert (
        promote.main(
            [
                "prepare",
                "--envelope",
                str(paths[0]),
                "--candidate",
                str(paths[1]),
                "--evaluation",
                str(paths[2]),
                "--targets",
                str(paths[3]),
                "--claim",
                json.dumps(CLAIM),
                "--components",
                "app",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert [path.read_bytes() for path in paths] == originals
    files = {name: (output / name).read_bytes() for name in promote.ARTIFACT_MEMBERS}
    archive = artifact_archive(files)
    h["archives"][5] = archive
    h["metadata"][5] = {
        "id": 5,
        "name": approval_proof.ARTIFACT,
        "expired": False,
        "workflow_run": {"id": 1, "head_sha": h["candidate"]["source_commit"]},
        "digest": "sha256:" + b.digest(archive),
        "created_at": h["created"],
    }
    h["files"] = files
    return files, [
        "--envelope",
        str(output / "release-identity.json"),
        "--candidate",
        str(output / "evidence/candidate.json"),
        "--evaluation",
        str(output / "evidence/evaluation.json"),
        "--baseline",
        str(output / "dependency-baseline.json"),
        "--artifact-id",
        "5",
        "--artifact-digest",
        b.digest(archive),
    ]


def workflow_fixture_tail(tmp_path, attempted="true", updated="true", failed=""):
    """Execute source shell with local az/curl/bot stubs, not authentic delivery."""
    import os
    import subprocess

    text = (
        Path(__file__).resolve().parents[2] / ".github/workflows/release-candidate.yml"
    ).read_text()
    job = text.split("  conditional-production:\n", 1)[1].split(
        "  deploy-production:\n", 1
    )[0]
    blocks = re.split(r"(?m)^      - ", job)[1:]
    summary = tmp_path / "summary.txt"
    summary.write_text("")
    env = {
        "PATH": os.environ["PATH"],
        "GITHUB_STEP_SUMMARY": str(summary),
        "PRODUCTION_PREFIX": "fixture",
        "ATTEMPTED": attempted,
        "UPDATED": updated,
        "PROMOTION": "success" if updated == "true" else "failure",
        "READINESS": "skipped",
        "SMOKE": "skipped",
        "BOT": "skipped",
    }
    stubs = (
        'az() { printf "fixture.invalid\\n"; }; '
        'curl() { [ "$FAIL" != "$STAGE" ] || return 22; printf "200"; }; '
        "sleep() { return 0; }; "
        'bash() { [ "$1" = scripts/verify-bot-conversation.sh ] || return 90; '
        '[ "$FAIL" != bot ]; }; '
    )
    stopped = updated != "true"
    for stage, label in (
        ("readiness", "READINESS"),
        ("smoke", "SMOKE"),
        ("bot", "BOT"),
    ):
        block = next(part for part in blocks if f"        id: {stage}\n" in part)
        script = block.split("        run: |\n", 1)[1]
        script = "\n".join(
            line[10:] for line in script.splitlines() if line.startswith("          ")
        )
        assert (
            subprocess.run(
                ["/bin/bash", "-n"],
                input=script.encode(),
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
        if stopped:
            continue
        rc = subprocess.run(
            ["/bin/bash", "-e", "-c", stubs + script],
            cwd=tmp_path,
            env=env | {"FAIL": failed, "STAGE": stage},
            capture_output=True,
            check=False,
        ).returncode
        env[label] = "success" if rc == 0 else "failure"
        stopped = rc != 0
    outcome = next(
        part
        for part in blocks
        if "Record actual conditional production outcome" in part
    )
    script = outcome.split("        run: |\n", 1)[1]
    script = "\n".join(
        line[10:] for line in script.splitlines() if line.startswith("          ")
    )
    result = subprocess.run(
        ["/bin/bash", "-e", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads((tmp_path / "conditional-evidence/outcome.json").read_bytes())


@pytest.mark.parametrize(
    "attempted,updated,failed,expected",
    [
        ("", "", "", "not_attempted"),
        ("true", "", "", "failed"),
        ("true", "true", "readiness", "partial"),
        ("true", "true", "smoke", "partial"),
        ("true", "true", "bot", "partial"),
        ("true", "true", "", "succeeded"),
    ],
)
def test_actual_workflow_tail_under_transport_assumptions(
    tmp_path, attempted, updated, failed, expected
):
    assert (
        workflow_fixture_tail(tmp_path, attempted, updated, failed)["outcome"]
        == expected
    )


@pytest.mark.parametrize("expired", [False, True])
def test_full_cli_prospective_real_adapters(prospective_route, monkeypatch, expired):
    from datetime import datetime

    from test_produce import answer_body, judge_body

    import produce

    h = prospective_route
    if expired:
        for key in ("started_at", "completed_at", "expires_at"):
            h["document"]["release_evidence"][key] = (
                datetime.fromisoformat(h["document"]["release_evidence"][key])
                - timedelta(hours=3)
            ).isoformat()
    files, args = prepare_fixture_route(h)
    assert promote.main(["consume", *args]) == 0
    calls = []
    questions = {q.query: q for q in produce.load_dataset(produce.DEFAULT_DATASET)}

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, request, timeout):
            payload = json.loads(request.data)
            calls.append((request.full_url, payload))
            if "question" in payload:
                body = {}
                if payload["question"] != "hello":
                    question = questions[payload["question"]]
                    body = answer_body()
                    body["citations"][0]["url"] = (
                        "https://github.com/o/r/blob/"
                        + "a" * 40
                        + "/kb/"
                        + question.expected_article
                    )
            else:
                body = judge_body()
            return Response(json.dumps(body).encode())

    monkeypatch.setattr(promote.urllib.request, "build_opener", lambda *_: Opener())
    monkeypatch.setattr(promote.time, "sleep", lambda _: None)
    output = h["tmp_path"] / "supplement"
    assert promote.main(["promote", *args, "--output", str(output)]) == 0
    supplement = json.loads((output / "supplement.json").read_bytes())
    assert supplement["refresh_count"] == int(expired)
    assert supplement["deployment_outcome"] == "not_attempted"
    assert (
        sum(
            "/containerApps/" in url and "/authConfigs/" not in url
            for url in h["events"]
        )
        == 10
    )
    assert len(calls) == (31 if expired else 0)
    assert sum(c[:3] == ["az", "rest", "--method"] for c in h["commands"]) == int(
        expired
    )
    assert sum(c[:3] == ["az", "containerapp", "update"] for c in h["commands"]) == 1
    result = json.loads((output / "result.json").read_bytes())
    assert freshness.classify(result, h["candidate"]).state == freshness.State.CURRENT
    assert {
        name: (h["tmp_path"] / "approved" / name).read_bytes() for name in files
    } == files
    archive_at = next(i for i, url in enumerate(h["events"]) if url.endswith("/zip"))
    review_at = next(i for i, url in enumerate(h["events"]) if "/approvals?" in url)
    assert archive_at < review_at
    monkeypatch.setattr(promote.subprocess, "run", h["real_run"])
    outcome = workflow_fixture_tail(output)
    assert outcome["outcome"] == "succeeded"
    assert outcome["readiness"] == outcome["http_smoke"] == outcome["bot"] == "success"
    assert json.loads((output / "supplement.json").read_bytes()) == supplement


@pytest.mark.parametrize("final_boundary", [False, True])
def test_real_adapter_identity_before_age_and_after_persistence(
    prospective_route, monkeypatch, final_boundary
):
    from datetime import datetime

    h = prospective_route
    if not final_boundary:
        for key in ("started_at", "completed_at", "expires_at"):
            h["document"]["release_evidence"][key] = (
                datetime.fromisoformat(h["document"]["release_evidence"][key])
                - timedelta(hours=3)
            ).isoformat()
    files, args = prepare_fixture_route(h)
    transport = h["transport"]
    output = h["tmp_path"] / "supplement"
    captures = 0

    def drift(url, headers, deadline, method="GET", body=None, limit=b.PAGE_BYTES):
        nonlocal captures
        raw = transport(url, headers, deadline, method=method, body=body, limit=limit)
        if "/containerApps/" in url and "/authConfigs/" not in url:
            captures += 1
            if captures == (7 if final_boundary else 1):
                assert (output / "supplement.json").exists() == final_boundary
                response = b.object_json(raw)
                response["properties"]["template"]["scale"]["maxReplicas"] += 1
                return b.canonical(response)
        return raw

    monkeypatch.setattr(b, "request_bytes", drift)
    monkeypatch.setattr(
        promote.urllib.request,
        "build_opener",
        lambda *_: pytest.fail("identity drift must not reach the evaluator"),
    )
    assert promote.main(["promote", *args, "--output", str(output)]) == 1
    assert (output / "supplement.json").exists() == final_boundary
    assert not any(c[:3] == ["az", "rest", "--method"] for c in h["commands"])
    assert not any(c[:3] == ["az", "containerapp", "update"] for c in h["commands"])
    assert {
        name: (h["tmp_path"] / "approved" / name).read_bytes() for name in files
    } == files


def consumed(h, files, invocation=None, raw=None):
    return promote.consume(
        (
            files["release-identity.json"],
            files["evidence/candidate.json"],
            files["evidence/evaluation.json"],
        ),
        files["dependency-baseline.json"] if raw is None else raw,
        h["api"],
        5,
        b.digest(h["archives"][5]),
        invocation or {"run_id": "1", "attempt": 1, "job": "conditional-production"},
        b.Deadline(1000, lambda: 0),
    )


@pytest.mark.parametrize(
    "fault,error",
    [
        ("digest", "ARTIFACT_DIGEST_BINDING"),
        ("foreign_run", "ARTIFACT_RUN_BINDING"),
        ("missing", "ARTIFACT_UNAVAILABLE"),
        ("bytes", "SAME_RUN_BYTES_CHANGED"),
        ("archive", "ARTIFACT_DIGEST_BINDING"),
        ("members", "ARTIFACT_ARCHIVE_INVALID"),
        ("baseline", "BASELINE_ORIGIN_CHANGED"),
        ("rerun", "REFRESH_RERUN_REFUSED"),
    ],
)
def test_prospective_transport_guards(prospective_route, fault, error):
    h = prospective_route
    files, _ = prepare_fixture_route(h)
    invocation, raw = None, None
    if fault == "digest":
        h["metadata"][5]["digest"] = "sha256:" + "f" * 64
    elif fault == "foreign_run":
        h["metadata"][5]["workflow_run"]["id"] = 9
    elif fault == "missing":
        h["metadata"][5]["expired"] = True
    elif fault == "bytes":
        files = files | {"release-identity.json": files["release-identity.json"] + b" "}
    elif fault == "archive":
        h["archives"][5] += b"changed"
    elif fault == "members":
        h["archives"][5] = artifact_archive(files | {"extra.json": b"{}"})
        h["metadata"][5]["digest"] = "sha256:" + b.digest(h["archives"][5])
    elif fault == "baseline":
        raw = files["dependency-baseline.json"] + b" "
    else:
        invocation = {"run_id": "1", "attempt": 2, "job": "conditional-production"}
    with pytest.raises(b.BaselineError, match="^" + error + "$"):
        consumed(h, files, invocation, raw)
    assert not any("/approvals?" in url for url in h["events"])


@pytest.mark.parametrize(
    "fault,error",
    [
        ("wrong_job", "APPROVAL_JOB_BINDING"),
        ("failed_owner", "APPROVAL_JOB_BINDING"),
        ("job_foreign_run", "APPROVAL_JOB_BINDING"),
        ("job_order", "APPROVAL_JOB_BINDING"),
        ("pagination", "APPROVAL_JOB_PAGINATION"),
        ("foreign_source", "APPROVAL_RUN_BINDING"),
        ("foreign_path", "APPROVAL_RUN_BINDING"),
        ("foreign_repo", "APPROVAL_RUN_BINDING"),
        ("foreign_event", "APPROVAL_RUN_BINDING"),
        ("duplicate_review", "APPROVAL_REVIEW_AMBIGUOUS"),
        ("no_review", "APPROVAL_REVIEW_AMBIGUOUS"),
        ("rejected", "APPROVAL_REVIEWER"),
        ("reviewer", "APPROVAL_REVIEWER"),
        ("bot", "APPROVAL_REVIEWER"),
        ("capture_time", "ARTIFACT_CAPTURE_TIME"),
    ],
)
def test_returned_proof_guards(prospective_route, fault, error):
    h = prospective_route
    files, _ = prepare_fixture_route(h)
    jobs = h["github"]["actions/runs/1/attempts/1/jobs?per_page=100"]
    run = h["github"]["actions/runs/1"]
    reviews = h["github"]["actions/runs/1/approvals?per_page=100"]
    if fault == "wrong_job":
        jobs["jobs"][0]["name"] = "release / other"
    elif fault == "failed_owner":
        jobs["jobs"][0]["conclusion"] = "failure"
    elif fault == "job_foreign_run":
        jobs["jobs"][0]["run_id"] = 9
    elif fault == "job_order":
        jobs["jobs"][0]["completed_at"] = "2020-01-02T00:00:00Z"
    elif fault == "pagination":
        jobs["total_count"] = 3
    elif fault == "foreign_source":
        run["head_sha"] = "f" * 40
    elif fault == "foreign_path":
        run["path"] = "other.yml"
    elif fault == "foreign_repo":
        run["repository"]["full_name"] = "other/repo"
    elif fault == "foreign_event":
        run["event"] = "pull_request"
    elif fault == "duplicate_review":
        reviews.append(copy.deepcopy(reviews[0]))
    elif fault == "no_review":
        reviews.clear()
    elif fault == "rejected":
        reviews[0]["state"] = "rejected"
    elif fault == "reviewer":
        reviews[0]["user"]["login"] = "other"
    elif fault == "bot":
        reviews[0]["user"]["type"] = "Bot"
    else:
        h["metadata"][5]["created_at"] = "2019-01-01T00:00:00Z"
    with pytest.raises(b.BaselineError, match="^" + error + "$"):
        consumed(h, files)
    assert not any(c[:3] == ["az", "containerapp", "update"] for c in h["commands"])


def safe_index():
    return {
        "name": "kb",
        "fields": [
            {"name": key, "type": "Edm.String"}
            for key in ("id", "title", "content", "url")
        ]
        + [
            {
                "name": "embedding",
                "type": "Collection(Edm.Single)",
                "dimensions": 2,
                "retrievable": True,
            }
        ],
        "vectorSearch": {
            "profiles": [{"name": "profile", "algorithm": "algorithm"}],
            "algorithms": [
                {
                    "name": "algorithm",
                    "kind": "hnsw",
                    "hnswParameters": {
                        "metric": "cosine",
                        "m": 4,
                        "efConstruction": 400,
                        "efSearch": 500,
                    },
                }
            ],
        },
    }


def test_index_nested_configuration_rejects():
    definition = safe_index()
    assert b.index_projection(definition)["name"] == "kb"
    for section in (
        definition["fields"][0],
        definition["vectorSearch"]["profiles"][0],
        definition["vectorSearch"]["algorithms"][0]["hnswParameters"],
    ):
        section["credential"] = "canary"
        with pytest.raises(b.BaselineError):
            b.index_projection(definition)
        section.pop("credential")
    definition["vectorSearch"]["vectorizers"] = [
        {
            "name": "vectorizer",
            "kind": "azureOpenAI",
            "azureOpenAIParameters": {"apiKey": "canary"},
        }
    ]
    with pytest.raises(
        b.BaselineError, match="^INDEX_VECTORIZER_CREDENTIAL_UNOBSERVABLE$"
    ):
        b.index_projection(definition, "embedding", "https://ai.example")
    definition["vectorSearch"].pop("vectorizers")
    definition["fields"][-1]["retrievable"] = False
    with pytest.raises(b.BaselineError, match="^VECTORS_UNOBSERVABLE$"):
        b.index_projection(definition)


@pytest.mark.parametrize(
    "fault,error",
    [
        ("duplicate_owner", "AMBIGUOUS_UPLOAD_OWNER"),
        ("dynamic_name", "AMBIGUOUS_UPLOAD_OWNER"),
        ("overwrite", "AMBIGUOUS_UPLOAD_OWNER"),
        ("duplicate_environment", "AMBIGUOUS_REVIEW_ENVIRONMENT"),
        ("production_environment", "AMBIGUOUS_REVIEW_ENVIRONMENT"),
        ("local_action", "WORKFLOW_CLOSURE"),
        ("duplicate_key", "WORKFLOW_CONSTRUCTION"),
    ],
)
def test_semantic_construction_guards(prospective_route, monkeypatch, fault, error):
    import yaml

    h = prospective_route
    source_root = approval_proof.ROOT
    scratch = h["tmp_path"] / "workflows"
    for path in (source_root / ".github/workflows").glob("*.yml"):
        destination = scratch / ".github/workflows" / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(path.read_bytes())
    body_path = scratch / approval_proof.BODY
    body = yaml.safe_load(body_path.read_bytes())
    jobs = body["jobs"]
    uploader = next(
        step
        for step in jobs[approval_proof.OWNER_JOB]["steps"]
        if step.get("with", {}).get("name") == approval_proof.ARTIFACT
    )
    if fault == "duplicate_owner":
        jobs["duplicate"] = {"steps": [copy.deepcopy(uploader)]}
    elif fault == "dynamic_name":
        uploader["with"]["name"] = "${{ inputs.name }}"
    elif fault == "overwrite":
        uploader["with"]["overwrite"] = True
    elif fault == "duplicate_environment":
        jobs[approval_proof.OWNER_JOB]["environment"] = "demo"
    elif fault == "production_environment":
        jobs[approval_proof.PRODUCTION_JOB]["environment"] = "staging"
    elif fault == "local_action":
        jobs[approval_proof.OWNER_JOB]["steps"].append({"uses": "./action"})
    body_path.write_text(yaml.safe_dump(body))
    if fault == "duplicate_key":
        body_path.write_text(body_path.read_text() + "\njobs: {}\n")
    monkeypatch.setattr(approval_proof, "ROOT", scratch)
    with pytest.raises(b.BaselineError, match="^" + error + "$"):
        approval_proof.source_proof(h["api"], h["source"])


def test_immutable_source_bytes_guard(prospective_route, monkeypatch):
    h = prospective_route
    transport = h["transport"]

    def altered(url, *args, **kwargs):
        raw = transport(url, *args, **kwargs)
        if "/contents/" in url:
            response = json.loads(raw)
            response["content"] = base64.b64encode(
                base64.b64decode(response["content"]) + b"\n"
            ).decode()
            return b.canonical(response)
        return raw

    monkeypatch.setattr(b, "request_bytes", altered)
    with pytest.raises(b.BaselineError, match="^WORKFLOW_SOURCE$"):
        approval_proof.source_proof(h["api"], h["source"])


def delegated_grant_state(consent="Principal"):
    client = af.API_CLIENT
    state = observable_external(client)
    state["grants"] = [
        {
            "id": "fixture-grant",
            "clientId": af.API,
            "consentType": consent,
            "principalId": "12121212-1212-1212-1212-121212121212"
            if consent == "Principal"
            else None,
            "resourceId": "55555555-5555-5555-5555-555555555555",
            "scope": "User.Read",
        }
    ]
    return state


@pytest.mark.parametrize("consent", ["Principal", "AllPrincipals"])
def test_delegated_grant_observable_positive(consent):
    state = delegated_grant_state(consent)
    external_state.validate(state)
    assert (state["grants"][0]["principalId"] is None) == (consent == "AllPrincipals")


@pytest.mark.parametrize(
    "consent,field,error",
    [
        ("Principal", "resourceId", "EXTERNAL_GRANT_RESOURCE"),
        ("AllPrincipals", "resourceId", "EXTERNAL_GRANT_RESOURCE"),
        ("Principal", "principalId", "EXTERNAL_GRANT_PRINCIPAL"),
    ],
)
@pytest.mark.parametrize("fault", ["missing", None, "", "not-a-uuid", 7])
def test_delegated_grant_required_identity(consent, field, error, fault):
    state = delegated_grant_state(consent)
    external_state.validate(state)
    if fault == "missing":
        del state["grants"][0][field]
        error = "UNSUPPORTED_SCHEMA"
    else:
        state["grants"][0][field] = fault
    with pytest.raises(b.BaselineError, match="^" + error + "$"):
        external_state.validate(state)


@pytest.mark.parametrize("consent", ["Principal", "AllPrincipals"])
def test_actual_capture_observable_delegated_grant(prospective_route, consent):
    h = prospective_route
    h["plane"]["external_state"]["grants"] = delegated_grant_state(consent)["grants"]
    captured = b.AzureCapture(b.Deadline(2400, lambda: 0)).capture(
        h["targets"]["staging"]
    )
    assert (
        captured["external_state"]["grants"] == h["plane"]["external_state"]["grants"]
    )
    assert any("/oauth2PermissionGrants?" in url for url in h["events"])
    assert all(args[:3] != ["az", "containerapp", "update"] for args in h["commands"])


@pytest.mark.parametrize(
    "consent,field,error",
    [
        ("Principal", "resourceId", "EXTERNAL_GRANT_RESOURCE"),
        ("AllPrincipals", "resourceId", "EXTERNAL_GRANT_RESOURCE"),
        ("Principal", "principalId", "EXTERNAL_GRANT_PRINCIPAL"),
    ],
)
def test_actual_capture_incomplete_delegated_grant(
    prospective_route, consent, field, error
):
    h = prospective_route
    h["plane"]["external_state"]["grants"] = delegated_grant_state(consent)["grants"]
    h["plane"]["external_state"]["grants"][0][field] = None
    with pytest.raises(b.BaselineError, match="^" + error + "$"):
        b.AzureCapture(b.Deadline(2400, lambda: 0)).capture(h["targets"]["staging"])
    assert any("/oauth2PermissionGrants?" in url for url in h["events"])
    assert all(args[:3] != ["az", "containerapp", "update"] for args in h["commands"])


@pytest.mark.parametrize(
    "key,error",
    [
        ("grants", "EXTERNAL_GRANT_BINDING"),
        ("assignments", "EXTERNAL_ASSIGNMENT_BINDING"),
    ],
)
def test_external_cross_binding_guards(key, error):
    client, foreign = (
        af.API,
        "4" * 8 + "-4444-4444-4444-" + "4" * 12,
    )
    state = observable_external(client)
    if key == "grants":
        state[key] = [
            {
                "id": "grant",
                "clientId": client,
                "consentType": "AllPrincipals",
                "principalId": None,
                "resourceId": foreign,
                "scope": "User.Read",
            }
        ]
        field = "clientId"
    else:
        state[key] = [
            {
                "id": "assignment",
                "principalId": foreign,
                "resourceId": client,
                "appRoleId": foreign,
                "principalType": "User",
            }
        ]
        field = "resourceId"
    external_state.validate(state)
    state[key][0][field] = foreign
    with pytest.raises(b.BaselineError, match="^" + error + "$"):
        external_state.validate(state)


@pytest.mark.parametrize(
    "fault,error",
    [
        ("deployment", "INDEX_VECTORIZER_BINDING"),
        ("endpoint", "INDEX_VECTORIZER_BINDING"),
        ("key", "INDEX_VECTORIZER_CREDENTIAL_UNOBSERVABLE"),
        ("authority", "INDEX_VECTORIZER_AUTHORITY_UNOBSERVABLE"),
    ],
)
def test_vectorizer_identity_and_authority_guards(fault, error):
    definition = safe_index()
    item = {
        "name": "vectorizer",
        "kind": "azureOpenAI",
        "azureOpenAIParameters": {
            "deploymentId": "embedding",
            "resourceUri": "https://ai.example",
        },
    }
    definition["vectorSearch"]["vectorizers"] = [item]
    definition["vectorSearch"]["profiles"][0]["vectorizer"] = "vectorizer"
    assert b.index_projection(definition, "embedding", "https://ai.example")[
        "vectorSearch"
    ]["vectorizers"] == [item]
    parameters = item["azureOpenAIParameters"]
    if fault == "deployment":
        parameters["deploymentId"] = "other"
    elif fault == "endpoint":
        parameters["resourceUri"] = "https://other.example"
    elif fault == "key":
        parameters["apiKey"] = "canary"
    else:
        parameters["authIdentity"] = {"userAssignedIdentity": "canary"}
    with pytest.raises(b.BaselineError, match="^" + error + "$") as raised:
        b.index_projection(definition, "embedding", "https://ai.example")
    assert "canary" not in str(raised.value)


def capture_authority_route(h, plane="staging"):
    return b.AzureCapture(b.Deadline(2400, lambda: 0)).capture(h["targets"][plane])


def assert_no_release_work(h):
    assert all(
        args[:3] not in (["az", "rest", "--method"], ["az", "containerapp", "update"])
        for args in h["commands"]
    )


@pytest.mark.parametrize("plane", ["staging", "production"])
def test_authority_actual_complete_positive(prospective_route, plane):
    h = prospective_route
    captured = capture_authority_route(h, plane)
    observed = captured["authority"]
    assert observed == h["plane"]["authority"]
    assert observed["tickets"]["id"] == h["targets"][plane]["tickets_resource_id"]
    assert {
        observed["api"]["id"],
        observed["workload"]["principal"]["id"],
        observed["searchIdentity"]["id"],
    } == {af.API, af.WORKLOAD, af.SEARCH}
    assert len(observed["arm"]["effective"]) == 5
    assert len(observed["graph"]["outgoing"]) == 3
    assert all(not item["assignments"] for item in observed["graph"]["outgoing"])
    for kind in (
        "roleAssignments",
        "denyAssignments",
        "roleAssignmentScheduleInstances",
        "roleEligibilityScheduleInstances",
        "federatedIdentityCredentials",
        "transitiveMemberOf",
        "appRoleAssignments",
        "registrationAssignments",
    ):
        assert any("/" + kind + "?" in url for url in h["events"])
    assert_no_release_work(h)


@pytest.mark.parametrize("plane", ["staging", "production"])
def test_authority_shared_application_federation_positive(prospective_route, plane):
    h = prospective_route
    credentials = h["responses"][
        "/v1.0/applications/" + af.API_OBJECT + "/federatedIdentityCredentials"
    ]["value"]
    sibling = copy.deepcopy(credentials[0])
    sibling.update(
        id="12121212-1212-1212-1212-121212121212",
        name="sibling-workload",
        subject=af.GROUP,
    )
    credentials.append(sibling)
    captured = capture_authority_route(h, plane)
    assert len(captured["authority"]["federation"]) == 2
    assert {c["subject"] for c in captured["authority"]["federation"]} == {
        af.WORKLOAD,
        af.GROUP,
    }
    authority.validate(
        copy.deepcopy(captured["authority"]),
        captured["target"],
        captured["app"],
        captured["external_state"],
    )
    credentials.pop(0)
    with pytest.raises(b.BaselineError, match="^AUTHORITY_FEDERATION_BINDING$"):
        capture_authority_route(h, plane)
    assert_no_release_work(h)


@pytest.mark.parametrize("plane", ["staging", "production"])
def test_authority_tenant_role_definitions_positive(prospective_route, plane):
    h = prospective_route
    replacements = {}
    for path, response in list(h["responses"].items()):
        if "/roleDefinitions/" in path:
            root = (
                "/providers/Microsoft.Authorization/roleDefinitions/"
                + path.rsplit("/", 1)[1]
            )
            replacements[path] = root
            definition = copy.deepcopy(response)
            definition["id"] = root
            definition["properties"]["type"] = "BuiltInRole"
            definition["properties"]["assignableScopes"] = ["/"]
            h["responses"][root] = definition
    for path, response in h["responses"].items():
        if path.endswith("/roleAssignments"):
            for record in response["value"]:
                props = record["properties"]
                props["roleDefinitionId"] = replacements[props["roleDefinitionId"]]
    captured = capture_authority_route(h, plane)
    assert {role["id"] for role in captured["authority"]["arm"]["roles"]} == set(
        replacements.values()
    )
    assert len(captured["authority"]["arm"]["effective"]) == 5
    assert all(
        any(urllib.parse.urlsplit(url).path == root for url in h["events"])
        for root in replacements.values()
    )
    authority.validate(
        copy.deepcopy(captured["authority"]),
        captured["target"],
        captured["app"],
        captured["external_state"],
    )
    assert_no_release_work(h)


@pytest.mark.parametrize("kind", ["roleAssignments", "denyAssignments"])
def test_authority_root_assignment_is_not_a_supported_scope(kind):
    identity = "/providers/Microsoft.Authorization/" + kind + "/" + af.ROLE
    with pytest.raises(b.BaselineError, match="^AUTHORITY_ROLE_BINDING$"):
        authority.authorization_id(identity, kind)
    obs = authority.Observe(
        lambda *_: pytest.fail("root assignments must not reach transport"),
        b.Deadline(10, lambda: 0),
    )
    with pytest.raises(b.BaselineError, match="^AUTHORITY_ROUTE$"):
        obs.read(
            authority.ARM
            + "/providers/Microsoft.Authorization/"
            + kind
            + "?api-version=2022-04-01"
        )
    assert obs.calls == 0


@pytest.mark.parametrize(
    "fault,error",
    [
        ("endpoint", "TICKETS_ENDPOINT_BINDING"),
        ("storage_id", "AUTHORITY_RESOURCE_BINDING"),
        ("storage_type", "AUTHORITY_RESOURCE_BINDING"),
        ("storage_unknown", "UNSUPPORTED_SCHEMA"),
        ("shared_keys", "TICKETS_POLICY_UNSUPPORTED"),
        ("uami_client", "AUTHORITY_WORKLOAD_BINDING"),
        ("uami_principal", "AUTHORITY_WORKLOAD_BINDING"),
        ("uami_tenant", "AUTHORITY_WORKLOAD_BINDING"),
        ("workload_app", "AUTHORITY_WORKLOAD_BINDING"),
        ("federation_subject", "AUTHORITY_FEDERATION_BINDING"),
        ("federation_issuer", "AUTHORITY_FEDERATION_BINDING"),
        ("federation_audiences", "AUTHORITY_FEDERATION_BINDING"),
        ("federation_malformed_subject", "INVALID_IDENTITY"),
        ("federation_empty", "AUTHORITY_FEDERATION_BINDING"),
        ("search_tenant", "INDEX_VECTORIZER_AUTHORITY_UNOBSERVABLE"),
        ("search_missing", "UNSUPPORTED_SCHEMA"),
        ("role_condition", "AUTHORITY_CONDITION_UNSUPPORTED"),
        ("role_unknown", "UNSUPPORTED_SCHEMA"),
        ("pim", "AUTHORITY_PIM_UNSUPPORTED"),
        ("delegation", "AUTHORITY_DELEGATION_UNSUPPORTED"),
        ("role_permissions", "AUTHORITY_OPERATION_DENIED"),
        ("assignment_type", "AUTHORITY_PRINCIPAL_BINDING"),
    ],
)
def test_authority_actual_binding_rejection(prospective_route, fault, error):
    h = prospective_route
    responses, target = h["responses"], h["targets"]["staging"]
    tickets = responses[target["tickets_resource_id"]]
    uami = responses[h["plane"]["authority"]["workload"]["resourceId"]]
    federation = responses[
        "/v1.0/applications/" + af.API_OBJECT + "/federatedIdentityCredentials"
    ]
    assignment_path = next(
        path
        for path, page in responses.items()
        if path.endswith("/roleAssignments") and page["value"]
    )
    assignment = responses[assignment_path]["value"][0]
    if fault == "endpoint":
        tickets["properties"]["primaryEndpoints"]["table"] = (
            "https://otheraccount.table.core.windows.net/"
        )
    elif fault == "storage_id":
        tickets["id"] += "other"
    elif fault == "storage_type":
        tickets["type"] = "Microsoft.Search/searchServices"
    elif fault == "storage_unknown":
        tickets["properties"]["unknownAuthorization"] = True
    elif fault == "shared_keys":
        tickets["properties"]["allowSharedKeyAccess"] = True
    elif fault.startswith("uami_"):
        key = {
            "uami_client": "clientId",
            "uami_principal": "principalId",
            "uami_tenant": "tenantId",
        }[fault]
        uami["properties"][key] = af.RESOURCE
    elif fault == "workload_app":
        responses["/v1.0/servicePrincipals/" + af.WORKLOAD]["appId"] = af.API_CLIENT
    elif fault == "federation_subject":
        federation["value"][0]["subject"] = af.SEARCH
    elif fault == "federation_issuer":
        federation["value"][0]["issuer"] = (
            "https://login.microsoftonline.com/" + af.SEARCH + "/v2.0"
        )
    elif fault == "federation_audiences":
        federation["value"][0]["audiences"] = ["unsupported-audience"]
    elif fault == "federation_malformed_subject":
        federation["value"][0]["subject"] = "not-a-uuid"
    elif fault == "federation_empty":
        federation["value"] = []
    elif fault == "search_tenant":
        responses[target["search_resource_id"]]["identity"]["tenantId"] = af.RESOURCE
    elif fault == "search_missing":
        del responses[target["search_resource_id"]]["identity"]
    elif fault == "role_condition":
        assignment["properties"]["condition"] = "canary"
    elif fault == "role_unknown":
        assignment["properties"]["unknownAuthority"] = True
    elif fault == "assignment_type":
        assignment["properties"]["principalType"] = "Group"
    elif fault == "role_permissions":
        for path, response in responses.items():
            if "/roleDefinitions/" in path:
                response["properties"]["permissions"][0]["dataActions"] = []
    else:
        suffix = (
            "/roleAssignmentScheduleInstances"
            if fault == "pim"
            else "/registrationAssignments"
        )
        path = next(p for p in responses if p.endswith(suffix))
        responses[path]["value"] = [{"id": "unsupported"}]
    with pytest.raises(b.BaselineError, match="^" + error + "$") as raised:
        capture_authority_route(h)
    assert "canary" not in str(raised.value)
    assert_no_release_work(h)


@pytest.mark.parametrize("mode", ["ManagedIdentity", "ClientSecret", "Certificate"])
def test_authority_incompatible_credential_mode(prospective_route, mode):
    h = prospective_route
    target = h["targets"]["staging"]
    configured = h["responses"][target["app_resource_id"]]["properties"]["template"][
        "containers"
    ][0]["env"]
    next(
        item
        for item in configured
        if item["name"] == "AzureAd__ClientCredentials__0__SourceType"
    )["value"] = mode
    with pytest.raises(b.BaselineError, match="^CONFIG_UNOBSERVABLE$"):
        capture_authority_route(h)
    assert not any("/federatedIdentityCredentials?" in url for url in h["events"])
    app = copy.deepcopy(h["plane"]["app"])
    app["effective_environment"]["AzureAd__ClientCredentials__0__SourceType"] = mode
    obs = authority.Observe(
        lambda *_: pytest.fail("incompatible mode must precede authority reads"),
        b.Deadline(10, lambda: 0),
    )
    with pytest.raises(b.BaselineError, match="^AUTHORITY_WORKLOAD_BINDING$"):
        authority.capture(
            obs, obs.deadline, target, app, {}, h["plane"]["external_state"]
        )
    assert obs.calls == 0
    assert_no_release_work(h)


@pytest.mark.parametrize("kind", ["roleAssignments", "denyAssignments"])
def test_authority_arm_record_metadata_is_explicitly_unsupported(
    prospective_route, kind
):
    h = prospective_route
    if kind == "denyAssignments":
        install_deny(h, excluded=True)
    path = next(
        path
        for path, response in h["responses"].items()
        if path.endswith("/" + kind) and response["value"]
    )
    h["responses"][path]["value"][0]["systemData"] = {
        "createdAt": "2020-01-01T00:00:00Z"
    }
    with pytest.raises(b.BaselineError, match="^UNSUPPORTED_SCHEMA$"):
        capture_authority_route(h)
    assert_no_release_work(h)


@pytest.mark.parametrize(
    "field",
    ["principalId", "resourceId", "appRoleId", "disabled", "member", "accountEnabled"],
)
def test_authority_outgoing_binding_rejection(prospective_route, field):
    h = prospective_route
    record = {
        "id": "outgoing",
        "principalId": af.API,
        "resourceId": af.RESOURCE,
        "appRoleId": af.ROLE,
    }
    h["responses"]["/v1.0/servicePrincipals/" + af.API + "/appRoleAssignments"][
        "value"
    ] = [record]
    h["responses"]["/v1.0/servicePrincipals/" + af.GROUP] = af.principal(
        af.GROUP,
        "12121212-1212-1212-1212-121212121212",
        h["targets"]["staging"]["tenant_id"],
        "Application",
    )
    assert (
        capture_authority_route(h)["authority"]["graph"]["resources"][0]["id"]
        == af.RESOURCE
    )
    if field in record:
        record[field] = af.GROUP
    elif field == "disabled":
        h["responses"]["/v1.0/servicePrincipals/" + af.RESOURCE]["appRoles"][0][
            "isEnabled"
        ] = False
    elif field == "member":
        h["responses"]["/v1.0/servicePrincipals/" + af.RESOURCE]["appRoles"][0][
            "allowedMemberTypes"
        ] = ["User"]
    else:
        h["responses"]["/v1.0/servicePrincipals/" + af.RESOURCE]["accountEnabled"] = (
            False
        )
    with pytest.raises(
        b.BaselineError, match="^AUTHORITY_(OUTGOING|PRINCIPAL)_BINDING$"
    ):
        capture_authority_route(h)
    assert_no_release_work(h)


def move_workload_assignments(h, parent, group=False):
    responses, observed = h["responses"], h["plane"]["authority"]
    replacements = {}
    for assignment in observed["arm"]["assignments"]:
        if assignment["principalId"] == af.WORKLOAD:
            item = copy.deepcopy(assignment)
            item["scope"] = parent
            item["id"] = (
                parent
                + "/providers/Microsoft.Authorization/roleAssignments/"
                + assignment["id"].rsplit("/", 1)[1]
            )
            if group:
                item["principalId"], item["principalType"] = af.GROUP, "Group"
            replacements[assignment["id"]] = item
    assignments = [replacements.get(a["id"], a) for a in observed["arm"]["assignments"]]
    for path, response in responses.items():
        if path.endswith("/roleAssignments"):
            scope = path.rsplit("/providers/", 1)[0]
            response["value"] = [
                {
                    "id": a["id"],
                    "name": a["id"].rsplit("/", 1)[1],
                    "type": "Microsoft.Authorization/roleAssignments",
                    "properties": {k: v for k, v in a.items() if k != "id"},
                }
                for a in assignments
                if authority.inherited(a["scope"], scope, observed["scopes"])
            ]
    if group:
        responses["/v1.0/servicePrincipals/" + af.WORKLOAD + "/transitiveMemberOf"][
            "value"
        ] = [{"id": af.GROUP, "@odata.type": "#microsoft.graph.group"}]
        responses["/v1.0/groups/" + af.GROUP] = {
            "id": af.GROUP,
            "securityEnabled": True,
            "groupTypes": [],
            "membershipRule": None,
            "membershipRuleProcessingState": None,
        }


@pytest.mark.parametrize("group", [False, True])
def test_authority_inherited_and_transitive_positive(prospective_route, group):
    h = prospective_route
    parent = "/subscriptions/" + h["targets"]["staging"]["subscription_id"]
    move_workload_assignments(h, parent, group)
    observed = capture_authority_route(h)["authority"]
    assert any(a["scope"] == parent for a in observed["arm"]["assignments"])
    assert any(m["groups"] for m in observed["memberships"]) == group
    assert len(observed["arm"]["effective"]) == 5
    assert_no_release_work(h)


def install_deny(h, excluded=False, child_only=False):
    target = h["targets"]["staging"]
    scope = target["ai_resource_id"]
    props = {
        "scope": scope,
        "permissions": [
            {
                "actions": [],
                "notActions": [],
                "dataActions": ["*"],
                "notDataActions": [],
            }
        ],
        "principals": [{"id": authority.ZERO, "type": "SystemDefined"}],
        "excludePrincipals": [
            {"id": af.WORKLOAD, "type": "ServicePrincipal"},
            {"id": af.SEARCH, "type": "ServicePrincipal"},
        ]
        if excluded
        else [],
        "doNotApplyToChildScopes": child_only,
        "isSystemProtected": True,
    }
    identity = (
        scope
        + "/providers/Microsoft.Authorization/denyAssignments/eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    )
    record = {
        "id": identity,
        "name": identity.rsplit("/", 1)[1],
        "type": "Microsoft.Authorization/denyAssignments",
        "properties": props,
    }
    for path, response in h["responses"].items():
        if path.endswith("/denyAssignments") and authority.inherited(
            scope, path.rsplit("/providers/", 1)[0], h["plane"]["authority"]["scopes"]
        ):
            response["value"] = [copy.deepcopy(record)]


@pytest.mark.parametrize(
    "excluded,child_only", [(False, False), (True, False), (False, True)]
)
def test_authority_deny_exclusion_inheritance(prospective_route, excluded, child_only):
    h = prospective_route
    install_deny(h, excluded, child_only)
    if excluded or child_only:
        assert capture_authority_route(h)["authority"]["arm"]["denies"]
    else:
        with pytest.raises(b.BaselineError, match="^AUTHORITY_OPERATION_DENIED$"):
            capture_authority_route(h)
    assert_no_release_work(h)


@pytest.mark.parametrize(
    "fault,error",
    [
        ("role", "DEPENDENCY_DRIFT"),
        ("federation", "DEPENDENCY_DRIFT"),
        ("endpoint", "TICKETS_ENDPOINT_BINDING"),
        ("inherited", "DEPENDENCY_DRIFT"),
        ("group", "AUTHORITY_MEMBERSHIP_UNSUPPORTED"),
        ("membership", "AUTHORITY_OPERATION_DENIED"),
        ("deny_exclusion", "AUTHORITY_OPERATION_DENIED"),
        ("deny_inheritance", "DEPENDENCY_DRIFT"),
        ("outgoing", "DEPENDENCY_DRIFT"),
        ("role_meaning", "DEPENDENCY_DRIFT"),
        ("scope_meaning", "DEPENDENCY_DRIFT"),
        ("account_enabled", "AUTHORITY_PRINCIPAL_BINDING"),
    ],
)
def test_authority_actual_current_drift_before_release(prospective_route, fault, error):
    h = prospective_route
    target = h["targets"]["staging"]
    if fault in ("outgoing", "role_meaning", "account_enabled"):
        h["responses"]["/v1.0/servicePrincipals/" + af.API + "/appRoleAssignments"][
            "value"
        ] = [
            {
                "id": "role-one",
                "principalId": af.API,
                "resourceId": af.RESOURCE,
                "appRoleId": af.ROLE,
            }
        ]
    if fault == "scope_meaning":
        h["plane"]["external_state"]["grants"] = delegated_grant_state("AllPrincipals")[
            "grants"
        ]
    if fault in ("group", "membership"):
        move_workload_assignments(
            h, "/subscriptions/" + target["subscription_id"], True
        )
    if fault.startswith("deny_"):
        install_deny(h, excluded=True, child_only=False)
    expected = capture_authority_route(h)
    production = capture_authority_route(h, "production")
    bound = copy.deepcopy(h["values"][3])
    bound["planes"] = {"staging": expected, "production": production}
    if fault == "role":
        path = next(p for p in h["responses"] if "/roleDefinitions/" in p)
        h["responses"][path]["properties"]["permissions"][0]["dataActions"] = [
            "Microsoft.Search/*",
            "Microsoft.CognitiveServices/*",
            "Microsoft.Storage/*",
        ]
    elif fault == "federation":
        h["responses"][
            "/v1.0/applications/" + af.API_OBJECT + "/federatedIdentityCredentials"
        ]["value"][0]["name"] = "changed"
    elif fault == "endpoint":
        h["responses"][target["tickets_resource_id"]]["properties"]["primaryEndpoints"][
            "table"
        ] = "https://otheraccount.table.core.windows.net/"
    elif fault == "inherited":
        move_workload_assignments(h, "/subscriptions/" + target["subscription_id"])
    elif fault == "group":
        h["responses"]["/v1.0/groups/" + af.GROUP]["groupTypes"] = ["DynamicMembership"]
    elif fault == "membership":
        h["responses"][
            "/v1.0/servicePrincipals/" + af.WORKLOAD + "/transitiveMemberOf"
        ]["value"] = []
    elif fault.startswith("deny_"):
        for path, page in h["responses"].items():
            if path.endswith("/denyAssignments") and page["value"]:
                key = (
                    "excludePrincipals"
                    if fault == "deny_exclusion"
                    else "doNotApplyToChildScopes"
                )
                page["value"][0]["properties"][key] = (
                    [] if fault == "deny_exclusion" else True
                )
    elif fault == "outgoing":
        h["responses"]["/v1.0/servicePrincipals/" + af.API + "/appRoleAssignments"][
            "value"
        ][0]["id"] = "role-two"
    elif fault == "role_meaning":
        h["responses"]["/v1.0/servicePrincipals/" + af.RESOURCE]["appRoles"][0][
            "value"
        ] = "User.ReadBasic.All"
    elif fault == "account_enabled":
        h["responses"]["/v1.0/servicePrincipals/" + af.RESOURCE]["accountEnabled"] = (
            False
        )
    else:
        h["responses"]["/v1.0/servicePrincipals/" + af.RESOURCE][
            "oauth2PermissionScopes"
        ][0]["type"] = "Admin"
    h["document"]["release_evidence"]["candidate"] = copy.deepcopy(h["candidate"])
    h["values"][3]["planes"] = bound["planes"]
    h["values"][6]["candidate_sha256"] = b.digest(b.canonical(h["candidate"]))
    h["values"][6]["evaluation_sha256"] = b.digest(b.canonical(h["document"]))
    h["values"][6]["baseline_sha256"] = b.digest(b.canonical(bound))
    values = (*h["values"][:4], b.canonical(bound), *h["values"][5:])
    calls = []
    with pytest.raises(b.BaselineError, match="^" + error + "$"):
        execute(
            values,
            capture=lambda t: b.AzureCapture(b.Deadline(2400, lambda: 0)).capture(t),
            producer=lambda *args: calls.append("producer"),
            events=calls,
        )
    assert not calls
    assert_no_release_work(h)


@pytest.mark.parametrize(
    "boundary", ["after_result", "before_persistence", "after_persistence"]
)
def test_authority_current_drift_at_final_boundaries(prospective_route, boundary):
    h = prospective_route
    bound = copy.deepcopy(h["values"][3])
    bound["planes"] = {
        plane: capture_authority_route(h, plane) for plane in ("staging", "production")
    }
    h["document"]["release_evidence"]["candidate"] = copy.deepcopy(h["candidate"])
    h["values"][3]["planes"] = bound["planes"]
    h["values"][6]["candidate_sha256"] = b.digest(b.canonical(h["candidate"]))
    h["values"][6]["evaluation_sha256"] = b.digest(b.canonical(h["document"]))
    h["values"][6]["baseline_sha256"] = b.digest(b.canonical(bound))
    values = (*h["values"][:4], b.canonical(bound), *h["values"][5:])
    captures, events = [], []
    drift_call = {"after_result": 3, "before_persistence": 5, "after_persistence": 7}[
        boundary
    ]

    def capture(target):
        captures.append(target)
        if len(captures) == drift_call:
            credential = h["responses"][
                "/v1.0/applications/" + af.API_OBJECT + "/federatedIdentityCredentials"
            ]["value"][0]
            credential["name"] = "changed-federation"
        return b.AzureCapture(b.Deadline(2400, lambda: 0)).capture(target)

    with pytest.raises(b.BaselineError, match="^DEPENDENCY_DRIFT$"):
        execute(
            values,
            capture=capture,
            producer=lambda *_: pytest.fail("CURRENT must not refresh"),
            events=events,
        )
    assert len(captures) == drift_call
    assert events == (["persist"] if boundary == "after_persistence" else [])
    assert_no_release_work(h)


@pytest.mark.parametrize("missing", ["tickets_resource_id", "authority"])
def test_authority_mandatory_schema_has_no_legacy_branch(
    tmp_path, monkeypatch, missing
):
    values = unit(tmp_path, monkeypatch)
    plane = values[3]["planes"]["staging"]
    del (plane["target"] if missing == "tickets_resource_id" else plane)[missing]
    with pytest.raises(b.BaselineError, match="^UNSUPPORTED_SCHEMA$"):
        b.validate_baseline(values[3])


def test_authority_actual_set_order_is_deterministic(prospective_route):
    h = prospective_route
    parent = "/subscriptions/" + h["targets"]["staging"]["subscription_id"]
    move_workload_assignments(h, parent, True)
    outgoing = h["responses"][
        "/v1.0/servicePrincipals/" + af.API + "/appRoleAssignments"
    ]
    outgoing["value"] = [
        {
            "id": identity,
            "principalId": af.API,
            "resourceId": af.RESOURCE,
            "appRoleId": af.ROLE,
        }
        for identity in ("role-one", "role-two")
    ]
    for path, response in h["responses"].items():
        if "/roleDefinitions/" in path:
            response["properties"]["permissions"][0]["dataActions"] = [
                "Microsoft.Search/*",
                "Microsoft.CognitiveServices/*",
                "Microsoft.Storage/*",
            ]
    first = capture_authority_route(h)
    assert any(
        len(response.get("value", [])) > 1 for response in h["responses"].values()
    )
    assert (
        len(
            next(
                item
                for item in first["authority"]["graph"]["outgoing"]
                if item["principalId"] == af.API
            )["assignments"]
        )
        == 2
    )
    for response in h["responses"].values():
        if isinstance(response.get("value"), list):
            response["value"].reverse()
        permissions = response.get("properties", {}).get("permissions", [])
        for permission in permissions:
            for patterns in permission.values():
                patterns.reverse()
    assert b.canonical(capture_authority_route(h)) == b.canonical(first)
    assert_no_release_work(h)


@pytest.mark.parametrize(
    "fault",
    [
        "loop",
        "foreign_host",
        "foreign_path",
        "version",
        "duplicate",
        "oversize",
        "deadline",
        "401",
        "403",
        "unknown",
    ],
)
def test_authority_bounded_observation(prospective_route, fault):
    url = (
        authority.GRAPH
        + "/servicePrincipals/"
        + af.API
        + "/appRoleAssignments?$top=100"
    )
    now, calls = [0], []
    deadline = b.Deadline(10, lambda: now[0])

    def get(current, method):
        calls.append(current)
        if fault in ("401", "403"):
            raise b.BaselineError(fault + " secret-canary")
        if fault == "deadline":
            now[0] = 11
        if fault == "oversize":
            return {"value": [], "padding": "x" * authority.MAX_BYTES}
        if fault == "duplicate":
            return {"value": [{"id": "same"}, {"id": "same"}]}
        if fault == "unknown":
            return {"value": [], "unknownAuthorization": True}
        link = {
            "loop": url,
            "foreign_host": url.replace("graph.microsoft.com", "evil.example"),
            "foreign_path": url.replace("appRoleAssignments", "appRoleAssignedTo"),
            "version": url.replace("v1.0", "beta"),
        }.get(fault)
        return {"value": [], "@odata.nextLink": link}

    with pytest.raises(b.BaselineError) as raised:
        authority.Observe(get, deadline).collection(url, True)
    assert "secret-canary" not in str(raised.value)
    assert "secret-canary" not in "".join(traceback.format_exception(raised.value))
    if fault in ("401", "403"):
        assert str(raised.value) == "AUTHORITY_READ_DENIED"
        assert raised.value.__suppress_context__
    assert len(calls) == 1
    assert_no_release_work(prospective_route)


def test_authority_terminal_pagination_positive():
    url = (
        authority.GRAPH
        + "/servicePrincipals/"
        + af.API
        + "/appRoleAssignments?$top=100"
    )
    pages = [
        {"value": [{"id": "first"}], "@odata.nextLink": url + "&$skiptoken=next"},
        {"value": [{"id": "last"}]},
    ]
    obs = authority.Observe(lambda *args: pages.pop(0), b.Deadline(10, lambda: 0))
    assert obs.collection(url, True) == [{"id": "first"}, {"id": "last"}]
    assert not pages and obs.calls == 2


def install_authority_transport_fault(h, monkeypatch, fault):
    target = h["targets"]["staging"]
    requests, reads = [], []

    class Stream(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)

    class Socket:
        def makefile(self, *args, **kwargs):
            if fault == "malformed_status":
                return io.BytesIO(b"secret-canary\r\n")
            return io.BytesIO(
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                b"100\r\nsecret-canary"
            )

    class Response(http.client.HTTPResponse):
        def read(self, size=None):
            reads.append(size)
            return super().read(size)

    class Opener:
        def __init__(self, handler):
            self.handler = handler

        def open(self, request, timeout):
            requests.append(request)
            if fault == "redirect":
                return self.handler.redirect_request(
                    request,
                    None,
                    302,
                    "Found",
                    HTTPMessage(),
                    request.full_url + "&redirected=true",
                )
            if fault in ("401", "403"):
                raise urllib.error.HTTPError(
                    request.full_url,
                    int(fault),
                    "secret-canary",
                    HTTPMessage(),
                    io.BytesIO(b"secret-canary"),
                )
            if fault in ("truncated_chunk", "malformed_status"):
                response = Response(Socket())
                response.begin()
                return response
            assert fault == "raw_overflow"
            return Stream(b"x" * (authority.MAX_BYTES + 1))

    monkeypatch.setattr(urllib.request, "build_opener", lambda handler: Opener(handler))

    def transport(url, headers, deadline, method="GET", body=None, limit=b.PAGE_BYTES):
        if urllib.parse.urlsplit(url).path == target["tickets_resource_id"]:
            assert limit == authority.MAX_BYTES and method == "GET" and body is None
            return h["real_request_bytes"](
                url, headers, deadline, limit=limit, method=method, body=body
            )
        return h["transport"](
            url, headers, deadline, method=method, body=body, limit=limit
        )

    monkeypatch.setattr(b, "request_bytes", transport)
    return requests, reads


@pytest.mark.parametrize(
    "fault",
    ["redirect", "raw_overflow", "401", "403", "truncated_chunk", "malformed_status"],
)
def test_authority_real_canned_transport_boundary(
    prospective_route, monkeypatch, fault
):
    h = prospective_route
    requests, reads = install_authority_transport_fault(h, monkeypatch, fault)
    code = {
        "redirect": "AUTHORITY_ROUTE",
        "raw_overflow": "RESPONSE_BYTES",
    }.get(fault, "AUTHORITY_READ_DENIED")
    with pytest.raises(b.BaselineError, match="^" + code + "$") as raised:
        capture_authority_route(h)
    rendered = "".join(traceback.format_exception(raised.value))
    assert "secret-canary" not in rendered
    if code == "AUTHORITY_READ_DENIED":
        assert raised.value.__suppress_context__
        assert "REMOTE_UNAVAILABLE" not in rendered
        assert "HTTPError" not in rendered
        assert "IncompleteRead" not in rendered
        assert "BadStatusLine" not in rendered
    assert len(requests) == 1
    assert urllib.parse.urlsplit(requests[0].full_url).netloc == "management.azure.com"
    if fault == "raw_overflow":
        assert sum(reads) == authority.MAX_BYTES + 1 and reads[-1] == 1
    elif fault == "truncated_chunk":
        assert reads == [65536]
    else:
        assert not reads
    assert_no_release_work(h)


@pytest.mark.parametrize("fault", ["401", "403", "truncated_chunk", "malformed_status"])
def test_authority_transport_cli_rejection(
    prospective_route, monkeypatch, capsys, fault
):
    h = prospective_route
    requests, _ = install_authority_transport_fault(h, monkeypatch, fault)
    envelope = {
        key: h["values"][6][key]
        for key in (
            "source_commit",
            "image",
            "component_status",
            "staging_verification",
        )
    }
    paths = [
        h["tmp_path"] / name
        for name in ("input.json", "candidate.json", "evaluation.json", "targets.json")
    ]
    for path, value in zip(
        paths, (envelope, h["candidate"], h["document"], h["targets"]), strict=True
    ):
        path.write_bytes(b.canonical(value))
    originals = [path.read_bytes() for path in paths]
    output = h["tmp_path"] / "approved"
    assert (
        promote.main(
            [
                "prepare",
                "--envelope",
                str(paths[0]),
                "--candidate",
                str(paths[1]),
                "--evaluation",
                str(paths[2]),
                "--targets",
                str(paths[3]),
                "--claim",
                json.dumps(CLAIM),
                "--components",
                "app",
                "--output",
                str(output),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "release promotion: rejected AUTHORITY_READ_DENIED; "
        "prepare a new observable same-run bundle\n"
    )
    assert "secret-canary" not in captured.err and "Traceback" not in captured.err
    assert not output.exists()
    assert [path.read_bytes() for path in paths] == originals
    assert len(requests) == 1
    assert_no_release_work(h)


@pytest.mark.parametrize("fault", ["truncated_chunk", "malformed_status"])
def test_authority_finite_http_guard_control(prospective_route, monkeypatch, fault):
    import ast
    import inspect

    h = prospective_route
    original = h["real_request_bytes"]
    tree = ast.parse(inspect.getsource(original))
    handlers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
        and isinstance(node.type, ast.Tuple)
        and any(
            ast.unparse(item) == "http.client.HTTPException" for item in node.type.elts
        )
    ]
    assert len(handlers) == 1
    handler = handlers[0]
    removed = [
        item
        for item in handler.type.elts
        if ast.unparse(item) == "http.client.HTTPException"
    ]
    assert len(removed) == 1
    handler.type.elts.remove(removed[0])
    compiled = compile(ast.fix_missing_locations(tree), "<finite-http-control>", "exec")
    functions = [
        item
        for item in compiled.co_consts
        if isinstance(item, types.CodeType) and item.co_name == "request_bytes"
    ]
    assert len(functions) == 1
    mutant = types.FunctionType(
        functions[0], dict(original.__globals__), "request_bytes"
    )
    mutant.__defaults__ = original.__defaults__
    requests, _ = install_authority_transport_fault(h, monkeypatch, fault)

    def rejected():
        with pytest.raises(b.BaselineError, match="^AUTHORITY_READ_DENIED$") as raised:
            capture_authority_route(h)
        assert "secret-canary" not in "".join(traceback.format_exception(raised.value))
        assert raised.value.__suppress_context__

    rejected()
    h["real_request_bytes"] = mutant
    expected = (
        http.client.IncompleteRead
        if fault == "truncated_chunk"
        else http.client.BadStatusLine
    )
    with pytest.raises(expected) as escaped:
        rejected()
    rendered = "".join(traceback.format_exception(escaped.value))
    assert not isinstance(escaped.value, b.BaselineError)
    if fault == "malformed_status":
        assert "secret-canary" in rendered
        with pytest.raises(AssertionError):
            assert "secret-canary" not in rendered
    assert len(requests) == 2
    assert_no_release_work(h)


@pytest.mark.parametrize(
    "fault,code",
    [
        ("depth", "AUTHORITY_BYTES"),
        ("container", "AUTHORITY_BYTES"),
        ("keys", "AUTHORITY_BYTES"),
        ("integer", "AUTHORITY_BYTES"),
        ("nan", "AUTHORITY_SCHEMA"),
        ("infinity", "AUTHORITY_SCHEMA"),
        ("encoding", "AUTHORITY_SCHEMA"),
    ],
)
def test_authority_parsed_shape_bounds(fault, code):
    value = {"ok": True}
    if fault == "depth":
        for _ in range(33):
            value = {"nested": value}
    elif fault == "container":
        value = {"items": [None] * 10001}
    elif fault == "keys":
        value = {1: True}
    elif fault == "integer":
        value = {"integer": 1 << 4096}
    elif fault in ("nan", "infinity"):
        value = {"float": float("nan" if fault == "nan" else "inf")}
    else:
        value = {"string": chr(0xD800)}
    url = authority.GRAPH + "/servicePrincipals/" + af.API
    obs = authority.Observe(lambda *_: value, b.Deadline(10, lambda: 0))
    with pytest.raises(b.BaselineError, match="^" + code + "$"):
        obs.read(url)
    assert obs.calls == 1


def test_authority_aggregate_and_call_bounds():
    url = authority.GRAPH + "/servicePrincipals/" + af.API
    response = {"padding": "x" * (authority.MAX_BYTES // 6 - 100)}
    calls = []
    obs = authority.Observe(
        lambda *_: calls.append(1) or response, b.Deadline(10, lambda: 0)
    )
    size = authority.response_size(response)
    assert size < authority.MAX_BYTES
    accepted = authority.MAX_TOTAL // size
    for _ in range(accepted):
        obs.read(url)
    with pytest.raises(b.BaselineError, match="^AUTHORITY_BYTES$"):
        obs.read(url)
    assert len(calls) == accepted + 1 and obs.total > authority.MAX_TOTAL
    calls.clear()
    obs = authority.Observe(lambda *_: calls.append(1) or {}, b.Deadline(10, lambda: 0))
    for _ in range(800):
        obs.read(url)
    with pytest.raises(b.BaselineError, match="^AUTHORITY_CALL_BOUND$"):
        obs.read(url)
    assert len(calls) == 800


@pytest.mark.parametrize(
    "fault,expected_calls", [("pages", 11), ("page_items", 1), ("total_items", 11)]
)
def test_authority_collection_work_bounds(fault, expected_calls):
    url = (
        authority.GRAPH
        + "/servicePrincipals/"
        + af.API
        + "/appRoleAssignments?$top=100"
    )
    calls = []

    def get(current, method):
        number = len(calls)
        calls.append(current)
        items = 101 if fault == "page_items" else 0 if fault == "pages" else 100
        values = [{"id": str(number * 1000 + i)} for i in range(items)]
        return {
            "value": values,
            "@odata.nextLink": url + "&$skiptoken=" + str(number + 1),
        }

    obs = authority.Observe(get, b.Deadline(10, lambda: 0))
    with pytest.raises(b.BaselineError, match="^AUTHORITY_PAGINATION$"):
        obs.collection(url, True)
    assert len(calls) == expected_calls


@pytest.mark.parametrize(
    "fault", ["method", "repeated", "unknown_query", "arm_version", "entities_method"]
)
def test_authority_route_rejected_before_transport(fault):
    url, method = (
        authority.GRAPH + "/servicePrincipals/" + af.API + "?$select=id",
        "GET",
    )
    if fault == "method":
        method = "POST"
    elif fault == "repeated":
        url += "&$select=appId"
    elif fault == "unknown_query":
        url += "&unsupported=true"
    elif fault == "arm_version":
        url = (
            authority.ARM + "/subscriptions/" + af.WORKLOAD + "?api-version=2020-05-01"
        )
    else:
        url = (
            authority.ARM
            + "/providers/Microsoft.Management/getEntities?api-version=2020-05-01"
        )
    obs = authority.Observe(
        lambda *_: pytest.fail("invalid route reached transport"),
        b.Deadline(10, lambda: 0),
    )
    with pytest.raises(b.BaselineError, match="^AUTHORITY_ROUTE$"):
        obs.read(url, method)
    assert obs.calls == 0


@pytest.mark.parametrize("guard", ["endpoint", "federation", "outgoing", "effective"])
def test_authority_singleton_guard_control(prospective_route, monkeypatch, guard):
    import ast
    import inspect

    h = prospective_route
    if guard == "outgoing":
        h["responses"]["/v1.0/servicePrincipals/" + af.API + "/appRoleAssignments"][
            "value"
        ] = [
            {
                "id": "outgoing",
                "principalId": af.API,
                "resourceId": af.RESOURCE,
                "appRoleId": af.ROLE,
            }
        ]
    captured = capture_authority_route(h)
    value = captured["authority"]
    function = (
        "validate"
        if guard == "endpoint"
        else "validate_details"
        if guard in ("federation", "outgoing")
        else "effective"
    )
    code = {
        "endpoint": "TICKETS_ENDPOINT_BINDING",
        "federation": "AUTHORITY_FEDERATION_BINDING",
        "outgoing": "AUTHORITY_OUTGOING_BINDING",
        "effective": "AUTHORITY_OPERATION_DENIED",
    }[guard]
    if guard == "endpoint":
        value["tickets"]["endpoint"] = "https://otheraccount.table.core.windows.net/"
    elif guard == "federation":
        value["federation"][0]["subject"] = af.SEARCH
    elif guard == "outgoing":
        next(o for o in value["graph"]["outgoing"] if o["principalId"] == af.API)[
            "assignments"
        ][0]["principalId"] = af.SEARCH
    else:
        value["arm"]["assignments"] = []
        value["arm"]["roles"] = []
        for inventory in value["arm"]["inventories"]:
            inventory["ids"] = []
        value["arm"]["effective"] = authority.ordered(
            [
                op | {"assignments": [], "denies": []}
                for op in authority.required_operations(
                    captured["target"], af.WORKLOAD, af.SEARCH
                )
            ]
        )

    def rejected():
        with pytest.raises(b.BaselineError, match="^" + code + "$"):
            authority.validate(
                value, captured["target"], captured["app"], captured["external_state"]
            )

    rejected()
    tree = ast.parse(inspect.getsource(getattr(authority, function)))
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "fail"
            and child.args
            and isinstance(child.args[0], ast.Constant)
            and child.args[0].value == code
            for stmt in node.body
            for child in ast.walk(stmt)
        )
    ]
    if guard == "outgoing":
        selected = next(
            node
            for node in matches
            if "assignment['principalId']" in ast.unparse(node.test)
        )
    elif guard == "federation":
        predicates = [
            node
            for node in matches
            if "c['subject']" in ast.unparse(node.test)
            and "workload['id']" in ast.unparse(node.test)
        ]
        assert len(predicates) == 1
        selected = predicates[0]
    else:
        selected = matches[0]
    selected.body = [ast.Pass()]
    namespace = dict(vars(authority))
    compiled = compile(
        ast.fix_missing_locations(tree), "<singleton-authority-control>", "exec"
    )
    functions = [
        constant
        for constant in compiled.co_consts
        if isinstance(constant, types.CodeType) and constant.co_name == function
    ]
    assert len(functions) == 1
    mutant = types.FunctionType(functions[0], namespace, function)
    monkeypatch.setattr(authority, function, mutant)
    with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
        rejected()
    assert_no_release_work(h)

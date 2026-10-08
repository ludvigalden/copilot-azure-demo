"""Bounded, redacted same-run dependency snapshots and artifact verification."""

from __future__ import annotations

import hashlib
import http.client
import io
import ipaddress
import json
import math
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import IO, Any, Protocol

PAGE_BYTES = 32 * 1024 * 1024
TOTAL_BYTES = 256 * 1024 * 1024
MAX_DOCUMENTS = 10_000
MAX_PAGES = 11


class BaselineError(ValueError):
    """Stable redacted failure; remote bodies and credentials are never reported."""


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def object_json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
        )
        if not isinstance(value, dict):
            raise TypeError()
        return value
    except (ValueError, TypeError, UnicodeError):
        raise BaselineError("INVALID_JSON") from None


def text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise BaselineError("MISSING_IDENTITY")
    return value


def sha(value: Any, length: int = 64) -> str:
    if not isinstance(value, str) or not re.fullmatch(f"[0-9a-f]{{{length}}}", value):
        raise BaselineError("INVALID_DIGEST")
    return value


@dataclass
class Deadline:
    end: float
    clock: Callable[[], float] = time.monotonic

    def remaining(self, cap: float = 60) -> float:
        remaining = self.end - self.clock()
        if remaining <= 0:
            raise BaselineError("DEADLINE")
        return min(cap, remaining)


def bounded_read(stream: IO[bytes], limit: int, deadline: Deadline) -> bytes:
    """Read no more than the permitted bytes plus one overflow sentinel."""
    chunks: list[bytes] = []
    size = 0
    while True:
        deadline.remaining()
        chunk = stream.read(min(65536, limit + 1 - size))
        deadline.remaining()
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            raise BaselineError("RESPONSE_BYTES")
        chunks.append(chunk)


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if (
            urllib.parse.urlsplit(req.full_url).netloc
            in ("management.azure.com", "graph.microsoft.com")
            and newurl != req.full_url
        ):
            raise BaselineError("AUTHORITY_ROUTE")
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise BaselineError("INSECURE_REDIRECT")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if (
            redirected is not None
            and urllib.parse.urlsplit(req.full_url).netloc
            != urllib.parse.urlsplit(newurl).netloc
        ):
            redirected.remove_header("Authorization")
            redirected.remove_header("Api-key")
        return redirected


def request_bytes(
    url: str,
    headers: dict[str, str],
    deadline: Deadline,
    limit: int = PAGE_BYTES,
    method: str = "GET",
    body: bytes | None = None,
) -> bytes:
    try:
        request = urllib.request.Request(url, headers=headers, method=method, data=body)
        with urllib.request.build_opener(SafeRedirect()).open(
            request, timeout=deadline.remaining()
        ) as response:
            return bounded_read(response, limit, deadline)
    except BaselineError:
        raise
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
        raise BaselineError("REMOTE_UNAVAILABLE") from None


class VerificationAPI(Protocol):
    def get(self, path: str) -> dict[str, Any]: ...
    def listing(self, path: str) -> list[dict[str, Any]]: ...
    def archive(self, artifact_id: int) -> bytes: ...


@dataclass
class GitHubAPI:
    repository: str
    token: str
    deadline: Deadline

    def get(self, path: str) -> dict[str, Any]:
        return object_json(
            request_bytes(
                f"https://api.github.com/repos/{self.repository}/{path}",
                {
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                self.deadline,
            )
        )

    def listing(self, path: str) -> list[dict[str, Any]]:
        raw = request_bytes(
            f"https://api.github.com/repos/{self.repository}/{path}?per_page=100",
            {
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
            },
            self.deadline,
        )
        try:
            result = json.loads(raw)
            if (
                not isinstance(result, list)
                or len(result) >= 100
                or any(not isinstance(item, dict) for item in result)
            ):
                raise ValueError()
            return result
        except (ValueError, TypeError):
            raise BaselineError("PROVENANCE_LIST_UNAVAILABLE") from None

    def archive(self, artifact_id: int) -> bytes:
        return request_bytes(
            f"https://api.github.com/repos/{self.repository}/actions/artifacts/{artifact_id}/zip",
            {"Authorization": f"Bearer {self.token}"},
            self.deadline,
        )


def origin_time(value: Any) -> datetime:
    try:
        stamp = datetime.fromisoformat(text(value))
        if stamp.utcoffset() is None or stamp > datetime.now(UTC):
            raise ValueError()
        return stamp
    except (ValueError, TypeError):
        raise BaselineError("ORIGIN_TIMESTAMP") from None


def exact(value: Any, required: set[str], optional: set[str] | None = None) -> None:
    if (
        not isinstance(value, dict)
        or not required <= set(value)
        or set(value) - (required | (optional or set()))
    ):
        raise BaselineError("UNSUPPORTED_SCHEMA")


def identifier(value: Any) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", text(value)):
        raise BaselineError("INVALID_IDENTITY")
    return value


def uuid(value: Any) -> str:
    if not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text(value)
    ):
        raise BaselineError("INVALID_IDENTITY")
    return value


def resource_id(value: Any) -> str:
    if not re.fullmatch(
        r"/subscriptions/[0-9a-f-]{36}/resourceGroups/[a-zA-Z0-9_.-]+/providers/"
        r"[a-zA-Z0-9.]+/[a-zA-Z0-9]+/[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9]+/[a-zA-Z0-9_.-]+)*",
        text(value),
    ):
        raise BaselineError("RESOURCE_BINDING")
    uuid(value.split("/")[2])
    return value


def host(value: Any) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+", text(value)):
        raise BaselineError("ENDPOINT_BINDING")
    return value


def endpoint(value: Any) -> str:
    parsed = urllib.parse.urlsplit(text(value))
    if (
        parsed.scheme != "https"
        or parsed.path not in ("", "/")
        or (
            parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or parsed.port
        )
    ):
        raise BaselineError("ENDPOINT_BINDING")
    host(parsed.netloc)
    return value


def image_digest(value: Any) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9./_-]+@sha256:[0-9a-f]{64}", text(value)):
        raise BaselineError("APP_IMAGE")
    return value


def integer(value: Any, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise BaselineError("INVALID_NUMBER")


def boolean(value: Any) -> None:
    if type(value) is not bool:
        raise BaselineError("INVALID_BOOLEAN")


def validate_target(target: Any) -> None:
    exact(
        target,
        {
            "tenant_id",
            "subscription_id",
            "app_resource_id",
            "search_resource_id",
            "ai_resource_id",
            "tickets_resource_id",
            "index_name",
            "chat_deployment",
            "embedding_deployment",
        },
    )
    uuid(target["tenant_id"])
    subscription = uuid(target["subscription_id"])
    for key, kind in (
        ("app_resource_id", "Microsoft.App/containerApps"),
        ("search_resource_id", "Microsoft.Search/searchServices"),
        ("ai_resource_id", "Microsoft.CognitiveServices/accounts"),
        ("tickets_resource_id", "Microsoft.Storage/storageAccounts"),
    ):
        value = resource_id(target[key])
        if key == "tickets_resource_id":
            import authority_state

            authority_state.arm_id(value, kind, subscription)
        if (
            value.split("/")[2] != subscription
            or "/providers/" + kind + "/" not in value
        ):
            raise BaselineError("RESOURCE_BINDING")
    for key in ("index_name", "chat_deployment", "embedding_deployment"):
        identifier(target[key])


def validate_model_name(model: Any) -> None:
    exact(model, {"name", "version", "format"})
    for value in model.values():
        identifier(value)
    if model["format"] != "OpenAI":
        raise BaselineError("MODEL_CONFIGURATION_UNOBSERVABLE")


def validate_candidate(candidate: Any) -> None:
    exact(
        candidate,
        {
            "staging_prefix",
            "shared_prefix",
            "source_commit",
            "kb_tree",
            "kb_source_commit",
            "image_source_commit",
            "image",
            "revision",
            "index_name",
            "index_snapshot_sha256",
            "index_definition_sha256",
            "document_count",
            "chat_deployment",
            "embedding_deployment",
            "chat_model",
            "embedding_model",
            "answer_prompt_sha256",
            "judge_prompt_sha256",
            "judge_host",
            "judge_api_version",
            "dataset_sha256",
            "run_id",
            "endpoint",
            "components",
        },
    )
    for key in ("source_commit", "kb_tree", "kb_source_commit", "image_source_commit"):
        sha(candidate[key], 40)
    for key in (
        "index_snapshot_sha256",
        "index_definition_sha256",
        "answer_prompt_sha256",
        "judge_prompt_sha256",
        "dataset_sha256",
    ):
        sha(candidate[key])
    for key in (
        "staging_prefix",
        "shared_prefix",
        "revision",
        "index_name",
        "chat_deployment",
        "embedding_deployment",
        "judge_api_version",
    ):
        identifier(candidate[key])
    image_digest(candidate["image"])
    integer(candidate["document_count"], 1, MAX_DOCUMENTS)
    host(candidate["judge_host"])
    url = text(candidate["endpoint"])
    if not url.endswith("/api/answers"):
        raise BaselineError("ENDPOINT_BINDING")
    endpoint(url.removesuffix("/api/answers"))
    if not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", text(candidate["run_id"])):
        raise BaselineError("INVALID_IDENTITY")
    if candidate["components"] != ["app"]:
        raise BaselineError("ORIGINAL_COMPONENTS")
    for role in ("chat", "embedding"):
        validate_model_name(candidate[f"{role}_model"])


def validate_identity(identity: Any) -> None:
    exact(identity, {"type", "userAssignedIdentities"})
    if (
        identity["type"] != "UserAssigned"
        or not isinstance(identity["userAssignedIdentities"], dict)
        or not identity["userAssignedIdentities"]
    ):
        raise BaselineError("IDENTITY_UNOBSERVABLE")
    for resource, item in identity["userAssignedIdentities"].items():
        resource_id(resource)
        if "/Microsoft.ManagedIdentity/userAssignedIdentities/" not in resource:
            raise BaselineError("IDENTITY_UNOBSERVABLE")
        exact(item, {"clientId", "principalId"})
        uuid(item["clientId"])
        uuid(item["principalId"])


def validate_ingress(ingress: Any) -> None:
    exact(
        ingress,
        {"fqdn", "external", "targetPort", "traffic"},
        {"transport", "allowInsecure"},
    )
    host(ingress["fqdn"])
    boolean(ingress["external"])
    integer(ingress["targetPort"], 1, 65535)
    if "transport" in ingress and ingress["transport"] not in ("auto", "http", "http2"):
        raise BaselineError("APP_UNOBSERVABLE")
    if "allowInsecure" in ingress:
        boolean(ingress["allowInsecure"])
    traffic = ingress["traffic"]
    if not isinstance(traffic, list) or len(traffic) != 1:
        raise BaselineError("APP_UNOBSERVABLE")
    exact(traffic[0], {"latestRevision", "weight"})
    boolean(traffic[0]["latestRevision"])
    integer(traffic[0]["weight"], 0, 100)
    if traffic != [{"latestRevision": True, "weight": 100}]:
        raise BaselineError("APP_UNOBSERVABLE")


def validate_resources(resources: Any) -> None:
    exact(resources, {"cpu", "memory"}, {"ephemeralStorage"})
    cpu = resources["cpu"]
    if type(cpu) not in (int, float) or not math.isfinite(cpu) or not 0 < cpu <= 32:
        raise BaselineError("APP_UNOBSERVABLE")
    for key in set(resources) - {"cpu"}:
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?Gi", text(resources[key])):
            raise BaselineError("APP_UNOBSERVABLE")


def validate_scale(scale: Any) -> None:
    exact(
        scale,
        {"minReplicas", "maxReplicas"},
        {"rules", "cooldownPeriod", "pollingInterval"},
    )
    for key in ("minReplicas", "maxReplicas"):
        integer(scale[key], 0, 1000)
    if scale["minReplicas"] > scale["maxReplicas"]:
        raise BaselineError("APP_UNOBSERVABLE")
    for key in ("cooldownPeriod", "pollingInterval"):
        if key in scale:
            integer(scale[key], 1, 3600)
    if not isinstance(scale.get("rules", []), list):
        raise BaselineError("APP_UNOBSERVABLE")
    for rule in scale.get("rules", []):
        exact(rule, {"name", "http"})
        identifier(rule["name"])
        exact(rule["http"], {"metadata"})
        exact(rule["http"]["metadata"], {"concurrentRequests"})
        if not re.fullmatch(
            r"[1-9][0-9]*", text(rule["http"]["metadata"]["concurrentRequests"])
        ):
            raise BaselineError("APP_UNOBSERVABLE")


def validate_environment(settings: Any) -> None:
    exact(settings, set(ENV_KEYS))
    for key, value in settings.items():
        text(value)
        if key in ("Search__Endpoint", "OpenAI__Endpoint", "Tickets__ServiceUri"):
            endpoint(value)
        elif key in (
            "AZURE_CLIENT_ID",
            "AzureAd__TenantId",
            "AzureAd__ClientId",
            "AzureAd__ClientCredentials__0__ManagedIdentityClientId",
        ):
            uuid(value)
        else:
            identifier(value)
    if (
        settings["AzureAd__ClientCredentials__0__SourceType"]
        != "SignedAssertionFromManagedIdentity"
    ):
        raise BaselineError("CONFIG_UNOBSERVABLE")


def validate_app(app: Any) -> None:
    exact(
        app,
        {
            "resource_id",
            "image",
            "revision",
            "effective_environment",
            "managed_identity",
            "ingress",
            "secret_references",
            "container_configuration",
            "scale",
            "environment_id",
            "workload_profile",
            "revision_suffix",
        },
    )
    resource_id(app["resource_id"])
    image_digest(app["image"])
    identifier(app["revision"])
    validate_environment(app["effective_environment"])
    validate_identity(app["managed_identity"])
    validate_ingress(app["ingress"])
    exact(app["container_configuration"], {"name", "resources"})
    identifier(app["container_configuration"]["name"])
    validate_resources(app["container_configuration"]["resources"])
    validate_scale(app["scale"])
    resource_id(app["environment_id"])
    identifier(app["workload_profile"])
    if app["revision_suffix"] != "":
        identifier(app["revision_suffix"])
    if not isinstance(app["secret_references"], dict):
        raise BaselineError("SECRET_VERSION_UNOBSERVABLE")
    for name, url in app["secret_references"].items():
        identifier(name)
        if not re.fullmatch(
            r"https://[a-zA-Z0-9-]+\.vault\.azure\.net/secrets/[a-zA-Z0-9-]+/[a-zA-Z0-9]+",
            text(url),
        ):
            raise BaselineError("SECRET_VERSION_UNOBSERVABLE")


def validate_network(network: Any, ai: bool) -> None:
    exact(
        network,
        {"defaultAction", "ipRules"} if ai else {"ipRules", "bypass"},
        {"virtualNetworkRules"} if ai else set(),
    )
    if ai:
        if network["defaultAction"] not in ("Allow", "Deny") or network.get(
            "virtualNetworkRules"
        ):
            raise BaselineError("CONFIG_UNOBSERVABLE")
    elif network["bypass"] not in ("None", "AzureServices"):
        raise BaselineError("CONFIG_UNOBSERVABLE")
    if not isinstance(network["ipRules"], list):
        raise BaselineError("CONFIG_UNOBSERVABLE")
    for item in network["ipRules"]:
        exact(item, {"value"})
        ipaddress.ip_network(text(item["value"]), strict=False)


def validate_configuration(configuration: Any, ai: bool) -> None:
    exact(
        configuration,
        {
            "disableLocalAuth",
            "publicNetworkAccess",
            "networkAcls",
            "customSubDomainName",
        }
        if ai
        else {
            "authOptions",
            "disableLocalAuth",
            "publicNetworkAccess",
            "networkRuleSet",
        },
    )
    boolean(configuration["disableLocalAuth"])
    if configuration["publicNetworkAccess"] not in ("Enabled", "Disabled"):
        raise BaselineError("CONFIG_UNOBSERVABLE")
    validate_network(configuration["networkAcls" if ai else "networkRuleSet"], ai)
    if ai:
        identifier(configuration["customSubDomainName"])
    else:
        auth = configuration["authOptions"]
        exact(auth, {"aadOrApiKey"})
        exact(auth["aadOrApiKey"], {"aadAuthFailureMode"})
        if auth["aadOrApiKey"]["aadAuthFailureMode"] not in (
            "http401WithBearerChallenge",
            "http403",
        ):
            raise BaselineError("CONFIG_UNOBSERVABLE")


def artifact_files(
    api: VerificationAPI,
    artifact_id: int,
    name: str,
    run_id: int,
    source: str,
    archive_sha256: str,
    members: set[str],
    deadline: Deadline,
) -> dict[str, bytes]:
    """Verify transport integrity only; metadata is not upload ownership proof."""
    integer(artifact_id, 1, 2**63 - 1)
    integer(run_id, 1, 2**63 - 1)
    sha(source, 40)
    sha(archive_sha256)
    metadata = api.get(f"actions/artifacts/{artifact_id}")
    if (
        metadata.get("id") != artifact_id
        or metadata.get("name") != name
        or metadata.get("expired") is not False
    ):
        raise BaselineError("ARTIFACT_UNAVAILABLE")
    run = metadata.get("workflow_run", {})
    if run.get("id") != run_id or run.get("head_sha") != source:
        raise BaselineError("ARTIFACT_RUN_BINDING")
    if metadata.get("digest") != "sha256:" + archive_sha256:
        raise BaselineError("ARTIFACT_DIGEST_BINDING")
    archive = api.archive(artifact_id)
    deadline.remaining()
    if len(archive) > PAGE_BYTES or digest(archive) != archive_sha256:
        raise BaselineError("ARTIFACT_ARCHIVE_DIGEST")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            infos = bundle.infolist()
            if (
                len(infos) != len(members)
                or {info.filename for info in infos} != members
            ):
                raise BaselineError("ARTIFACT_MEMBERS")
            if sum(info.file_size for info in infos) > PAGE_BYTES:
                raise BaselineError("RESPONSE_BYTES")
            return {
                info.filename: bounded_read(bundle.open(info), PAGE_BYTES, deadline)
                for info in infos
            }
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
        raise BaselineError("ARTIFACT_ARCHIVE_INVALID") from None


ENV_KEYS = (
    "AZURE_CLIENT_ID",
    "Search__Endpoint",
    "Search__IndexName",
    "OpenAI__Endpoint",
    "OpenAI__DeploymentName",
    "AzureAd__TenantId",
    "AzureAd__ClientId",
    "AzureAd__Scope",
    "AzureAd__ClientCredentials__0__SourceType",
    "AzureAd__ClientCredentials__0__ManagedIdentityClientId",
    "Tickets__ServiceUri",
)


def app_projection(app: dict[str, Any]) -> dict[str, Any]:
    """Do not hash ARM definitions containing secret values."""
    properties = app["properties"]
    configuration = properties["configuration"]
    if set(configuration) - {
        "activeRevisionsMode",
        "ingress",
        "secrets",
        "registries",
        "dapr",
        "service",
        "maxInactiveRevisions",
    }:
        raise BaselineError("APP_UNOBSERVABLE")
    if configuration.get("registries"):
        raise BaselineError("REGISTRY_CONFIGURATION_UNOBSERVABLE")
    containers = properties["template"]["containers"]
    if (
        len(containers) != 1
        or properties["configuration"]["activeRevisionsMode"] != "Single"
    ):
        raise BaselineError("APP_UNOBSERVABLE")
    container = containers[0]
    if set(container) - {"name", "image", "env", "resources"}:
        raise BaselineError("APP_UNOBSERVABLE")
    if (
        set(properties["template"])
        - {"containers", "scale", "revisionSuffix", "volumes", "initContainers"}
        or properties["template"].get("volumes")
        or properties["template"].get("initContainers")
    ):
        raise BaselineError("APP_UNOBSERVABLE")
    if properties["configuration"].get("dapr", {}).get("enabled") or properties[
        "configuration"
    ].get("service"):
        raise BaselineError("APP_UNOBSERVABLE")
    settings: dict[str, str] = {}
    for item in container.get("env", []):
        name = text(item.get("name"))
        if name not in ENV_KEYS or name in settings or "secretRef" in item:
            raise BaselineError("CONFIG_UNOBSERVABLE")
        settings[name] = text(item.get("value"))
    settings.setdefault("Search__IndexName", "kb")
    settings.setdefault("OpenAI__DeploymentName", "chat")
    if set(settings) != set(ENV_KEYS):
        raise BaselineError("CONFIG_UNOBSERVABLE")
    secrets: dict[str, str] = {}
    for item in properties["configuration"].get("secrets", []):
        url = text(item.get("keyVaultUrl"))
        if not re.fullmatch(
            r"https://[a-zA-Z0-9-]+\.vault\.azure\.net/secrets/[^/]+/[a-zA-Z0-9]+", url
        ):
            raise BaselineError("SECRET_VERSION_UNOBSERVABLE")
        secrets[text(item.get("name"))] = url
    image = text(container.get("image"))
    if not re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", image):
        raise BaselineError("APP_IMAGE")
    validate_environment(settings)
    validate_ingress(configuration["ingress"])
    validate_scale(properties["template"]["scale"])
    validate_resources(container["resources"])
    identity = app["identity"]
    validate_identity(identity)
    if identity.get("type") != "UserAssigned" or not identity.get(
        "userAssignedIdentities"
    ):
        raise BaselineError("IDENTITY_UNOBSERVABLE")
    if properties["latestReadyRevisionName"] != properties["latestRevisionName"]:
        raise BaselineError("APP_NOT_READY")
    result = {
        "resource_id": text(app.get("id")),
        "image": image,
        "revision": text(properties.get("latestReadyRevisionName")),
        "effective_environment": settings,
        "managed_identity": identity,
        "ingress": properties["configuration"]["ingress"],
        "secret_references": secrets,
        "container_configuration": {
            key: container[key] for key in ("name", "resources")
        },
        "scale": properties["template"]["scale"],
        "environment_id": text(properties.get("managedEnvironmentId")),
        "workload_profile": text(properties.get("workloadProfileName")),
        "revision_suffix": properties["template"].get("revisionSuffix", ""),
    }
    validate_app(result)
    return result


def vectorizer_projection(
    item: Any, embedding_deployment: str, ai_endpoint: str
) -> dict[str, Any]:
    """Credential-safe observable vectorizer identity, or an explicit rejection.

    The approved dependency identity binds deployment and endpoint here, then
    observes Search's system identity and its AI authority in every plane.
    Keyed and explicit authIdentity configurations remain unsupported.
    """
    parameters = item.get("azureOpenAIParameters") if isinstance(item, dict) else None
    if not isinstance(parameters, dict):
        raise BaselineError("INDEX_VECTORIZER_UNOBSERVABLE")
    if parameters.get("apiKey") is not None or item.get("apiKey") is not None:
        raise BaselineError("INDEX_VECTORIZER_CREDENTIAL_UNOBSERVABLE")
    if (
        parameters.get("authIdentity") is not None
        or item.get("authIdentity") is not None
    ):
        raise BaselineError("INDEX_VECTORIZER_AUTHORITY_UNOBSERVABLE")
    exact(item, {"name", "kind", "azureOpenAIParameters"})
    identifier(item["name"])
    if item["kind"] != "azureOpenAI":
        raise BaselineError("INDEX_VECTORIZER_UNOBSERVABLE")
    exact(parameters, {"deploymentId", "resourceUri"})
    identifier(parameters["deploymentId"])
    endpoint(parameters["resourceUri"])
    if parameters["deploymentId"] != embedding_deployment or parameters[
        "resourceUri"
    ].rstrip("/") != ai_endpoint.rstrip("/"):
        raise BaselineError("INDEX_VECTORIZER_BINDING")
    return {
        "name": item["name"],
        "kind": item["kind"],
        "azureOpenAIParameters": {
            "deploymentId": parameters["deploymentId"],
            "resourceUri": parameters["resourceUri"],
        },
    }


def index_projection(
    definition: dict[str, Any],
    embedding_deployment: str | None = None,
    ai_endpoint: str | None = None,
) -> dict[str, Any]:
    exact(
        definition,
        {"name", "fields", "vectorSearch"},
        {
            "@odata.etag",
            "scoringProfiles",
            "defaultScoringProfile",
            "corsOptions",
            "suggesters",
            "analyzers",
            "tokenizers",
            "tokenFilters",
            "charFilters",
            "similarity",
            "semantic",
        },
    )
    identifier(definition["name"])
    for key in (
        "scoringProfiles",
        "defaultScoringProfile",
        "corsOptions",
        "suggesters",
        "analyzers",
        "tokenizers",
        "tokenFilters",
        "charFilters",
        "similarity",
        "semantic",
    ):
        if definition.get(key) not in (None, []):
            raise BaselineError("INDEX_SCHEMA_UNOBSERVABLE")
    fields = definition["fields"]
    if not isinstance(fields, list) or not fields:
        raise BaselineError("INDEX_SCHEMA_UNOBSERVABLE")
    seen = set()
    for field in fields:
        exact(
            field,
            {"name", "type"},
            {
                "key",
                "searchable",
                "filterable",
                "sortable",
                "facetable",
                "retrievable",
                "stored",
                "dimensions",
                "vectorSearchProfile",
            },
        )
        name = identifier(field["name"])
        if name in seen or field["type"] not in (
            "Edm.String",
            "Collection(Edm.Single)",
        ):
            raise BaselineError("INDEX_SCHEMA_UNOBSERVABLE")
        seen.add(name)
        for key in (
            "key",
            "searchable",
            "filterable",
            "sortable",
            "facetable",
            "retrievable",
            "stored",
        ):
            if key in field:
                boolean(field[key])
        if "dimensions" in field:
            integer(field["dimensions"], 1, 4096)
        if "vectorSearchProfile" in field:
            identifier(field["vectorSearchProfile"])
    if seen != {"id", "title", "content", "url", "embedding"}:
        raise BaselineError("INDEX_SCHEMA_UNOBSERVABLE")
    vector = definition["vectorSearch"]
    exact(vector, {"profiles", "algorithms"}, {"compressions", "vectorizers"})
    if vector.get("compressions"):
        raise BaselineError("INDEX_SCHEMA_UNOBSERVABLE")
    vectorizers = vector.get("vectorizers", [])
    projected = []
    for item in vectorizers:
        if embedding_deployment is None or ai_endpoint is None:
            raise BaselineError("INDEX_VECTORIZER_UNOBSERVABLE")
        projected.append(vectorizer_projection(item, embedding_deployment, ai_endpoint))
    if not isinstance(vector["profiles"], list) or not isinstance(
        vector["algorithms"], list
    ):
        raise BaselineError("INDEX_SCHEMA_UNOBSERVABLE")
    for profile in vector["profiles"]:
        exact(profile, {"name", "algorithm"}, {"vectorizer"})
        identifier(profile["name"])
        identifier(profile["algorithm"])
        if "vectorizer" in profile:
            identifier(profile["vectorizer"])
            if profile["vectorizer"] not in {item["name"] for item in projected}:
                raise BaselineError("INDEX_VECTORIZER_UNOBSERVABLE")
    for algorithm in vector["algorithms"]:
        exact(algorithm, {"name", "kind", "hnswParameters"})
        identifier(algorithm["name"])
        if algorithm["kind"] != "hnsw":
            raise BaselineError("INDEX_SCHEMA_UNOBSERVABLE")
        params = algorithm["hnswParameters"]
        exact(params, {"metric", "m", "efConstruction", "efSearch"})
        if params["metric"] not in ("cosine", "euclidean", "dotProduct"):
            raise BaselineError("INDEX_SCHEMA_UNOBSERVABLE")
        for key in ("m", "efConstruction", "efSearch"):
            integer(params[key], 1, 10000)
    if not any(
        field["name"] == "embedding"
        and field.get("retrievable") is True
        and field["type"] == "Collection(Edm.Single)"
        and "dimensions" in field
        for field in fields
    ):
        raise BaselineError("VECTORS_UNOBSERVABLE")
    return {
        "name": definition["name"],
        "fields": fields,
        "vectorSearch": {
            "profiles": vector["profiles"],
            "algorithms": vector["algorithms"],
            "vectorizers": projected,
        },
    }


def fingerprint_documents(
    fetch: Callable[[int], bytes],
    count: int,
    dimensions: int,
    deadline: Deadline,
) -> dict[str, Any]:
    if type(count) is not int or not 1 <= count <= MAX_DOCUMENTS:
        raise BaselineError("DOCUMENT_COUNT")
    end = min(deadline.end, deadline.clock() + 180)
    pass_deadline = Deadline(end, deadline.clock)
    total = 0
    rows = 0
    previous = ""
    hasher = hashlib.sha256()
    content_hasher = hashlib.sha256(b"[")
    for page in range(MAX_PAGES):
        pass_deadline.remaining()
        raw = fetch(page * 1000)
        pass_deadline.remaining()
        if len(raw) > PAGE_BYTES:
            raise BaselineError("RESPONSE_BYTES")
        total += len(raw)
        if total > TOTAL_BYTES:
            raise BaselineError("TOTAL_BYTES")
        response = object_json(raw)
        values = response.get("value")
        if (
            not isinstance(values, list)
            or len(values) > 1000
            or response.get("@odata.nextLink")
        ):
            raise BaselineError("PAGINATION")
        if not values:
            if rows != count:
                raise BaselineError("COUNT_DRIFT")
            content_hasher.update(b"]")
            return {
                "count": rows,
                "content_vector_sha256": hasher.hexdigest(),
                "index_snapshot_sha256": content_hasher.hexdigest(),
            }
        for item in values:
            if not isinstance(item, dict) or set(item) != {
                "id",
                "title",
                "content",
                "url",
                "embedding",
            }:
                raise BaselineError("DOCUMENT_SCHEMA")
            identifier = text(item["id"])
            if identifier <= previous:
                raise BaselineError("DOCUMENT_ORDER_OR_DUPLICATE")
            previous = identifier
            for key in ("title", "content", "url"):
                text(item[key])
            vector = item["embedding"]
            if (
                not isinstance(vector, list)
                or len(vector) != dimensions
                or any(
                    type(value) not in (int, float) or not math.isfinite(value)
                    for value in vector
                )
            ):
                raise BaselineError("VECTOR")
            hasher.update(canonical(item) + b"\n")
            if rows:
                content_hasher.update(b", ")
            content_hasher.update(
                json.dumps(
                    {key: item[key] for key in ("id", "title", "content", "url")},
                    sort_keys=True,
                ).encode()
            )
            rows += 1
            if rows > count or rows > MAX_DOCUMENTS:
                raise BaselineError("COUNT_DRIFT")
    raise BaselineError("TERMINAL_PROBE_MISSING")


def azure_token(resource: str, deadline: Deadline) -> str:
    try:
        result = subprocess.run(
            [
                "az",
                "account",
                "get-access-token",
                "--resource",
                resource,
                "--query",
                "accessToken",
                "-o",
                "tsv",
            ],
            capture_output=True,
            timeout=deadline.remaining(),
            check=False,
        )
        if result.returncode or len(result.stdout) > 16384:
            raise BaselineError("AUTH_DENIED")
        return text(result.stdout.decode().strip())
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeError):
        raise BaselineError("AUTH_DENIED") from None


def model_projection(model: dict[str, Any]) -> dict[str, Any]:
    properties = model["properties"]
    exact(
        properties,
        {"model"},
        {
            "raiPolicyName",
            "versionUpgradeOption",
            "currentCapacity",
            "provisioningState",
            "dynamicThrottlingEnabled",
        },
    )
    validate_model_name(properties["model"])
    for key in ("raiPolicyName", "versionUpgradeOption", "provisioningState"):
        if key in properties:
            identifier(properties[key])
    if "currentCapacity" in properties:
        integer(properties["currentCapacity"], 0, 1000000)
    if "dynamicThrottlingEnabled" in properties:
        boolean(properties["dynamicThrottlingEnabled"])
    sku = model["sku"]
    exact(sku, {"name", "capacity"}, {"tier"})
    identifier(sku["name"])
    integer(sku["capacity"], 0, 1000000)
    if "tier" in sku:
        identifier(sku["tier"])
    return {
        "resource_id": resource_id(model["id"]),
        "properties": properties,
        "sku": sku,
    }


@dataclass
class AzureCapture:
    deadline: Deadline

    def get(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        limit: int = PAGE_BYTES,
    ) -> bytes:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise BaselineError("CAPTURE_URL")
        if parsed.netloc == "management.azure.com":
            audience = "https://management.azure.com/"
        elif parsed.netloc == "graph.microsoft.com":
            audience = "https://graph.microsoft.com/"
        elif re.fullmatch(r"[a-z0-9-]+\.search\.windows\.net", parsed.netloc):
            audience = "https://search.azure.com/"
        else:
            raise BaselineError("CAPTURE_URL")
        return request_bytes(
            url,
            {
                "Authorization": "Bearer " + azure_token(audience, self.deadline),
                "Content-Type": "application/json",
            },
            self.deadline,
            limit=limit,
            method=method,
            body=body,
        )

    def capture(self, target: dict[str, Any]) -> dict[str, Any]:
        """Read-only observations; denied fields and unresolved references reject."""
        import authority_state
        import external_state

        self.deadline.remaining()
        validate_target(target)

        def arm(resource: str, version: str) -> dict[str, Any]:
            result = object_json(
                self.get(
                    f"https://management.azure.com{resource}?api-version={version}"
                )
            )
            if result.get("id", "").lower() != resource.lower():
                raise BaselineError("RESOURCE_BINDING")
            return result

        app = app_projection(arm(target["app_resource_id"], "2026-07-01"))
        search = arm(target["search_resource_id"], "2025-05-01")
        ai = arm(target["ai_resource_id"], "2024-10-01")
        search_endpoint = (
            "https://"
            + target["search_resource_id"].rsplit("/", 1)[1]
            + ".search.windows.net"
        )
        ai_endpoint = endpoint(ai["properties"]["endpoint"])
        configuration = {}
        for name, resource, keys in (
            (
                "search_configuration",
                search,
                (
                    "authOptions",
                    "disableLocalAuth",
                    "publicNetworkAccess",
                    "networkRuleSet",
                ),
            ),
            (
                "ai_configuration",
                ai,
                (
                    "disableLocalAuth",
                    "publicNetworkAccess",
                    "networkAcls",
                    "customSubDomainName",
                ),
            ),
        ):
            configuration[name] = {key: resource["properties"][key] for key in keys}
            validate_configuration(configuration[name], name == "ai_configuration")
        url = search_endpoint + "/indexes/" + target["index_name"]
        definition = index_projection(
            object_json(self.get(url + "?api-version=2025-09-01")),
            target["embedding_deployment"],
            ai_endpoint,
        )
        if definition["name"] != target["index_name"]:
            raise BaselineError("RESOURCE_BINDING")
        count_raw = self.get(url + "/docs/$count?api-version=2025-09-01")
        if not re.fullmatch(rb"[1-9][0-9]*", count_raw):
            raise BaselineError("DOCUMENT_COUNT")
        dimensions = next(
            field["dimensions"]
            for field in definition["fields"]
            if field["name"] == "embedding"
        )
        documents = fingerprint_documents(
            lambda skip: self.get(
                url + "/docs/search?api-version=2025-09-01",
                "POST",
                canonical(
                    {
                        "search": "*",
                        "select": "id,title,content,url,embedding",
                        "orderby": "id asc",
                        "top": 1000,
                        "skip": skip,
                    }
                ),
            ),
            int(count_raw),
            dimensions,
            self.deadline,
        )
        models = {
            role: model_projection(
                arm(
                    target["ai_resource_id"]
                    + "/deployments/"
                    + target[role + "_deployment"],
                    "2024-10-01",
                )
            )
            for role in ("chat", "embedding")
        }
        observer = authority_state.Observe(
            lambda url, method: object_json(
                self.get(url, method, limit=authority_state.MAX_BYTES)
            ),
            self.deadline,
        )
        external = external_state.capture(
            lambda url: observer.read(url), target, app, models
        )
        if (
            external["application"]["appId"]
            != app["effective_environment"]["AzureAd__ClientId"]
        ):
            raise BaselineError("EXTERNAL_APP_BINDING")
        authority = authority_state.capture(
            observer, self.deadline, target, app, search, external
        )
        return {
            "target": target,
            "authority": authority,
            "app": app,
            "search_endpoint": search_endpoint,
            "ai_endpoint": ai_endpoint,
            "index_schema_sha256": digest(canonical(definition)),
            "documents": documents,
            "models": models,
            "external_state": external,
            **configuration,
        }


def validate_baseline(document: dict[str, Any]) -> None:
    import authority_state
    import external_state

    try:
        exact(document, {"schema", "candidate", "captured_at", "planes"})
        if document["schema"] != "DependencyBaselineV1":
            raise BaselineError("BASELINE_SCHEMA")
        origin_time(document["captured_at"])
        validate_candidate(document["candidate"])
        exact(document["planes"], {"staging", "production"})
        for plane in document["planes"].values():
            exact(
                plane,
                {
                    "target",
                    "authority",
                    "app",
                    "search_endpoint",
                    "ai_endpoint",
                    "index_schema_sha256",
                    "documents",
                    "models",
                    "search_configuration",
                    "ai_configuration",
                    "external_state",
                },
            )
            validate_target(plane["target"])
            validate_app(plane["app"])
            external_state.validate(plane["external_state"])
            authority_state.validate(
                plane["authority"],
                plane["target"],
                plane["app"],
                plane["external_state"],
            )
            if (
                plane["external_state"]["application"]["appId"]
                != plane["app"]["effective_environment"]["AzureAd__ClientId"]
            ):
                raise BaselineError("EXTERNAL_APP_BINDING")
            endpoint(plane["search_endpoint"])
            endpoint(plane["ai_endpoint"])
            sha(plane["index_schema_sha256"])
            exact(
                plane["documents"],
                {"count", "content_vector_sha256", "index_snapshot_sha256"},
            )
            sha(plane["documents"]["content_vector_sha256"])
            sha(plane["documents"]["index_snapshot_sha256"])
            integer(plane["documents"]["count"], 1, MAX_DOCUMENTS)
            exact(plane["models"], {"chat", "embedding"})
            for model in plane["models"].values():
                exact(model, {"resource_id", "properties", "sku"})
                model_projection(
                    {
                        "id": model["resource_id"],
                        "properties": model["properties"],
                        "sku": model["sku"],
                    }
                )
            validate_configuration(plane["search_configuration"], False)
            validate_configuration(plane["ai_configuration"], True)
            target = plane["target"]
            if plane["app"]["resource_id"] != target["app_resource_id"]:
                raise BaselineError("RESOURCE_BINDING")
            settings = plane["app"]["effective_environment"]
            if settings["AzureAd__TenantId"] != target["tenant_id"] or (
                settings["Search__Endpoint"].rstrip("/")
                != plane["search_endpoint"].rstrip("/")
                or settings["OpenAI__Endpoint"].rstrip("/")
                != plane["ai_endpoint"].rstrip("/")
                or settings["Search__IndexName"] != target["index_name"]
                or settings["OpenAI__DeploymentName"] != target["chat_deployment"]
            ):
                raise BaselineError("ENDPOINT_BINDING")
            for role in ("chat", "embedding"):
                if (
                    plane["models"][role]["resource_id"]
                    != target["ai_resource_id"]
                    + "/deployments/"
                    + target[f"{role}_deployment"]
                ):
                    raise BaselineError("RESOURCE_BINDING")
                if plane["external_state"]["rai"][role]["name"] != plane["models"][
                    role
                ]["properties"].get("raiPolicyName"):
                    raise BaselineError("EXTERNAL_RAI_BINDING")
        canonical(document)
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        if isinstance(error, BaselineError):
            raise
        raise BaselineError("BASELINE_SCHEMA") from None


def compare_planes(
    baseline: dict[str, Any], capture: Callable[[dict[str, Any]], dict[str, Any]]
) -> str:
    validate_baseline(baseline)
    for name in ("staging", "production"):
        expected = baseline["planes"][name]
        actual = capture(expected["target"])
        if canonical(actual) != canonical(expected):
            raise BaselineError("DEPENDENCY_DRIFT")
    return digest(canonical(baseline["planes"]))

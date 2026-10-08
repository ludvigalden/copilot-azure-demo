"""Bounded external authorization observations; unsupported shapes reject."""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable
from typing import Any

import dependency_baseline as b

APPLICATION_FIELDS = (
    "id",
    "appId",
    "signInAudience",
    "identifierUris",
    "requiredResourceAccess",
    "api",
    "appRoles",
    "web",
    "spa",
    "publicClient",
    "isFallbackPublicClient",
)
SP_FIELDS = (
    "id",
    "appId",
    "accountEnabled",
    "appRoleAssignmentRequired",
    "servicePrincipalType",
    "appRoles",
    "oauth2PermissionScopes",
)


def string_list(value: Any) -> None:
    if not isinstance(value, list) or len(value) > 1000:
        raise b.BaselineError("EXTERNAL_STATE_SCHEMA")
    for item in value:
        b.text(item)


def roles(value: Any, scope: bool = False) -> None:
    if not isinstance(value, list) or len(value) > 1000:
        raise b.BaselineError("EXTERNAL_STATE_SCHEMA")
    seen_ids, seen_values = set(), set()
    for item in value:
        required = {"id", "isEnabled", "value"}
        optional = (
            {
                "adminConsentDescription",
                "adminConsentDisplayName",
                "type",
                "userConsentDescription",
                "userConsentDisplayName",
            }
            if scope
            else {"allowedMemberTypes", "description", "displayName", "origin"}
        )
        b.exact(item, required, optional)
        b.uuid(item["id"])
        b.boolean(item["isEnabled"])
        b.identifier(item["value"])
        if item["id"].lower() in seen_ids or item["value"] in seen_values:
            raise b.BaselineError("EXTERNAL_DEFINITION_DUPLICATE")
        seen_ids.add(item["id"].lower())
        seen_values.add(item["value"])
        for key in set(item) - required:
            if key == "allowedMemberTypes":
                string_list(item[key])
            elif item[key] is not None:
                b.text(item[key])


def canonical_definitions(value: Any) -> list[dict[str, Any]]:
    result = []
    for definition in value:
        item = dict(definition)
        if "allowedMemberTypes" in item:
            item["allowedMemberTypes"] = sorted(item["allowedMemberTypes"])
        result.append(item)
    return sorted(result, key=b.canonical)


def normalize(state: dict[str, Any]) -> dict[str, Any]:
    application = state["application"]
    application["identifierUris"] = sorted(application["identifierUris"])
    application["appRoles"] = canonical_definitions(application["appRoles"])
    application["api"]["oauth2PermissionScopes"] = canonical_definitions(
        application["api"]["oauth2PermissionScopes"]
    )
    for resource in application["requiredResourceAccess"]:
        resource["resourceAccess"] = sorted(resource["resourceAccess"], key=b.canonical)
    application["requiredResourceAccess"] = sorted(
        application["requiredResourceAccess"], key=b.canonical
    )
    for key in ("web", "spa", "publicClient"):
        application[key]["redirectUris"] = sorted(application[key]["redirectUris"])
    principal = state["service_principal"]
    for key in ("appRoles", "oauth2PermissionScopes"):
        principal[key] = canonical_definitions(principal[key])
    for grant in state["grants"]:
        grant["scope"] = " ".join(sorted(grant["scope"].split()))
    for key in ("grants", "assignments"):
        state[key] = sorted(state[key], key=b.canonical)
    for policy in state["rai"].values():
        policy["properties"]["contentFilters"] = sorted(
            policy["properties"]["contentFilters"], key=b.canonical
        )
    return state


def application(value: Any) -> None:
    b.exact(value, set(APPLICATION_FIELDS))
    b.uuid(value["id"])
    b.uuid(value["appId"])
    if value["signInAudience"] != "AzureADMyOrg":
        raise b.BaselineError("EXTERNAL_TENANT_UNSUPPORTED")
    string_list(value["identifierUris"])
    if not isinstance(value["requiredResourceAccess"], list):
        raise b.BaselineError("EXTERNAL_STATE_SCHEMA")
    for resource in value["requiredResourceAccess"]:
        b.exact(resource, {"resourceAppId", "resourceAccess"})
        b.uuid(resource["resourceAppId"])
        if not isinstance(resource["resourceAccess"], list):
            raise b.BaselineError("EXTERNAL_STATE_SCHEMA")
        for access in resource["resourceAccess"]:
            b.exact(access, {"id", "type"})
            b.uuid(access["id"])
            if access["type"] not in ("Role", "Scope"):
                raise b.BaselineError("EXTERNAL_STATE_SCHEMA")
    api = value["api"]
    b.exact(
        api,
        {
            "acceptMappedClaims",
            "knownClientApplications",
            "oauth2PermissionScopes",
            "preAuthorizedApplications",
            "requestedAccessTokenVersion",
        },
    )
    if api["acceptMappedClaims"] not in (None, False):
        raise b.BaselineError("MAPPED_CLAIMS_UNOBSERVABLE")
    if api["knownClientApplications"] or api["preAuthorizedApplications"]:
        raise b.BaselineError("EXTERNAL_CLIENT_UNOBSERVABLE")
    if api["requestedAccessTokenVersion"] != 2:
        raise b.BaselineError("EXTERNAL_TOKEN_VERSION")
    roles(api["oauth2PermissionScopes"], True)
    roles(value["appRoles"])
    for kind in ("web", "spa", "publicClient"):
        b.exact(
            value[kind],
            {"redirectUris"},
            {"homePageUrl", "logoutUrl", "implicitGrantSettings"}
            if kind == "web"
            else set(),
        )
        string_list(value[kind]["redirectUris"])
        for key in ("homePageUrl", "logoutUrl"):
            if value[kind].get(key) is not None:
                b.text(value[kind][key])
        if "implicitGrantSettings" in value[kind]:
            settings = value[kind]["implicitGrantSettings"]
            b.exact(settings, {"enableAccessTokenIssuance", "enableIdTokenIssuance"})
            for enabled in settings.values():
                b.boolean(enabled)
    if value["isFallbackPublicClient"] not in (None, False):
        raise b.BaselineError("PUBLIC_CLIENT_UNSUPPORTED")


def service_principal(value: Any) -> None:
    b.exact(value, set(SP_FIELDS))
    b.uuid(value["id"])
    b.uuid(value["appId"])
    for key in ("accountEnabled", "appRoleAssignmentRequired"):
        b.boolean(value[key])
    if value["servicePrincipalType"] != "Application" or not value["accountEnabled"]:
        raise b.BaselineError("EXTERNAL_PRINCIPAL_UNSUPPORTED")
    roles(value["appRoles"])
    roles(value["oauth2PermissionScopes"], True)


def validate(state: Any) -> None:
    b.exact(
        state,
        {
            "auth_config",
            "application",
            "service_principal",
            "grants",
            "assignments",
            "rai",
        },
    )
    # Disabled Easy Auth is the only supported credential-safe child configuration.
    b.exact(state["auth_config"], {"platform", "globalValidation", "identityProviders"})
    b.exact(state["auth_config"]["platform"], {"enabled"})
    if state["auth_config"]["platform"]["enabled"] is not False:
        raise b.BaselineError("AUTH_CONFIG_UNOBSERVABLE")
    if (
        state["auth_config"]["globalValidation"]
        or state["auth_config"]["identityProviders"]
    ):
        raise b.BaselineError("AUTH_CONFIG_UNOBSERVABLE")
    application(state["application"])
    service_principal(state["service_principal"])
    if state["application"]["appId"] != state["service_principal"]["appId"]:
        raise b.BaselineError("EXTERNAL_APP_BINDING")
    for key, fields in (
        (
            "grants",
            {"id", "clientId", "consentType", "principalId", "resourceId", "scope"},
        ),
        (
            "assignments",
            {"id", "principalId", "resourceId", "appRoleId", "principalType"},
        ),
    ):
        values = state[key]
        if not isinstance(values, list) or len(values) > 1000:
            raise b.BaselineError("EXTERNAL_STATE_SCHEMA")
        for item in values:
            b.exact(item, fields)
            for field in fields:
                if key == "grants" and field in ("resourceId", "principalId"):
                    continue
                if (
                    field not in ("scope", "consentType", "principalType", "id")
                    and item[field] is not None
                ):
                    b.uuid(item[field])
            b.text(item["id"])
            if key == "grants":
                if item["clientId"] != state["service_principal"]["id"]:
                    raise b.BaselineError("EXTERNAL_GRANT_BINDING")
                b.text(item["scope"])
                if item["consentType"] not in ("AllPrincipals", "Principal"):
                    raise b.BaselineError("EXTERNAL_STATE_SCHEMA")
                try:
                    b.uuid(item["resourceId"])
                except b.BaselineError:
                    raise b.BaselineError("EXTERNAL_GRANT_RESOURCE") from None
                if item["consentType"] == "Principal":
                    try:
                        b.uuid(item["principalId"])
                    except b.BaselineError:
                        raise b.BaselineError("EXTERNAL_GRANT_PRINCIPAL") from None
                elif item["principalId"] is not None:
                    b.uuid(item["principalId"])
            else:
                if item["resourceId"] != state["service_principal"]["id"]:
                    raise b.BaselineError("EXTERNAL_ASSIGNMENT_BINDING")
                if item["principalType"] not in ("User", "Group", "ServicePrincipal"):
                    raise b.BaselineError("EXTERNAL_STATE_SCHEMA")
    b.exact(state["rai"], {"chat", "embedding"})
    for policy in state["rai"].values():
        b.exact(policy, {"name", "properties"})
        b.identifier(policy["name"])
        properties = policy["properties"]
        b.exact(
            properties,
            {"mode", "basePolicyName", "type", "contentFilters", "customBlocklists"},
        )
        for key in ("mode", "basePolicyName", "type"):
            b.identifier(properties[key])
        if properties["customBlocklists"]:
            raise b.BaselineError("RAI_BLOCKLIST_UNOBSERVABLE")
        if not isinstance(properties["contentFilters"], list):
            raise b.BaselineError("EXTERNAL_STATE_SCHEMA")
        for item in properties["contentFilters"]:
            b.exact(
                item, {"name", "enabled", "blocking", "severityThreshold", "source"}
            )
            b.boolean(item["enabled"])
            b.boolean(item["blocking"])
            for key in ("name", "severityThreshold", "source"):
                b.identifier(item[key])


def collection(get: Callable[[str], dict[str, Any]], url: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    seen: set[str] = set()
    first = urllib.parse.urlsplit(url)
    original = urllib.parse.parse_qs(first.query, keep_blank_values=True)
    seen_ids: set[str] = set()
    for _ in range(11):
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if (
            url in seen
            or parsed.scheme != "https"
            or parsed.netloc != "graph.microsoft.com"
            or parsed.path != first.path
            or parsed.username
            or parsed.password
            or parsed.fragment
            or any(query.get(k) != v for k, v in original.items())
            or set(query) - set(original) - {"$skiptoken", "$skipToken", "$skip"}
        ):
            raise b.BaselineError("EXTERNAL_PAGINATION")
        seen.add(url)
        page = get(url)
        b.exact(page, {"value"}, {"@odata.context", "@odata.nextLink"})
        if not isinstance(page["value"], list) or len(page["value"]) > 100:
            raise b.BaselineError("EXTERNAL_PAGINATION")
        for item in page["value"]:
            identity = b.text(item.get("id")).lower()
            if identity in seen_ids:
                raise b.BaselineError("EXTERNAL_PAGINATION")
            seen_ids.add(identity)
        values.extend(page["value"])
        if len(values) > 1000:
            raise b.BaselineError("EXTERNAL_PAGINATION")
        if "@odata.nextLink" not in page or page["@odata.nextLink"] is None:
            return sorted(values, key=b.canonical)
        url = b.text(page["@odata.nextLink"])
    raise b.BaselineError("EXTERNAL_PAGINATION")


def capture(
    get: Callable[[str], dict[str, Any]],
    target: dict[str, Any],
    app: dict[str, Any],
    models: dict[str, Any],
) -> dict[str, Any]:
    auth = get(
        f"https://management.azure.com{target['app_resource_id']}/authConfigs/current?api-version=2026-07-01"
    )
    auth_properties = auth["properties"]
    b.exact(auth_properties, {"platform", "globalValidation", "identityProviders"})
    client = app["effective_environment"]["AzureAd__ClientId"]
    root = "https://graph.microsoft.com/v1.0"
    applications = collection(
        get,
        root
        + "/applications?"
        + urllib.parse.urlencode(
            {
                "$filter": f"appId eq '{client}'",
                "$select": ",".join(APPLICATION_FIELDS),
                "$top": "100",
            }
        ),
    )
    principals = collection(
        get,
        root
        + "/servicePrincipals?"
        + urllib.parse.urlencode(
            {
                "$filter": f"appId eq '{client}'",
                "$select": ",".join(SP_FIELDS),
                "$top": "100",
            }
        ),
    )
    if len(applications) != 1 or len(principals) != 1:
        raise b.BaselineError("EXTERNAL_APP_UNOBSERVABLE")
    principal = principals[0]
    grants = collection(
        get,
        root
        + "/oauth2PermissionGrants?"
        + urllib.parse.urlencode(
            {
                "$filter": f"clientId eq '{principal['id']}'",
                "$select": "id,clientId,consentType,principalId,resourceId,scope",
                "$top": "100",
            }
        ),
    )
    assignments = collection(
        get,
        root
        + f"/servicePrincipals/{principal['id']}/appRoleAssignedTo?"
        + urllib.parse.urlencode(
            {
                "$select": "id,principalId,resourceId,appRoleId,principalType",
                "$top": "100",
            }
        ),
    )
    rai = {}
    for role, model in models.items():
        name = b.identifier(model["properties"].get("raiPolicyName"))
        response = get(
            f"https://management.azure.com{target['ai_resource_id']}/raiPolicies/{name}?api-version=2024-10-01"
        )
        rai[role] = {"name": response["name"], "properties": response["properties"]}
        if response["name"] != name:
            raise b.BaselineError("RAI_BINDING")
    result = {
        "auth_config": auth_properties,
        "application": applications[0],
        "service_principal": principal,
        "grants": grants,
        "assignments": assignments,
        "rai": rai,
    }
    validate(result)
    return normalize(result)

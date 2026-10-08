"""Bounded nonsecret execution-identity and authorization observations."""

from __future__ import annotations

import math
import re
import urllib.parse
from collections.abc import Callable
from typing import Any

import dependency_baseline as b
import external_state as e

ARM = "https://management.azure.com"
GRAPH = "https://graph.microsoft.com/v1.0"
MODE = "observed_execution_authority_v1"
MAX_BYTES = 8 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
ZERO = "00000000-0000-0000-0000-000000000000"
SP_FIELDS = (*e.SP_FIELDS, "appOwnerOrganizationId")


def fail(code: str = "AUTHORITY_UNOBSERVABLE") -> None:
    raise b.BaselineError(code)


def normalized_id(value: Any) -> str:
    return b.text(value).lower()


def arm_id(value: Any, kind: str, subscription: str) -> str:
    value = b.resource_id(value)
    pattern = (
        r"/subscriptions/"
        + re.escape(subscription)
        + r"/resourceGroups/[a-zA-Z0-9_.()-]+/providers/"
        + re.escape(kind)
        + r"/[a-zA-Z0-9_.()-]+"
    )
    if not re.fullmatch(pattern, value, re.IGNORECASE):
        fail("AUTHORITY_RESOURCE_BINDING")
    return value


def table_endpoint(value: Any) -> str:
    parsed = urllib.parse.urlsplit(b.text(value))
    if (
        parsed.scheme != "https"
        or not re.fullmatch(r"[a-z0-9]{3,24}\.table\.core\.windows\.net", parsed.netloc)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        fail("TICKETS_ENDPOINT_BINDING")
    return "https://" + parsed.netloc


def ordered(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(values, key=b.canonical)


def response_size(value: Any, depth: int = 0) -> int:
    if depth > 32:
        fail("AUTHORITY_BYTES")
    if isinstance(value, dict):
        if len(value) > 10000 or any(not isinstance(k, str) for k in value):
            fail("AUTHORITY_BYTES")
        size = 2
        for key, item in value.items():
            size += response_size(key, depth + 1) + response_size(item, depth + 1) + 2
            if size > MAX_BYTES:
                fail("AUTHORITY_BYTES")
        return size
    if isinstance(value, list):
        if len(value) > 10000:
            fail("AUTHORITY_BYTES")
        size = 2
        for item in value:
            size += response_size(item, depth + 1) + 1
            if size > MAX_BYTES:
                fail("AUTHORITY_BYTES")
        return size
    if isinstance(value, str):
        if len(value) > MAX_BYTES // 6:
            fail("AUTHORITY_BYTES")
        try:
            return 2 + 6 * len(value.encode("utf-8"))
        except UnicodeError:
            fail("AUTHORITY_SCHEMA")
    if value is None or type(value) is bool:
        return 5
    if type(value) is int:
        if value.bit_length() > 4096:
            fail("AUTHORITY_BYTES")
        return 2 + value.bit_length()
    if type(value) is float:
        if not math.isfinite(value):
            fail("AUTHORITY_SCHEMA")
        return 32
    fail("AUTHORITY_SCHEMA")
    return 0


def graph_object(value: Any, fields: set[str]) -> dict[str, Any]:
    b.exact(value, fields, {"@odata.context", "@odata.type"})
    return {k: value[k] for k in fields}


def canonical_principal(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    for key in ("appRoles", "oauth2PermissionScopes"):
        result[key] = e.canonical_definitions(value[key])
    return result


class Observe:
    """Only approved routes; terminal pagination and aggregate work are mandatory."""

    def __init__(self, get: Callable[..., dict[str, Any]], deadline: b.Deadline):
        self.get = get
        self.deadline = deadline
        self.total = 0
        self.calls = 0

    def read(self, url: str, method: str = "GET") -> dict[str, Any]:
        self.deadline.remaining()
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc not in ("management.azure.com", "graph.microsoft.com")
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            fail("AUTHORITY_ROUTE")
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        name = r"[a-zA-Z0-9_.()-]+"
        subscription = r"/subscriptions/[0-9a-f-]{36}"
        group = subscription + r"/resourceGroups/" + name
        management_group = r"/providers/Microsoft.Management/managementGroups/" + name
        resource = (
            group
            + r"/providers/(?:Microsoft.Search/searchServices/"
            + name
            + r"(?:/indexes/"
            + name
            + r")?|Microsoft.CognitiveServices/accounts/"
            + name
            + r"(?:/deployments/"
            + name
            + r")?|Microsoft.Storage/storageAccounts/"
            + name
            + r"(?:/tableServices/default(?:/tables/"
            + name
            + r")?)?)"
        )
        scope = (
            r"(?:"
            + subscription
            + "|"
            + group
            + "|"
            + management_group
            + "|"
            + resource
            + ")"
        )
        arm_routes = {
            "2020-05-01": r"(?:/providers/Microsoft.Management/getEntities|"
            + management_group
            + ")",
            "2022-12-01": subscription,
            "2023-01-31": group
            + r"/providers/Microsoft.ManagedIdentity/userAssignedIdentities/"
            + name,
            "2025-01-01": group
            + r"/providers/Microsoft.Storage/storageAccounts/"
            + name,
            "2026-07-01": group
            + r"/providers/Microsoft.App/containerApps/"
            + name
            + r"/authConfigs/current",
            "2024-10-01": group
            + r"/providers/Microsoft.CognitiveServices/accounts/"
            + name
            + r"/raiPolicies/"
            + name,
            "2020-10-01": scope
            + r"/providers/Microsoft.Authorization/(?:roleAssignmentScheduleInstances|roleEligibilityScheduleInstances)",
            "2022-04-01": r"(?:"
            + scope
            + r"/providers/Microsoft.Authorization/(?:roleAssignments|denyAssignments|roleDefinitions/[0-9a-f-]{36})|/providers/Microsoft.Authorization/roleDefinitions/[0-9a-f-]{36})",
            "2022-10-01": r"(?:"
            + subscription
            + "|"
            + group
            + r")/providers/Microsoft.ManagedServices/registrationAssignments",
        }
        graph_route = re.fullmatch(
            r"/v1\.0/(?:applications(?:/[0-9a-f-]{36}/federatedIdentityCredentials)?|servicePrincipals(?:/[0-9a-f-]{36}(?:/(?:transitiveMemberOf|appRoleAssignments|appRoleAssignedTo))?)?|groups/[0-9a-f-]{36}|oauth2PermissionGrants)",
            parsed.path,
        )
        if any(len(values) != 1 for values in query.values()):
            fail("AUTHORITY_ROUTE")
        if parsed.netloc == "management.azure.com":
            version = query.get("api-version", [""])[0]
            route = arm_routes.get(version)
            if route is None or not re.fullmatch(route, parsed.path):
                fail("AUTHORITY_ROUTE")
            if set(query) - {
                "api-version",
                "$filter",
                "$top",
                "$skiptoken",
                "$skipToken",
                "$skip",
            }:
                fail("AUTHORITY_ROUTE")
            expected_method = (
                "POST"
                if parsed.path == "/providers/Microsoft.Management/getEntities"
                else "GET"
            )
        else:
            if not graph_route or set(query) - {
                "$select",
                "$filter",
                "$top",
                "$skiptoken",
                "$skipToken",
                "$skip",
            }:
                fail("AUTHORITY_ROUTE")
            expected_method = "GET"
        if method != expected_method:
            fail("AUTHORITY_ROUTE")
        self.calls += 1
        if self.calls > 800:
            fail("AUTHORITY_CALL_BOUND")
        try:
            response = self.get(url, method)
        except b.BaselineError as error:
            if str(error) in {"AUTHORITY_ROUTE", "RESPONSE_BYTES", "DEADLINE"}:
                raise
            raise b.BaselineError("AUTHORITY_READ_DENIED") from None
        self.deadline.remaining()
        size = response_size(response)
        self.total += size
        if size > MAX_BYTES or self.total > MAX_TOTAL:
            fail("AUTHORITY_BYTES")
        if not isinstance(response, dict):
            fail()
        return response

    def collection(
        self, url: str, graph: bool = False, method: str = "GET"
    ) -> list[dict[str, Any]]:
        first = urllib.parse.urlsplit(url)
        original = urllib.parse.parse_qs(first.query, keep_blank_values=True)
        seen_urls: set[str] = set()
        seen_ids: set[str] = set()
        result: list[dict[str, Any]] = []
        next_key = "@odata.nextLink" if graph else "nextLink"
        for _ in range(11):
            current = urllib.parse.urlsplit(url)
            query = urllib.parse.parse_qs(current.query, keep_blank_values=True)
            if (
                url in seen_urls
                or current.scheme != first.scheme
                or current.netloc != first.netloc
                or current.path != first.path
                or current.fragment
                or current.username
                or current.password
                or any(query.get(k) != v for k, v in original.items())
                or set(query) - set(original) - {"$skiptoken", "$skipToken", "$skip"}
            ):
                fail("AUTHORITY_PAGINATION")
            seen_urls.add(url)
            page = self.read(url, method)
            b.exact(page, {"value"}, {next_key, "@odata.context", "count"})
            if "count" in page:
                b.integer(page["count"], 0, 1000)
            values = page["value"]
            if not isinstance(values, list) or len(values) > 100:
                fail("AUTHORITY_PAGINATION")
            for item in values:
                if not isinstance(item, dict):
                    fail()
                identity = normalized_id(item.get("id"))
                if identity in seen_ids:
                    fail("AUTHORITY_DUPLICATE")
                seen_ids.add(identity)
                result.append(item)
            if len(result) > 1000:
                fail("AUTHORITY_PAGINATION")
            if next_key not in page or page[next_key] is None:
                return result
            url = b.text(page[next_key])
        fail("AUTHORITY_PAGINATION")
        return []

    def arm(self, resource: str, version: str, kind: str) -> dict[str, Any]:
        item = self.read(f"{ARM}{resource}?api-version={version}")
        if (
            normalized_id(item.get("id")) != normalized_id(resource)
            or normalized_id(item.get("type")) != kind.lower()
        ):
            fail("AUTHORITY_RESOURCE_BINDING")
        return item

    def principal(self, identity: str, tenant: str, kind: str) -> dict[str, Any]:
        item = self.read(
            f"{GRAPH}/servicePrincipals/{b.uuid(identity)}?"
            + urllib.parse.urlencode({"$select": ",".join(SP_FIELDS)})
        )
        item = graph_object(item, set(SP_FIELDS))
        validate_principal(item, tenant, kind)
        if item["id"] != identity:
            fail("AUTHORITY_PRINCIPAL_BINDING")
        return canonical_principal(item)


def validate_principal(item: Any, tenant: str, kind: str) -> None:
    b.exact(item, set(SP_FIELDS))
    b.uuid(item["id"])
    b.uuid(item["appId"])
    if item["appOwnerOrganizationId"] is not None:
        b.uuid(item["appOwnerOrganizationId"])
    if (
        item["servicePrincipalType"] != kind
        or item["accountEnabled"] is not True
        or (
            kind == "ManagedIdentity"
            and item["appOwnerOrganizationId"] not in (None, tenant)
        )
    ):
        fail("AUTHORITY_PRINCIPAL_BINDING")
    b.boolean(item["appRoleAssignmentRequired"])
    e.roles(item["appRoles"])
    e.roles(item["oauth2PermissionScopes"], True)


def ancestry(obs: Observe, target: dict[str, Any]) -> list[str]:
    subscription, tenant = target["subscription_id"], target["tenant_id"]
    entities = obs.collection(
        f"{ARM}/providers/Microsoft.Management/getEntities?"
        + urllib.parse.urlencode(
            {
                "api-version": "2020-05-01",
                "$filter": f"name eq '{subscription}'",
                "$top": "100",
            }
        ),
        method="POST",
    )
    matches = [
        x
        for x in entities
        if normalized_id(x["id"]) == "/subscriptions/" + subscription
    ]
    if len(matches) != 1:
        fail("AUTHORITY_ANCESTRY_UNOBSERVABLE")
    properties = matches[0].get("properties", {})
    if properties.get("tenantId") != tenant:
        fail("AUTHORITY_ANCESTRY_BINDING")
    names = properties.get("parentNameChain")
    e.string_list(names)
    if not 1 <= len(names) <= 7 or tenant not in names or len(set(names)) != len(names):
        fail("AUTHORITY_ANCESTRY_UNOBSERVABLE")
    parents = {}
    for name in names:
        b.identifier(name)
        scope = "/providers/Microsoft.Management/managementGroups/" + name
        group = obs.read(f"{ARM}{scope}?api-version=2020-05-01")
        if normalized_id(group.get("id")) != normalized_id(scope):
            fail("AUTHORITY_ANCESTRY_BINDING")
        props = group.get("properties", {})
        if props.get("tenantId") != tenant:
            fail("AUTHORITY_ANCESTRY_UNOBSERVABLE")
        parent = props.get("details", {}).get("parent")
        parents[normalized_id(scope)] = (
            None if parent is None else normalized_id(parent.get("id"))
        )
    root = "/providers/Microsoft.Management/managementGroups/" + tenant
    parent = properties.get("parent")
    if not isinstance(parent, dict):
        fail("AUTHORITY_ANCESTRY_BINDING")
    current = normalized_id(parent.get("id"))
    reverse = []
    while current is not None:
        if current in reverse or current not in parents:
            fail("AUTHORITY_ANCESTRY_BINDING")
        reverse.append(current)
        current = parents[current]
    if set(reverse) != set(parents) or reverse[-1] != normalized_id(root):
        fail("AUTHORITY_ANCESTRY_BINDING")
    sub = obs.read(f"{ARM}/subscriptions/{subscription}?api-version=2022-12-01")
    if (
        sub.get("tenantId") != tenant
        or sub.get("subscriptionId") != subscription
        or sub.get("state") != "Enabled"
    ):
        fail("AUTHORITY_ANCESTRY_BINDING")
    canonical_scopes = {
        normalized_id(
            "/providers/Microsoft.Management/managementGroups/" + name
        ): "/providers/Microsoft.Management/managementGroups/" + name
        for name in names
    }
    return [
        *(canonical_scopes[scope] for scope in reversed(reverse)),
        "/subscriptions/" + subscription,
    ]


def resource_scopes(target: dict[str, Any], ancestors: list[str]) -> list[str]:
    values = set(ancestors)
    for key in ("search_resource_id", "ai_resource_id", "tickets_resource_id"):
        resource = target[key]
        values.add(resource.split("/providers/", 1)[0])
        values.add(resource)
    values.add(target["search_resource_id"] + "/indexes/" + target["index_name"])
    for role in ("chat", "embedding"):
        values.add(
            target["ai_resource_id"] + "/deployments/" + target[role + "_deployment"]
        )
    values.add(target["tickets_resource_id"] + "/tableServices/default")
    values.add(target["tickets_resource_id"] + "/tableServices/default/tables/tickets")
    return [*ancestors, *sorted(values - set(ancestors))]


def memberships(obs: Observe, principal: str) -> list[dict[str, Any]]:
    values = obs.collection(
        f"{GRAPH}/servicePrincipals/{principal}/transitiveMemberOf?$select=id&$top=100",
        True,
    )
    groups = []
    for value in values:
        if value.get("@odata.type") != "#microsoft.graph.group":
            fail("AUTHORITY_MEMBERSHIP_UNSUPPORTED")
        identity = b.uuid(value["id"])
        group = obs.read(
            f"{GRAPH}/groups/{identity}?"
            + urllib.parse.urlencode(
                {
                    "$select": "id,securityEnabled,groupTypes,membershipRule,membershipRuleProcessingState"
                }
            )
        )
        group = graph_object(
            group,
            {
                "id",
                "securityEnabled",
                "groupTypes",
                "membershipRule",
                "membershipRuleProcessingState",
            },
        )
        if (
            group["id"] != identity
            or group["securityEnabled"] is not True
            or group["groupTypes"] != []
            or group["membershipRule"] is not None
            or group["membershipRuleProcessingState"] is not None
        ):
            fail("AUTHORITY_MEMBERSHIP_UNSUPPORTED")
        groups.append(group)
    return ordered(groups)


def permission(value: Any) -> None:
    b.exact(value, {"actions", "notActions", "dataActions", "notDataActions"})
    for strings in value.values():
        e.string_list(strings)
        if len(strings) != len(set(strings)):
            fail("AUTHORITY_DUPLICATE")


def authorization_id(identity: Any, kind: str) -> str:
    identity = b.text(identity)
    suffix = "/providers/Microsoft.Authorization/" + kind + "/"
    if identity.count(suffix) != 1:
        fail("AUTHORITY_ROLE_BINDING")
    scope, name = identity.split(suffix)
    b.uuid(name)
    if not scope and kind == "roleDefinitions":
        return "/"
    if not re.fullmatch(
        r"/(?:subscriptions/[0-9a-f-]{36}(?:/resourceGroups/[a-zA-Z0-9_.-]+(?:/providers/[a-zA-Z0-9.]+/[a-zA-Z0-9]+/[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9]+/[a-zA-Z0-9_.-]+)*)?)?|providers/Microsoft.Management/managementGroups/[a-zA-Z0-9_.()-]+)",
        scope,
    ):
        fail("AUTHORITY_ROLE_BINDING")
    return scope


def permissions(value: Any) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= 1000:
        fail("AUTHORITY_ROLE_UNOBSERVABLE")
    for item in value:
        permission(item)


def subject_types(subjects: dict[str, list[str]]) -> dict[str, str]:
    result = {p: "ServicePrincipal" for p in subjects}
    for groups in subjects.values():
        for group in groups:
            if group in result and result[group] != "Group":
                fail("AUTHORITY_PRINCIPAL_BINDING")
            result[group] = "Group"
    return result


def deny_principals(value: Any, known: dict[str, str]) -> None:
    records(value, {"id", "type"})
    for item in value:
        identity = b.uuid(item["id"])
        kind = item["type"]
        if identity == ZERO:
            if kind != "SystemDefined":
                fail("AUTHORITY_PRINCIPAL_BINDING")
        elif kind not in ("User", "Group", "ServicePrincipal"):
            fail("AUTHORITY_PRINCIPAL_UNSUPPORTED")
        elif identity in known and known[identity] != kind:
            fail("AUTHORITY_PRINCIPAL_BINDING")


def role_definition(obs: Observe, identity: str, scopes: list[str]) -> dict[str, Any]:
    role_scope = authorization_id(identity, "roleDefinitions")
    if role_scope != "/" and role_scope not in scopes:
        fail("AUTHORITY_ROLE_BINDING")
    role = obs.arm(identity, "2022-04-01", "Microsoft.Authorization/roleDefinitions")
    props = role["properties"]
    b.exact(
        props,
        {"roleName", "description", "type", "permissions", "assignableScopes"},
        {"createdOn", "updatedOn", "createdBy", "updatedBy"},
    )
    if props["type"] not in ("BuiltInRole", "CustomRole"):
        fail("AUTHORITY_ROLE_UNSUPPORTED")
    e.string_list(props["assignableScopes"])
    permissions(props["permissions"])
    if len(set(props["assignableScopes"])) != len(props["assignableScopes"]):
        fail("AUTHORITY_DUPLICATE")
    for scope in props["assignableScopes"]:
        if scope != "/" and scope not in scopes:
            fail("AUTHORITY_ROLE_SCOPE")
    return {
        "id": identity,
        "type": props["type"],
        "assignableScopes": sorted(props["assignableScopes"]),
        "permissions": ordered(
            [{k: sorted(v) for k, v in p.items()} for p in props["permissions"]]
        ),
    }


def inherited(parent: str, child: str, scopes: list[str]) -> bool:
    parent, child = normalized_id(parent), normalized_id(child)
    if parent == child or child.startswith(parent + "/"):
        return True
    groups = [normalized_id(s) for s in scopes if "/managementgroups/" in s.lower()]
    if parent in groups:
        return child not in groups or groups.index(parent) <= groups.index(child)
    return False


def permission_matches(patterns: list[str], operation: str) -> bool:
    return any(
        re.fullmatch(re.escape(p).replace(r"\*", ".*"), operation, re.IGNORECASE)
        for p in patterns
    )


def permits(permissions: list[dict[str, Any]], operation: str, data: bool) -> bool:
    allow, subtract = (
        ("dataActions", "notDataActions") if data else ("actions", "notActions")
    )
    return any(
        permission_matches(p[allow], operation)
        and not permission_matches(p[subtract], operation)
        for p in permissions
    )


def required_operations(
    target: dict[str, Any], workload: str, search: str
) -> list[dict[str, Any]]:
    return [
        {
            "principalId": workload,
            "scope": target["search_resource_id"] + "/indexes/" + target["index_name"],
            "operation": "Microsoft.Search/searchServices/indexes/documents/read",
            "data": True,
        },
        {
            "principalId": workload,
            "scope": target["ai_resource_id"]
            + "/deployments/"
            + target["chat_deployment"],
            "operation": "Microsoft.CognitiveServices/accounts/OpenAI/deployments/chat/completions/action",
            "data": True,
        },
        {
            "principalId": search,
            "scope": target["ai_resource_id"]
            + "/deployments/"
            + target["embedding_deployment"],
            "operation": "Microsoft.CognitiveServices/accounts/OpenAI/deployments/embeddings/action",
            "data": True,
        },
        {
            "principalId": workload,
            "scope": target["tickets_resource_id"],
            "operation": "Microsoft.Storage/storageAccounts/tableServices/tables/write",
            "data": False,
        },
        {
            "principalId": workload,
            "scope": target["tickets_resource_id"]
            + "/tableServices/default/tables/tickets",
            "operation": "Microsoft.Storage/storageAccounts/tableServices/tables/entities/add/action",
            "data": True,
        },
    ]


def effective(
    arm: dict[str, Any],
    subjects: dict[str, list[str]],
    scopes: list[str],
    operations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    definitions = {r["id"]: r for r in arm["roles"]}
    results = []
    for operation in operations:
        principal, scope = operation["principalId"], operation["scope"]
        identities = [principal, *subjects[principal]]
        grants = []
        for assignment in arm["assignments"]:
            role = definitions.get(assignment["roleDefinitionId"])
            if role is None:
                fail("AUTHORITY_ROLE_BINDING")
            if not any(
                s == "/" or inherited(s, assignment["scope"], scopes)
                for s in role["assignableScopes"]
            ):
                fail("AUTHORITY_ROLE_SCOPE")
            if (
                assignment["principalId"] in identities
                and inherited(assignment["scope"], scope, scopes)
                and permits(
                    role["permissions"], operation["operation"], operation["data"]
                )
            ):
                grants.append(assignment["id"])
        denied = []
        for deny in arm["denies"]:
            applies = any(p["id"] in [ZERO, *identities] for p in deny["principals"])
            excluded = any(
                p["id"] in [ZERO, *identities] for p in deny["excludePrincipals"]
            )
            at_scope = normalized_id(deny["scope"]) == normalized_id(scope)
            if (
                applies
                and not excluded
                and (
                    at_scope
                    or (
                        not deny["doNotApplyToChildScopes"]
                        and inherited(deny["scope"], scope, scopes)
                    )
                )
                and permits(
                    deny["permissions"], operation["operation"], operation["data"]
                )
            ):
                denied.append(deny["id"])
        if not grants or denied:
            fail("AUTHORITY_OPERATION_DENIED")
        results.append(
            operation | {"assignments": sorted(grants), "denies": sorted(denied)}
        )
    return ordered(results)


def arm_authority(
    obs: Observe, scopes: list[str], subjects: dict[str, list[str]]
) -> dict[str, Any]:
    assignments: dict[str, Any] = {}
    denies: dict[str, Any] = {}
    definitions: dict[str, Any] = {}
    normalized_scopes = {normalized_id(x) for x in scopes}
    inventories = []
    known = subject_types(subjects)
    for scope in scopes:
        root = f"{ARM}{scope}/providers/Microsoft.Authorization/"
        for schedule in (
            "roleAssignmentScheduleInstances",
            "roleEligibilityScheduleInstances",
        ):
            records = obs.collection(
                root + schedule + "?api-version=2020-10-01&$filter=atScope()"
            )
            if records:
                fail("AUTHORITY_PIM_UNSUPPORTED")
            inventories.append({"scope": scope, "kind": schedule, "ids": []})
        if re.fullmatch(
            r"/subscriptions/[^/]+(?:/resourceGroups/[^/]+)?", scope, re.IGNORECASE
        ):
            delegated = obs.collection(
                f"{ARM}{scope}/providers/Microsoft.ManagedServices/registrationAssignments?api-version=2022-10-01"
            )
            if delegated:
                fail("AUTHORITY_DELEGATION_UNSUPPORTED")
            inventories.append(
                {"scope": scope, "kind": "registrationAssignments", "ids": []}
            )
        for kind, destination in (
            ("roleAssignments", assignments),
            ("denyAssignments", denies),
        ):
            records = obs.collection(
                root + kind + "?api-version=2022-04-01&$filter=atScope()"
            )
            ids = []
            for record in records:
                b.exact(record, {"id", "name", "type", "properties"})
                if (
                    record["type"].lower()
                    != ("Microsoft.Authorization/" + kind).lower()
                ):
                    fail("AUTHORITY_RESOURCE_BINDING")
                props = record["properties"]
                if normalized_id(
                    props.get("scope")
                ) not in normalized_scopes or not inherited(
                    props["scope"], scope, scopes
                ):
                    fail("AUTHORITY_SCOPE_BINDING")
                identity = b.text(record["id"])
                if normalized_id(authorization_id(identity, kind)) != normalized_id(
                    props["scope"]
                ):
                    fail("AUTHORITY_SCOPE_BINDING")
                if identity.rsplit("/", 1)[1] != record["name"]:
                    fail("AUTHORITY_RESOURCE_BINDING")
                if kind == "roleAssignments":
                    b.exact(
                        props,
                        {"principalId", "principalType", "roleDefinitionId", "scope"},
                        {
                            "condition",
                            "conditionVersion",
                            "delegatedManagedIdentityResourceId",
                            "description",
                            "createdOn",
                            "updatedOn",
                            "createdBy",
                            "updatedBy",
                        },
                    )
                    b.uuid(props["principalId"])
                    if props["principalType"] not in (
                        "User",
                        "Group",
                        "ServicePrincipal",
                    ):
                        fail("AUTHORITY_PRINCIPAL_UNSUPPORTED")
                    if (
                        props["principalId"] in known
                        and props["principalType"] != known[props["principalId"]]
                    ):
                        fail("AUTHORITY_PRINCIPAL_BINDING")
                    if any(
                        props.get(k) is not None
                        for k in (
                            "condition",
                            "conditionVersion",
                            "delegatedManagedIdentityResourceId",
                        )
                    ):
                        fail("AUTHORITY_CONDITION_UNSUPPORTED")
                    projection = {
                        k: props[k]
                        for k in (
                            "principalId",
                            "principalType",
                            "roleDefinitionId",
                            "scope",
                        )
                    }
                    role = props["roleDefinitionId"]
                    if role not in definitions:
                        definitions[role] = role_definition(obs, role, scopes)
                else:
                    b.exact(
                        props,
                        {
                            "scope",
                            "permissions",
                            "principals",
                            "excludePrincipals",
                            "doNotApplyToChildScopes",
                            "isSystemProtected",
                        },
                        {
                            "denyAssignmentName",
                            "description",
                            "condition",
                            "conditionVersion",
                            "createdOn",
                            "updatedOn",
                            "createdBy",
                            "updatedBy",
                        },
                    )
                    if (
                        props.get("condition") is not None
                        or props.get("conditionVersion") is not None
                    ):
                        fail("AUTHORITY_CONDITION_UNSUPPORTED")
                    for key in ("doNotApplyToChildScopes", "isSystemProtected"):
                        b.boolean(props[key])
                    permissions(props["permissions"])
                    for key in ("principals", "excludePrincipals"):
                        deny_principals(props[key], known)
                    if not props["principals"]:
                        fail("AUTHORITY_DENY_BINDING")
                    projection = {
                        k: props[k]
                        for k in (
                            "scope",
                            "permissions",
                            "principals",
                            "excludePrincipals",
                            "doNotApplyToChildScopes",
                            "isSystemProtected",
                        )
                    }
                    projection["permissions"] = ordered(
                        [
                            {k: sorted(v) for k, v in p.items()}
                            for p in props["permissions"]
                        ]
                    )
                    for key in ("principals", "excludePrincipals"):
                        projection[key] = ordered(props[key])
                    projection["applicability"] = {
                        principal: {
                            "principal_matches": any(
                                p["id"] in [ZERO, principal, *groups]
                                for p in props["principals"]
                            ),
                            "excluded": any(
                                p["id"] in [ZERO, principal, *groups]
                                for p in props["excludePrincipals"]
                            ),
                            "scopes": [
                                s
                                for s in scopes
                                if normalized_id(s) == normalized_id(props["scope"])
                                or (
                                    not props["doNotApplyToChildScopes"]
                                    and inherited(props["scope"], s, scopes)
                                )
                            ],
                        }
                        for principal, groups in subjects.items()
                    }
                projection["id"] = identity
                if identity in destination and destination[identity] != projection:
                    fail("AUTHORITY_INVENTORY_DRIFT")
                destination[identity] = projection
                ids.append(identity)
            inventories.append({"scope": scope, "kind": kind, "ids": sorted(ids)})
    return {
        "inventories": ordered(inventories),
        "assignments": ordered(list(assignments.values())),
        "denies": ordered(list(denies.values())),
        "roles": ordered(list(definitions.values())),
    }


def graph_authority(
    obs: Observe, principals: list[str], external: dict[str, Any], tenant: str
) -> dict[str, Any]:
    resources: dict[str, Any] = {}
    outgoing = []
    delegated = []

    def resource(identity: str) -> dict[str, Any]:
        b.uuid(identity)
        if identity not in resources:
            resources[identity] = obs.principal(identity, tenant, "Application")
        return resources[identity]

    for principal in principals:
        records = obs.collection(
            f"{GRAPH}/servicePrincipals/{principal}/appRoleAssignments?"
            + urllib.parse.urlencode(
                {"$select": "id,principalId,resourceId,appRoleId", "$top": "100"}
            ),
            True,
        )
        for record in records:
            b.exact(record, {"id", "principalId", "resourceId", "appRoleId"})
            for key in ("principalId", "resourceId", "appRoleId"):
                b.uuid(record[key])
            if record["principalId"] != principal or record["appRoleId"] == ZERO:
                fail("AUTHORITY_OUTGOING_BINDING")
            role = [
                r
                for r in resource(record["resourceId"])["appRoles"]
                if r["id"] == record["appRoleId"]
            ]
            if (
                len(role) != 1
                or role[0]["isEnabled"] is not True
                or "Application" not in role[0].get("allowedMemberTypes", [])
            ):
                fail("AUTHORITY_OUTGOING_BINDING")
        outgoing.append({"principalId": principal, "assignments": ordered(records)})
    for grant in external["grants"]:
        definitions = resource(grant["resourceId"])["oauth2PermissionScopes"]
        names = grant["scope"].split()
        if not names or len(names) != len(set(names)):
            fail("AUTHORITY_DELEGATED_BINDING")
        for name in names:
            matches = [s for s in definitions if s["value"] == name]
            if len(matches) != 1 or matches[0]["isEnabled"] is not True:
                fail("AUTHORITY_DELEGATED_BINDING")
        delegated.append(
            {
                "grantId": grant["id"],
                "resourceId": grant["resourceId"],
                "scopes": sorted(names),
            }
        )
    return {
        "outgoing": ordered(outgoing),
        "delegated": ordered(delegated),
        "resources": ordered(list(resources.values())),
    }


def capture(
    obs: Observe,
    deadline: b.Deadline,
    target: dict[str, Any],
    app: dict[str, Any],
    search: dict[str, Any],
    external: dict[str, Any],
) -> dict[str, Any]:
    deadline.remaining()
    tenant = target["tenant_id"]
    settings = app["effective_environment"]
    if (
        settings["AzureAd__ClientCredentials__0__SourceType"]
        != "SignedAssertionFromManagedIdentity"
    ):
        fail("AUTHORITY_WORKLOAD_BINDING")
    tickets = arm_id(
        target["tickets_resource_id"],
        "Microsoft.Storage/storageAccounts",
        target["subscription_id"],
    )
    account = obs.arm(tickets, "2025-01-01", "Microsoft.Storage/storageAccounts")
    props = account["properties"]
    required = (
        "allowSharedKeyAccess",
        "allowBlobPublicAccess",
        "minimumTlsVersion",
        "publicNetworkAccess",
        "networkAcls",
        "primaryEndpoints",
    )
    non_table_properties = {
        "provisioningState",
        "creationTime",
        "primaryLocation",
        "secondaryLocation",
        "statusOfPrimary",
        "statusOfSecondary",
        "lastGeoFailoverTime",
        "encryption",
        "accessTier",
        "supportsHttpsTrafficOnly",
        "isHnsEnabled",
        "isLocalUserEnabled",
        "isSftpEnabled",
        "largeFileSharesState",
        "privateEndpointConnections",
        "keyCreationTime",
        "keyPolicy",
        "sasPolicy",
        "allowCrossTenantReplication",
        "defaultToOAuthAuthentication",
        "allowedCopyScope",
        "azureFilesIdentityBasedAuthentication",
        "customDomain",
        "routingPreference",
        "dnsEndpointType",
        "secondaryEndpoints",
        "geoReplicationStats",
        "blobRestoreStatus",
        "immutableStorageWithVersioning",
        "isNfsV3Enabled",
        "storageAccountSkuConversionStatus",
        "allowProtectedAppendWritesAll",
        "allowProtectedAppendWrites",
        "failoverInProgress",
        "isAccountMigrationInProgress",
        "dualStackEndpointPreference",
        "placement",
    }
    b.exact(props, set(required), non_table_properties)
    if (
        "supportsHttpsTrafficOnly" in props
        and props["supportsHttpsTrafficOnly"] is not True
    ):
        fail("TICKETS_POLICY_UNSUPPORTED")
    if props.get("dnsEndpointType", "Standard") != "Standard":
        fail("TICKETS_POLICY_UNSUPPORTED")
    if "provisioningState" in props and props["provisioningState"] != "Succeeded":
        fail("TICKETS_POLICY_UNOBSERVABLE")
    if "statusOfPrimary" in props and props["statusOfPrimary"] != "available":
        fail("TICKETS_POLICY_UNOBSERVABLE")
    if (
        props["allowSharedKeyAccess"] is not False
        or props["allowBlobPublicAccess"] is not False
        or props["minimumTlsVersion"] != "TLS1_2"
    ):
        fail("TICKETS_POLICY_UNSUPPORTED")
    uri = table_endpoint(props["primaryEndpoints"].get("table"))
    if uri != table_endpoint(settings["Tickets__ServiceUri"]):
        fail("TICKETS_ENDPOINT_BINDING")
    network = props["networkAcls"]
    b.exact(
        network,
        {"defaultAction", "bypass", "ipRules", "virtualNetworkRules"},
        {"resourceAccessRules"},
    )
    if (
        props["publicNetworkAccess"] != "Enabled"
        or network["defaultAction"] != "Allow"
        or any(
            network.get(k)
            for k in ("ipRules", "virtualNetworkRules", "resourceAccessRules")
        )
    ):
        fail("TICKETS_POLICY_UNSUPPORTED")
    if network["bypass"] not in ("None", "AzureServices"):
        fail("TICKETS_POLICY_UNSUPPORTED")
    attached = app["managed_identity"]["userAssignedIdentities"]
    selected = [
        (r, p)
        for r, p in attached.items()
        if p["clientId"] == settings["AZURE_CLIENT_ID"]
    ]
    if (
        len(selected) != 1
        or settings["AzureAd__ClientCredentials__0__ManagedIdentityClientId"]
        != settings["AZURE_CLIENT_ID"]
    ):
        fail("AUTHORITY_WORKLOAD_BINDING")
    identity, binding = selected[0]
    arm_id(
        identity,
        "Microsoft.ManagedIdentity/userAssignedIdentities",
        target["subscription_id"],
    )
    uami = obs.arm(
        identity, "2023-01-31", "Microsoft.ManagedIdentity/userAssignedIdentities"
    )["properties"]
    for key in ("clientId", "principalId", "tenantId"):
        b.uuid(uami.get(key))
    if (
        any(uami[k] != binding[k] for k in ("clientId", "principalId"))
        or uami["tenantId"] != tenant
    ):
        fail("AUTHORITY_WORKLOAD_BINDING")
    workload = obs.principal(uami["principalId"], tenant, "ManagedIdentity")
    if (
        workload["appId"] != uami["clientId"]
        or workload["appId"] == external["application"]["appId"]
        or workload["id"] == external["service_principal"]["id"]
    ):
        fail("AUTHORITY_WORKLOAD_BINDING")
    api = obs.principal(external["service_principal"]["id"], tenant, "Application")
    if (
        api["appId"] != external["application"]["appId"]
        or api["appOwnerOrganizationId"] != tenant
        or {k: api[k] for k in e.SP_FIELDS} != external["service_principal"]
    ):
        fail("AUTHORITY_PRINCIPAL_BINDING")
    federation = obs.collection(
        f"{GRAPH}/applications/{external['application']['id']}/federatedIdentityCredentials?$top=100",
        True,
    )
    for credential in federation:
        b.exact(
            credential,
            {"id", "name", "issuer", "subject", "audiences"},
            {"description"},
        )
        b.uuid(credential["id"])
        b.uuid(credential["subject"])
        if credential[
            "issuer"
        ] != f"https://login.microsoftonline.com/{tenant}/v2.0" or credential[
            "audiences"
        ] != ["api://AzureADTokenExchange"]:
            fail("AUTHORITY_FEDERATION_BINDING")
    if not any(c["subject"] == uami["principalId"] for c in federation):
        fail("AUTHORITY_FEDERATION_BINDING")
    identity_state = search.get("identity")
    b.exact(identity_state, {"type", "principalId", "tenantId"})
    if (
        identity_state["type"] != "SystemAssigned"
        or identity_state["tenantId"] != tenant
    ):
        fail("INDEX_VECTORIZER_AUTHORITY_UNOBSERVABLE")
    search_principal = obs.principal(
        identity_state["principalId"], tenant, "ManagedIdentity"
    )
    if search_principal["id"] in (workload["id"], api["id"]):
        fail("AUTHORITY_PRINCIPAL_BINDING")
    chain = ancestry(obs, target)
    scopes = resource_scopes(target, chain)
    subjects = {}
    groups = []
    for principal in (workload["id"], search_principal["id"]):
        observed = memberships(obs, principal)
        subjects[principal] = [g["id"] for g in observed]
        groups.append({"principalId": principal, "groups": observed})
    result = {
        "mode": MODE,
        "tenantId": tenant,
        "tickets": {
            "id": tickets,
            "endpoint": uri,
            "policy": {k: props[k] for k in required if k != "primaryEndpoints"},
        },
        "workload": {
            "resourceId": identity,
            "properties": {k: uami[k] for k in ("clientId", "principalId", "tenantId")},
            "principal": workload,
        },
        "api": api,
        "federation": ordered(
            [
                {k: c[k] for k in ("id", "name", "issuer", "subject", "audiences")}
                for c in federation
            ]
        ),
        "searchIdentity": search_principal,
        "ancestry": chain,
        "scopes": scopes,
        "memberships": ordered(groups),
        "arm": arm_authority(obs, scopes, subjects),
        "graph": graph_authority(
            obs, [api["id"], workload["id"], search_principal["id"]], external, tenant
        ),
    }
    result["arm"]["effective"] = effective(
        result["arm"],
        subjects,
        scopes,
        required_operations(target, workload["id"], search_principal["id"]),
    )
    validate(result, target, app, external)
    return result


def validate(
    value: Any, target: dict[str, Any], app: dict[str, Any], external: dict[str, Any]
) -> None:
    b.exact(
        value,
        {
            "mode",
            "tenantId",
            "tickets",
            "workload",
            "api",
            "federation",
            "searchIdentity",
            "ancestry",
            "scopes",
            "memberships",
            "arm",
            "graph",
        },
    )
    if value["mode"] != MODE or value["tenantId"] != target["tenant_id"]:
        fail("AUTHORITY_BINDING")
    b.exact(value["tickets"], {"id", "endpoint", "policy"})
    if value["tickets"]["id"] != target["tickets_resource_id"] or table_endpoint(
        value["tickets"]["endpoint"]
    ) != table_endpoint(app["effective_environment"]["Tickets__ServiceUri"]):
        fail("TICKETS_ENDPOINT_BINDING")
    b.exact(value["workload"], {"resourceId", "properties", "principal"})
    for principal, kind in (
        (value["api"], "Application"),
        (value["workload"]["principal"], "ManagedIdentity"),
        (value["searchIdentity"], "ManagedIdentity"),
    ):
        validate_principal(principal, target["tenant_id"], kind)
    if (
        value["api"]["id"] != external["service_principal"]["id"]
        or value["api"]["appId"] != app["effective_environment"]["AzureAd__ClientId"]
        or {k: value["api"][k] for k in e.SP_FIELDS} != external["service_principal"]
    ):
        fail("AUTHORITY_PRINCIPAL_BINDING")
    workload = value["workload"]
    b.exact(workload["properties"], {"clientId", "principalId", "tenantId"})
    if (
        workload["properties"]
        != {
            "clientId": workload["principal"]["appId"],
            "principalId": workload["principal"]["id"],
            "tenantId": target["tenant_id"],
        }
        or workload["properties"]["clientId"]
        != app["effective_environment"]["AZURE_CLIENT_ID"]
    ):
        fail("AUTHORITY_WORKLOAD_BINDING")
    attached = app["managed_identity"]["userAssignedIdentities"]
    if attached.get(workload["resourceId"]) != {
        k: workload["properties"][k] for k in ("clientId", "principalId")
    }:
        fail("AUTHORITY_WORKLOAD_BINDING")
    if not value["ancestry"] or value["scopes"] != resource_scopes(
        target, value["ancestry"]
    ):
        fail("AUTHORITY_SCOPE_BINDING")
    b.exact(
        value["arm"], {"inventories", "assignments", "denies", "roles", "effective"}
    )
    b.exact(value["graph"], {"outgoing", "delegated", "resources"})
    validate_details(value, target, app, external)
    b.canonical(value)


def records(value: Any, fields: set[str], identity: str = "id") -> None:
    if not isinstance(value, list) or len(value) > 1000:
        fail()
    seen = set()
    for item in value:
        b.exact(item, fields)
        key = normalized_id(item[identity])
        if key in seen:
            fail("AUTHORITY_DUPLICATE")
        seen.add(key)


def validate_details(
    value: dict[str, Any],
    target: dict[str, Any],
    app: dict[str, Any],
    external: dict[str, Any],
) -> None:
    tenant = target["tenant_id"]
    workload = value["workload"]["principal"]
    api, search = value["api"], value["searchIdentity"]
    principals = [api["id"], workload["id"], search["id"]]
    if (
        len(set(principals)) != 3
        or len({api["appId"], workload["appId"], search["appId"]}) != 3
        or api["appOwnerOrganizationId"] != tenant
    ):
        fail("AUTHORITY_PRINCIPAL_BINDING")
    if (
        app["effective_environment"]["AzureAd__ClientCredentials__0__SourceType"]
        != "SignedAssertionFromManagedIdentity"
        or app["effective_environment"][
            "AzureAd__ClientCredentials__0__ManagedIdentityClientId"
        ]
        != workload["appId"]
    ):
        fail("AUTHORITY_WORKLOAD_BINDING")
    arm_id(
        value["workload"]["resourceId"],
        "Microsoft.ManagedIdentity/userAssignedIdentities",
        target["subscription_id"],
    )
    arm_id(
        value["tickets"]["id"],
        "Microsoft.Storage/storageAccounts",
        target["subscription_id"],
    )
    policy = value["tickets"]["policy"]
    b.exact(
        policy,
        {
            "allowSharedKeyAccess",
            "allowBlobPublicAccess",
            "minimumTlsVersion",
            "publicNetworkAccess",
            "networkAcls",
        },
    )
    network = policy["networkAcls"]
    b.exact(
        network,
        {"defaultAction", "bypass", "ipRules", "virtualNetworkRules"},
        {"resourceAccessRules"},
    )
    if (
        policy["allowSharedKeyAccess"] is not False
        or policy["allowBlobPublicAccess"] is not False
        or policy["minimumTlsVersion"] != "TLS1_2"
        or policy["publicNetworkAccess"] != "Enabled"
        or network["defaultAction"] != "Allow"
        or network["bypass"] not in ("None", "AzureServices")
        or any(
            network.get(k)
            for k in ("ipRules", "virtualNetworkRules", "resourceAccessRules")
        )
    ):
        fail("TICKETS_POLICY_UNSUPPORTED")
    records(value["federation"], {"id", "name", "issuer", "subject", "audiences"})
    if not value["federation"]:
        fail("AUTHORITY_FEDERATION_BINDING")
    for credential in value["federation"]:
        b.uuid(credential["id"])
        b.identifier(credential["name"])
        b.uuid(credential["subject"])
        if credential[
            "issuer"
        ] != f"https://login.microsoftonline.com/{tenant}/v2.0" or credential[
            "audiences"
        ] != ["api://AzureADTokenExchange"]:
            fail("AUTHORITY_FEDERATION_BINDING")
    if not any(c["subject"] == workload["id"] for c in value["federation"]):
        fail("AUTHORITY_FEDERATION_BINDING")
    chain = value["ancestry"]
    e.string_list(chain)
    if (
        not 2 <= len(chain) <= 8
        or len(set(chain)) != len(chain)
        or chain[0] != "/providers/Microsoft.Management/managementGroups/" + tenant
        or chain[-1] != "/subscriptions/" + target["subscription_id"]
    ):
        fail("AUTHORITY_ANCESTRY_BINDING")
    for scope in chain[:-1]:
        if not re.fullmatch(
            r"/providers/Microsoft.Management/managementGroups/[a-zA-Z0-9_.()-]+", scope
        ):
            fail("AUTHORITY_ANCESTRY_BINDING")
    records(value["memberships"], {"principalId", "groups"}, "principalId")
    subjects = {}
    for membership in value["memberships"]:
        if membership["principalId"] not in (workload["id"], search["id"]):
            fail("AUTHORITY_MEMBERSHIP_BINDING")
        groups = membership["groups"]
        records(
            groups,
            {
                "id",
                "securityEnabled",
                "groupTypes",
                "membershipRule",
                "membershipRuleProcessingState",
            },
        )
        for group in groups:
            b.uuid(group["id"])
            if (
                group["securityEnabled"] is not True
                or group["groupTypes"] != []
                or group["membershipRule"] is not None
                or group["membershipRuleProcessingState"] is not None
            ):
                fail("AUTHORITY_MEMBERSHIP_UNSUPPORTED")
        subjects[membership["principalId"]] = [g["id"] for g in groups]
    if set(subjects) != {workload["id"], search["id"]}:
        fail("AUTHORITY_MEMBERSHIP_BINDING")
    arm, scopes = value["arm"], value["scopes"]
    for key, fields in (
        (
            "assignments",
            {"id", "principalId", "principalType", "roleDefinitionId", "scope"},
        ),
        ("roles", {"id", "type", "assignableScopes", "permissions"}),
        (
            "denies",
            {
                "id",
                "scope",
                "permissions",
                "principals",
                "excludePrincipals",
                "doNotApplyToChildScopes",
                "isSystemProtected",
                "applicability",
            },
        ),
    ):
        records(arm[key], fields)
    for role in arm["roles"]:
        role_scope = authorization_id(role["id"], "roleDefinitions")
        if role_scope != "/" and role_scope not in scopes:
            fail("AUTHORITY_ROLE_BINDING")
        if (
            role["type"] not in ("BuiltInRole", "CustomRole")
            or not role["assignableScopes"]
        ):
            fail("AUTHORITY_ROLE_BINDING")
        e.string_list(role["assignableScopes"])
        if len(set(role["assignableScopes"])) != len(role["assignableScopes"]) or any(
            s != "/" and s not in scopes for s in role["assignableScopes"]
        ):
            fail("AUTHORITY_ROLE_SCOPE")
        permissions(role["permissions"])
    known = subject_types(subjects)
    referenced_roles = set()
    for assignment in arm["assignments"]:
        b.uuid(assignment["principalId"])
        if (
            assignment["principalType"] not in ("User", "Group", "ServicePrincipal")
            or assignment["scope"] not in scopes
        ):
            fail("AUTHORITY_SCOPE_BINDING")
        if authorization_id(assignment["id"], "roleAssignments") != assignment["scope"]:
            fail("AUTHORITY_SCOPE_BINDING")
        if (
            assignment["principalId"] in known
            and assignment["principalType"] != known[assignment["principalId"]]
        ):
            fail("AUTHORITY_PRINCIPAL_BINDING")
        referenced_roles.add(assignment["roleDefinitionId"])
    if referenced_roles != {r["id"] for r in arm["roles"]}:
        fail("AUTHORITY_ROLE_BINDING")
    for deny in arm["denies"]:
        if (
            deny["scope"] not in scopes
            or authorization_id(deny["id"], "denyAssignments") != deny["scope"]
        ):
            fail("AUTHORITY_SCOPE_BINDING")
        b.boolean(deny["doNotApplyToChildScopes"])
        b.boolean(deny["isSystemProtected"])
        permissions(deny["permissions"])
        for key in ("principals", "excludePrincipals"):
            deny_principals(deny[key], known)
        if not deny["principals"]:
            fail("AUTHORITY_DENY_BINDING")
        expected = {
            principal: {
                "principal_matches": any(
                    p["id"] in [ZERO, principal, *groups] for p in deny["principals"]
                ),
                "excluded": any(
                    p["id"] in [ZERO, principal, *groups]
                    for p in deny["excludePrincipals"]
                ),
                "scopes": [
                    s
                    for s in scopes
                    if normalized_id(s) == normalized_id(deny["scope"])
                    or (
                        not deny["doNotApplyToChildScopes"]
                        and inherited(deny["scope"], s, scopes)
                    )
                ],
            }
            for principal, groups in subjects.items()
        }
        if deny["applicability"] != expected:
            fail("AUTHORITY_DENY_BINDING")
    expected_pairs = {
        (s, k)
        for s in scopes
        for k in (
            "roleAssignments",
            "denyAssignments",
            "roleAssignmentScheduleInstances",
            "roleEligibilityScheduleInstances",
        )
    }
    expected_pairs |= {
        (s, "registrationAssignments")
        for s in scopes
        if re.fullmatch(
            r"/subscriptions/[^/]+(?:/resourceGroups/[^/]+)?", s, re.IGNORECASE
        )
    }
    inventories = arm["inventories"]
    if not isinstance(inventories, list) or len(inventories) != len(expected_pairs):
        fail("AUTHORITY_INVENTORY_BINDING")
    pairs = set()
    for inventory in inventories:
        b.exact(inventory, {"scope", "kind", "ids"})
        pair = (inventory["scope"], inventory["kind"])
        if pair in pairs:
            fail("AUTHORITY_DUPLICATE")
        pairs.add(pair)
        e.string_list(inventory["ids"])
        if inventory["kind"] in ("roleAssignments", "denyAssignments"):
            key = "assignments" if inventory["kind"] == "roleAssignments" else "denies"
            expected = sorted(
                r["id"]
                for r in arm[key]
                if inherited(r["scope"], inventory["scope"], scopes)
            )
            if inventory["ids"] != expected:
                fail("AUTHORITY_INVENTORY_BINDING")
        elif inventory["ids"]:
            fail("AUTHORITY_UNSUPPORTED")
    if pairs != expected_pairs:
        fail("AUTHORITY_INVENTORY_BINDING")
    if arm["effective"] != effective(
        arm, subjects, scopes, required_operations(target, workload["id"], search["id"])
    ):
        fail("AUTHORITY_EFFECTIVE_BINDING")
    graph = value["graph"]
    records(graph["resources"], set(SP_FIELDS))
    resources = {r["id"]: r for r in graph["resources"]}
    for resource in resources.values():
        validate_principal(resource, tenant, "Application")
    records(graph["outgoing"], {"principalId", "assignments"}, "principalId")
    if {o["principalId"] for o in graph["outgoing"]} != set(principals):
        fail("AUTHORITY_OUTGOING_BINDING")
    referenced = set()
    for outgoing in graph["outgoing"]:
        records(
            outgoing["assignments"], {"id", "principalId", "resourceId", "appRoleId"}
        )
        for assignment in outgoing["assignments"]:
            b.uuid(assignment["appRoleId"])
            resource = resources.get(assignment["resourceId"])
            if (
                assignment["principalId"] != outgoing["principalId"]
                or resource is None
                or assignment["appRoleId"] == ZERO
            ):
                fail("AUTHORITY_OUTGOING_BINDING")
            referenced.add(assignment["resourceId"])
            matches = [
                r for r in resource["appRoles"] if r["id"] == assignment["appRoleId"]
            ]
            if (
                len(matches) != 1
                or matches[0]["isEnabled"] is not True
                or "Application" not in matches[0].get("allowedMemberTypes", [])
            ):
                fail("AUTHORITY_OUTGOING_BINDING")
    records(graph["delegated"], {"grantId", "resourceId", "scopes"}, "grantId")
    expected = []
    for grant in external["grants"]:
        resource = resources.get(grant["resourceId"])
        names = grant["scope"].split()
        if resource is None or not names or len(set(names)) != len(names):
            fail("AUTHORITY_DELEGATED_BINDING")
        referenced.add(grant["resourceId"])
        for name in names:
            matches = [
                s for s in resource["oauth2PermissionScopes"] if s["value"] == name
            ]
            if len(matches) != 1 or matches[0]["isEnabled"] is not True:
                fail("AUTHORITY_DELEGATED_BINDING")
        expected.append(
            {
                "grantId": grant["id"],
                "resourceId": grant["resourceId"],
                "scopes": sorted(names),
            }
        )
    if graph["delegated"] != ordered(expected) or referenced != set(resources):
        fail("AUTHORITY_DELEGATED_BINDING")

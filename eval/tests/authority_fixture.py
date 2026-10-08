"""Explicit nonsecret authority transport fixtures; no live observations."""

import copy

import authority_state as a

WORKLOAD_CLIENT = "88888888-8888-8888-8888-888888888888"
WORKLOAD = "44444444-4444-4444-4444-444444444444"
SEARCH = "99999999-9999-9999-9999-999999999999"
SEARCH_CLIENT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
API_CLIENT = "66666666-6666-6666-6666-666666666666"
API_OBJECT = "77777777-7777-7777-7777-777777777777"
API = "33333333-3333-3333-3333-333333333333"
RESOURCE = "55555555-5555-5555-5555-555555555555"
ROLE = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
GROUP = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def principal(identity, client, tenant, kind="ManagedIdentity"):
    return {
        "id": identity,
        "appId": client,
        "accountEnabled": True,
        "appRoleAssignmentRequired": False,
        "servicePrincipalType": kind,
        "appOwnerOrganizationId": tenant if kind == "Application" else None,
        "appRoles": [],
        "oauth2PermissionScopes": [],
    }


def fixture(plane):
    target, app = plane["target"], plane["app"]
    tenant, subscription = target["tenant_id"], target["subscription_id"]
    root = "/providers/Microsoft.Management/managementGroups/" + tenant
    chain = [root, "/subscriptions/" + subscription]
    scopes = a.resource_scopes(target, chain)
    identity = next(iter(app["managed_identity"]["userAssignedIdentities"]))
    workload = principal(WORKLOAD, WORKLOAD_CLIENT, tenant)
    search = principal(SEARCH, SEARCH_CLIENT, tenant)
    api = principal(API, API_CLIENT, tenant, "Application")
    resource = principal(
        RESOURCE, "dddddddd-dddd-dddd-dddd-dddddddddddd", tenant, "Application"
    )
    resource["oauth2PermissionScopes"] = [
        {
            "id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            "value": "User.Read",
            "isEnabled": True,
            "type": "User",
        }
    ]
    resource["appRoles"] = [
        {
            "id": ROLE,
            "value": "User.Read.All",
            "isEnabled": True,
            "allowedMemberTypes": ["Application"],
        }
    ]
    policy = {
        "allowSharedKeyAccess": False,
        "allowBlobPublicAccess": False,
        "minimumTlsVersion": "TLS1_2",
        "publicNetworkAccess": "Enabled",
        "networkAcls": {
            "defaultAction": "Allow",
            "bypass": "None",
            "ipRules": [],
            "virtualNetworkRules": [],
        },
    }
    roles, assignments = [], []
    for index, (subject, scope) in enumerate(
        (
            (WORKLOAD, target["search_resource_id"]),
            (WORKLOAD, target["ai_resource_id"]),
            (WORKLOAD, target["tickets_resource_id"]),
            (SEARCH, target["ai_resource_id"]),
        ),
        1,
    ):
        suffix = f"00000000-0000-0000-0000-{index:012d}"
        role_id = (
            chain[-1] + "/providers/Microsoft.Authorization/roleDefinitions/" + suffix
        )
        roles.append(
            {
                "id": role_id,
                "type": "CustomRole",
                "assignableScopes": [chain[-1]],
                "permissions": [
                    {
                        "actions": ["*"],
                        "notActions": [],
                        "dataActions": ["*"],
                        "notDataActions": [],
                    }
                ],
            }
        )
        assignments.append(
            {
                "id": scope
                + "/providers/Microsoft.Authorization/roleAssignments/"
                + suffix,
                "scope": scope,
                "principalId": subject,
                "principalType": "ServicePrincipal",
                "roleDefinitionId": role_id,
            }
        )
    inventories = []
    for scope in scopes:
        for kind in (
            "roleAssignments",
            "denyAssignments",
            "roleAssignmentScheduleInstances",
            "roleEligibilityScheduleInstances",
        ):
            ids = (
                sorted(
                    r["id"]
                    for r in assignments
                    if a.inherited(r["scope"], scope, scopes)
                )
                if kind == "roleAssignments"
                else []
            )
            inventories.append({"scope": scope, "kind": kind, "ids": ids})
        if scope in (
            chain[-1],
            target["tickets_resource_id"].split("/providers/", 1)[0],
        ):
            inventories.append(
                {"scope": scope, "kind": "registrationAssignments", "ids": []}
            )
    arm = {
        "roles": a.ordered(roles),
        "assignments": a.ordered(assignments),
        "denies": [],
        "inventories": a.ordered(inventories),
    }
    arm["effective"] = a.effective(
        arm,
        {WORKLOAD: [], SEARCH: []},
        scopes,
        a.required_operations(target, WORKLOAD, SEARCH),
    )
    federation = [
        {
            "id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
            "name": "workload",
            "issuer": f"https://login.microsoftonline.com/{tenant}/v2.0",
            "subject": WORKLOAD,
            "audiences": ["api://AzureADTokenExchange"],
        }
    ]
    value = {
        "mode": a.MODE,
        "tenantId": tenant,
        "tickets": {
            "id": target["tickets_resource_id"],
            "endpoint": app["effective_environment"]["Tickets__ServiceUri"].rstrip("/"),
            "policy": policy,
        },
        "workload": {
            "resourceId": identity,
            "properties": {
                "clientId": WORKLOAD_CLIENT,
                "principalId": WORKLOAD,
                "tenantId": tenant,
            },
            "principal": workload,
        },
        "api": api,
        "searchIdentity": search,
        "federation": federation,
        "ancestry": chain,
        "scopes": scopes,
        "memberships": a.ordered(
            [
                {"principalId": WORKLOAD, "groups": []},
                {"principalId": SEARCH, "groups": []},
            ]
        ),
        "arm": arm,
        "graph": {
            "outgoing": a.ordered(
                [{"principalId": p, "assignments": []} for p in (API, WORKLOAD, SEARCH)]
            ),
            "delegated": [],
            "resources": [],
        },
    }
    responses = {
        target["tickets_resource_id"]: {
            "id": target["tickets_resource_id"],
            "type": "Microsoft.Storage/storageAccounts",
            "properties": policy
            | {"primaryEndpoints": {"table": value["tickets"]["endpoint"] + "/"}},
        },
        identity: {
            "id": identity,
            "type": "Microsoft.ManagedIdentity/userAssignedIdentities",
            "properties": value["workload"]["properties"],
        },
        "/providers/Microsoft.Management/getEntities": {
            "value": [
                {
                    "id": chain[-1],
                    "properties": {
                        "tenantId": tenant,
                        "parentNameChain": [tenant],
                        "parent": {"id": root},
                    },
                }
            ]
        },
        root: {
            "id": root,
            "properties": {"tenantId": tenant, "details": {"parent": None}},
        },
        chain[-1]: {
            "subscriptionId": subscription,
            "tenantId": tenant,
            "state": "Enabled",
        },
        "/v1.0/applications/" + API_OBJECT + "/federatedIdentityCredentials": {
            "value": federation
        },
    }
    for p in (workload, search, api, resource):
        responses["/v1.0/servicePrincipals/" + p["id"]] = p
    for p in (WORKLOAD, SEARCH):
        responses[f"/v1.0/servicePrincipals/{p}/transitiveMemberOf"] = {"value": []}
    for p in (API, WORKLOAD, SEARCH):
        responses[f"/v1.0/servicePrincipals/{p}/appRoleAssignments"] = {"value": []}
    for role in roles:
        responses[role["id"]] = {
            "id": role["id"],
            "type": "Microsoft.Authorization/roleDefinitions",
            "properties": {
                "roleName": "fixture",
                "description": "",
                "type": role["type"],
                "assignableScopes": role["assignableScopes"],
                "permissions": role["permissions"],
            },
        }
    for inventory in inventories:
        kind, scope = inventory["kind"], inventory["scope"]
        provider = (
            "Microsoft.ManagedServices"
            if kind == "registrationAssignments"
            else "Microsoft.Authorization"
        )
        records = []
        if kind == "roleAssignments":
            records = [
                {
                    "id": r["id"],
                    "name": r["id"].rsplit("/", 1)[1],
                    "type": "Microsoft.Authorization/roleAssignments",
                    "properties": {k: v for k, v in r.items() if k != "id"},
                }
                for r in assignments
                if r["id"] in inventory["ids"]
            ]
        responses[scope + "/providers/" + provider + "/" + kind] = {"value": records}
    return copy.deepcopy(value), copy.deepcopy(responses)

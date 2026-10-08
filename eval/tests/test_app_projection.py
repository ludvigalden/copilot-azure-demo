"""Live-shape coverage for the app projection on captured ARM GET forms.

The fixtures mirror the captured 2026-07-01 Container Apps GET responses
key for key: nine-key configuration, thirteen-key ingress, five-key
scale, container probes, lowercase identity dictionary keys, transport
"Auto", and explicit nulls. Both planes are covered — staging with
customDomains null and the explicit index/deployment environment,
production with the SniEnabled managed-certificate binding and the
omitted index/deployment entries the projection defaults. Everything
carried is configuration and identifiers; a GET response never contains
secret values.
"""

import copy

import pytest

import dependency_baseline as b

SUBSCRIPTION = "654085ac-1de7-4f78-9a90-473a05012c34"
TENANT = "2117ed34-480a-456c-82c8-1845bf21c3c5"
ENVIRONMENT = (
    "/subscriptions/654085ac-1de7-4f78-9a90-473a05012c34/resourceGroups/"
    "copaz-rg/providers/Microsoft.App/managedEnvironments/copaz-env"
)
CERTIFICATE = ENVIRONMENT + "/managedCertificates/copaz-cert"
IMAGE = "ghcr.io/ludvigalden/copilot-azure-demo@sha256:"
FQDN_SUFFIX = ".calmplant-5ad96f60.swedencentral.azurecontainerapps.io"
ALT_FQDN = "copaz-staging-alt" + FQDN_SUFFIX
KEYVAULT_URL = "https://demo-vault.vault.azure.net/secrets/tickets/0123456789abcdef"
CERTIFICATE_BINDING = [
    {
        "bindingType": "SniEnabled",
        "certificateId": CERTIFICATE,
        "name": "copilot-azure.demo.ludvigalden.com",
    }
]
LIVENESS_PROBE = [{"type": "Liveness", "httpGet": {"path": "/health", "port": 8080}}]
HTTP_RULE = [{"name": "rule", "http": {"metadata": {"concurrentRequests": "10"}}}]

FACTS = {
    "staging": {
        "group": "copaz-staging-rg",
        "app": "copaz-staging-app",
        "identity": "copaz-staging-identity",
        "client": "486a3ea3-6207-4f93-ab93-bfaa8b25d099",
        "principal": "fe6ee72c-9a7c-4d7e-8cb0-fec679180c70",
        "digest": "5ca8bbc1fd6357aa16a2e6d0e44936389fe61229c6f4461a5f457a9ee7964431",
        "revision": 16,
        "tickets": "https://copazstagingtickets.table.core.windows.net/",
        "index": "kb-staging",
        "deployment": "chat-staging",
        "custom_domains": None,
    },
    "production": {
        "group": "copaz-rg",
        "app": "copaz-app",
        "identity": "copaz-identity",
        "client": "4ee0f855-64aa-4858-b976-ac3e5f01ddea",
        "principal": "4122628c-a0c1-4d90-830d-cebcb4b69522",
        "digest": "7d3b492ef38e9352d05948c7f6ea6e9b2c15954900690bc68e636e2fc4a1d7a3",
        "revision": 12,
        "tickets": "https://copaztickets.table.core.windows.net/",
        "index": None,
        "deployment": None,
        "custom_domains": CERTIFICATE_BINDING,
    },
}


def live_app(name):
    """Build the captured raw ARM GET document for the named plane."""
    facts = FACTS[name]
    group, app = facts["group"], facts["app"]
    revision = f"{app}--{facts['revision']:07d}"
    root = f"/subscriptions/{SUBSCRIPTION}/"
    identity_key = (
        root
        + f"resourcegroups/{group}/providers/"
        + "Microsoft.ManagedIdentity/userAssignedIdentities/"
        + facts["identity"]
    )
    env = [
        {"name": "AZURE_CLIENT_ID", "value": facts["client"]},
        {"name": "Tickets__ServiceUri", "value": facts["tickets"]},
        {
            "name": "OpenAI__Endpoint",
            "value": "https://copaz-ai.cognitiveservices.azure.com/",
        },
        {"name": "Search__Endpoint", "value": "https://copaz-srch.search.windows.net"},
        {"name": "AzureAd__TenantId", "value": TENANT},
        {"name": "AzureAd__ClientId", "value": "1355363a-03ec-4e9e-a70d-557a785a0e3b"},
        {"name": "AzureAd__Scope", "value": "api://copaz-api/access_as_user"},
        {
            "name": "AzureAd__ClientCredentials__0__SourceType",
            "value": "SignedAssertionFromManagedIdentity",
        },
        {
            "name": "AzureAd__ClientCredentials__0__ManagedIdentityClientId",
            "value": facts["client"],
        },
    ]
    if facts["index"] is not None:
        env.append({"name": "OpenAI__DeploymentName", "value": facts["deployment"]})
        env.append({"name": "Search__IndexName", "value": facts["index"]})
    return {
        "id": root
        + f"resourceGroups/{group}/providers/Microsoft.App/containerapps/{app}",
        "identity": {
            "type": "UserAssigned",
            "userAssignedIdentities": {
                identity_key: {
                    "clientId": facts["client"],
                    "principalId": facts["principal"],
                }
            },
        },
        "properties": {
            "configuration": {
                "activeRevisionsMode": "Single",
                "dapr": None,
                "identitySettings": [],
                "ingress": {
                    "additionalPortMappings": None,
                    "allowInsecure": False,
                    "clientCertificateMode": None,
                    "corsPolicy": None,
                    "customDomains": copy.deepcopy(facts["custom_domains"]),
                    "exposedPort": 0,
                    "external": True,
                    "fqdn": app + FQDN_SUFFIX,
                    "ipSecurityRestrictions": [],
                    "stickySessions": None,
                    "targetPort": 8080,
                    "traffic": [{"latestRevision": True, "weight": 100}],
                    "transport": "Auto",
                },
                "maxInactiveRevisions": 0,
                "registries": None,
                "runtime": None,
                "secrets": None,
                "service": None,
            },
            "environmentId": ENVIRONMENT,
            "latestReadyRevisionName": revision,
            "latestRevisionName": revision,
            "managedEnvironmentId": ENVIRONMENT,
            "template": {
                "containers": [
                    {
                        "env": env,
                        "image": IMAGE + facts["digest"],
                        "name": "it-support",
                        "probes": [],
                        "resources": {
                            "cpu": 0.5,
                            "ephemeralStorage": "2Gi",
                            "memory": "1Gi",
                        },
                    }
                ],
                "initContainers": None,
                "revisionSuffix": "",
                "scale": {
                    "cooldownPeriod": 300,
                    "maxReplicas": 1,
                    "minReplicas": None,
                    "pollingInterval": 30,
                    "rules": None,
                },
                "serviceBinds": None,
                "terminationGracePeriodSeconds": None,
                "volumes": [],
            },
            "workloadProfileName": "Consumption",
        },
    }


def raw_identity(name):
    """The raw GET identity block for the named plane's facts."""
    facts = FACTS[name]
    return {
        "type": "UserAssigned",
        "userAssignedIdentities": {
            f"/subscriptions/{SUBSCRIPTION}/resourcegroups/{facts['group']}"
            "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/"
            f"{facts['identity']}": {
                "clientId": facts["client"],
                "principalId": facts["principal"],
            }
        },
    }


def canonical_identity_key(name):
    facts = FACTS[name]
    return (
        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{facts['group']}"
        "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/"
        f"{facts['identity']}"
    )


def set_path(document, path, value):
    """Return a deep copy with the value planted at the tuple path."""
    target = copy.deepcopy(document)
    node = target
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = copy.deepcopy(value)
    return target


def set_env(document, name, value):
    """Return a deep copy with one named environment entry's value set."""
    target = copy.deepcopy(document)
    for item in target["properties"]["template"]["containers"][0]["env"]:
        if item["name"] == name:
            item["value"] = value
            return target
    raise AssertionError(f"missing environment entry: {name}")


def canonical_projection(document):
    return b.canonical(b.app_projection(document))


@pytest.mark.parametrize("plane", ["staging", "production"])
def test_live_get_projects_cleanly_on_both_planes(plane):
    result = b.app_projection(live_app(plane))
    assert result["ingress"]["transport"] == "auto"
    assert result["ingress"]["customDomains"] == copy.deepcopy(
        FACTS[plane]["custom_domains"]
    )
    assert result["ingress"]["fqdn"] == FACTS[plane]["app"] + FQDN_SUFFIX
    assert result["scale"] == {
        "minReplicas": None,
        "maxReplicas": 1,
        "rules": None,
        "cooldownPeriod": 300,
        "pollingInterval": 30,
    }
    assert result["identity_settings"] == []
    assert result["runtime"] is None
    assert result["container_configuration"]["probes"] == []
    assert result["secret_references"] == {}
    assert (
        result["revision"] == f"{FACTS[plane]['app']}--{FACTS[plane]['revision']:07d}"
    )
    key = next(iter(result["managed_identity"]["userAssignedIdentities"]))
    assert key == canonical_identity_key(plane)
    assert "/resourceGroups/" in key and "/resourcegroups/" not in key
    index = result["effective_environment"]["Search__IndexName"]
    deployment = result["effective_environment"]["OpenAI__DeploymentName"]
    if plane == "staging":
        assert index == "kb-staging"
        assert deployment == "chat-staging"
    else:
        assert index == "kb"
        assert deployment == "chat"


def test_projection_is_deterministic():
    for plane in ("staging", "production"):
        first = b.app_projection(live_app(plane))
        second = b.app_projection(live_app(plane))
        assert first == second


@pytest.mark.parametrize(
    "path,value",
    [
        (
            ("properties", "configuration", "identitySettings"),
            [{"identity": canonical_identity_key("staging"), "lifecycle": "All"}],
        ),
        (
            ("properties", "configuration", "runtime"),
            {"name": "docker", "version": "1.0"},
        ),
        (("properties", "template", "containers", 0, "probes"), LIVENESS_PROBE),
        (("properties", "template", "scale", "minReplicas"), 1),
        (("properties", "template", "scale", "maxReplicas"), 2),
        (("properties", "template", "scale", "rules"), HTTP_RULE),
        (("properties", "template", "scale", "cooldownPeriod"), 60),
        (
            ("properties", "configuration", "ingress", "customDomains"),
            CERTIFICATE_BINDING,
        ),
        (("properties", "configuration", "ingress", "transport"), "Http"),
        (("properties", "configuration", "ingress", "fqdn"), ALT_FQDN),
        (("properties", "configuration", "ingress", "external"), False),
        (("properties", "configuration", "ingress", "allowInsecure"), True),
        (("properties", "configuration", "ingress", "targetPort"), 8081),
        (
            ("properties", "template", "containers", 0, "image"),
            IMAGE + "f" * 64,
        ),
    ],
)
def test_projected_change_fires_drift(path, value):
    # compare_planes raises DEPENDENCY_DRIFT exactly when the canonical
    # projections differ, so this inequality is the drift predicate.
    assert canonical_projection(set_path(live_app("staging"), path, value)) != (
        canonical_projection(live_app("staging"))
    )


def test_identity_change_fires_drift():
    swapped = set_path(live_app("staging"), ("identity",), raw_identity("production"))
    assert canonical_projection(swapped) != canonical_projection(live_app("staging"))


def test_scope_change_fires_drift():
    swapped = set_env(live_app("staging"), "AzureAd__Scope", "api://other-api/x")
    assert canonical_projection(swapped) != canonical_projection(live_app("staging"))


def test_index_name_change_fires_drift():
    swapped = set_env(live_app("staging"), "Search__IndexName", "kb")
    assert canonical_projection(swapped) != canonical_projection(live_app("staging"))


@pytest.mark.parametrize("plane", ["staging", "production"])
def test_custom_domains_flip_fires_drift(plane):
    flipped_value = None if plane == "production" else CERTIFICATE_BINDING
    base = live_app(plane)
    path = ("properties", "configuration", "ingress", "customDomains")
    assert canonical_projection(set_path(base, path, flipped_value)) != (
        canonical_projection(base)
    )


@pytest.mark.parametrize(
    "path,null,full",
    [
        (("properties", "template", "scale", "minReplicas"), None, 1),
        (("properties", "template", "scale", "rules"), None, HTTP_RULE),
        (
            ("properties", "configuration", "runtime"),
            None,
            {"name": "docker", "version": "1.0"},
        ),
    ],
)
def test_null_value_flips_on_projected_keys(path, null, full):
    # Projected nulls are observable in both directions: null to a value
    # and the value back to null each change the canonical projection.
    for before, after in ((null, full), (full, null)):
        assert canonical_projection(set_path(live_app("staging"), path, before)) != (
            canonical_projection(set_path(live_app("staging"), path, after))
        )


@pytest.mark.parametrize(
    "path,value",
    [
        (("properties", "template", "serviceBinds"), [{"service": "copaz-bind"}]),
        (("properties", "template", "terminationGracePeriodSeconds"), 30),
        (
            ("properties", "configuration", "ingress", "additionalPortMappings"),
            [{"targetPort": 9090, "exposedPort": 9090, "external": True}],
        ),
        (
            ("properties", "configuration", "ingress", "clientCertificateMode"),
            "Require",
        ),
        (
            ("properties", "configuration", "ingress", "corsPolicy"),
            {"allowedOrigins": ["*"]},
        ),
        (
            ("properties", "configuration", "ingress", "stickySessions"),
            {"affinity": "sticky"},
        ),
        (("properties", "configuration", "ingress", "exposedPort"), 8080),
        (
            ("properties", "configuration", "ingress", "ipSecurityRestrictions"),
            [{"action": "Allow", "ipAddressRange": "0.0.0.0/0", "name": "all"}],
        ),
        (("properties", "configuration", "ingress", "transport"), "Tcp"),
        (("properties", "configuration", "dapr"), {"enabled": True}),
        (("properties", "configuration", "dapr"), "enabled"),
        (("properties", "configuration", "service"), {"type": "Redis"}),
    ],
)
def test_classified_deviation_fails_closed(path, value):
    with pytest.raises(b.BaselineError, match="APP_UNOBSERVABLE"):
        b.app_projection(set_path(live_app("staging"), path, value))


@pytest.mark.parametrize(
    "scope",
    ["https://copaz-api", "copaz-api/access_as_user", "api://copaz_api/x y"],
)
def test_malformed_scope_fails_invalid_identity(scope):
    with pytest.raises(b.BaselineError, match="INVALID_IDENTITY"):
        b.app_projection(set_env(live_app("staging"), "AzureAd__Scope", scope))


def test_null_secrets_are_absence_not_a_crash():
    result = b.app_projection(live_app("staging"))
    assert result["secret_references"] == {}


def test_non_list_secrets_fail():
    path = ("properties", "configuration", "secrets")
    with pytest.raises(b.BaselineError, match="SECRET_VERSION_UNOBSERVABLE"):
        b.app_projection(set_path(live_app("staging"), path, "tickets"))


def test_plain_secret_value_fails():
    # An entry carrying a plain value instead of a KeyVault reference is
    # rejected before any secret material could enter the projection.
    path = ("properties", "configuration", "secrets")
    with pytest.raises(b.BaselineError, match="MISSING_IDENTITY"):
        b.app_projection(
            set_path(
                live_app("staging"),
                path,
                [{"name": "tickets", "value": "a-plain-secret"}],
            )
        )


def test_malformed_keyvault_url_fails():
    path = ("properties", "configuration", "secrets")
    with pytest.raises(b.BaselineError, match="SECRET_VERSION_UNOBSERVABLE"):
        b.app_projection(
            set_path(
                live_app("staging"),
                path,
                [{"name": "tickets", "keyVaultUrl": "https://example.com/s/t/1"}],
            )
        )


def test_keyvault_secret_reference_projects():
    path = ("properties", "configuration", "secrets")
    result = b.app_projection(
        set_path(
            live_app("staging"),
            path,
            [{"name": "tickets", "keyVaultUrl": KEYVAULT_URL}],
        )
    )
    assert result["secret_references"] == {"tickets": KEYVAULT_URL}


def test_identity_key_casing_normalizes_for_exact_comparison():
    lower = live_app("staging")
    stored_key = canonical_identity_key("staging")
    lifted = set_path(
        lower,
        ("identity", "userAssignedIdentities"),
        {
            stored_key: lower["identity"]["userAssignedIdentities"][
                next(iter(lower["identity"]["userAssignedIdentities"]))
            ]
        },
    )
    assert (
        b.app_projection(lower)["managed_identity"]
        == b.app_projection(lifted)["managed_identity"]
    )


def test_stored_noncanonical_transport_fails():
    result = b.app_projection(live_app("staging"))
    result["ingress"]["transport"] = "Auto"
    with pytest.raises(b.BaselineError, match="APP_UNOBSERVABLE"):
        b.validate_app(result)


def test_stored_noncanonical_identity_fails():
    result = b.app_projection(live_app("staging"))
    identities = result["managed_identity"]["userAssignedIdentities"]
    key = next(iter(identities))
    lowered = key.replace("/resourceGroups/", "/resourcegroups/")
    result["managed_identity"]["userAssignedIdentities"] = {lowered: identities[key]}
    with pytest.raises(b.BaselineError, match="IDENTITY_UNOBSERVABLE"):
        b.validate_app(result)

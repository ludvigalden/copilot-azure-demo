locals {
  # A shared-resources run consumes another environment's search service,
  # AI Services account, container apps environment, and API application
  # instead of owning them; the owner apply runs with the variable empty.
  owns_shared = var.shared_prefix == ""

  # A shared run owns a resource group of its own while consuming the
  # shared services from the owner's group.
  resource_group_name = local.owns_shared ? "${var.prefix}-rg" : "${var.shared_prefix}-rg"
  environment_name    = "${var.prefix}-env"
  container_app_name  = "${var.prefix}-app"
  identity_name       = "${var.prefix}-identity"
  search_name         = "${var.prefix}-srch"
  ai_name             = "${var.prefix}-ai"
  tickets_account     = "${replace(var.prefix, "-", "")}tickets"

  # Name of the one container in the container app.
  container_name = "it-support"

  # Exactly one side of each pair exists: the owned resource or, on a
  # shared run, the data source of the other environment's resource. The
  # splats are per attribute so a sensitive sibling (the search service's
  # query keys) never marks the shared values sensitive; one() on the
  # empty side yields null instead of indexing a missing instance.
  search_name_effective = one(concat(
    azurerm_search_service.search[*].name,
    data.azurerm_search_service.shared[*].name,
  ))
  search_service_id = one(concat(
    azurerm_search_service.search[*].id,
    data.azurerm_search_service.shared[*].id,
  ))
  app_environment_id = one(concat(
    azurerm_container_app_environment.env[*].id,
    data.azurerm_container_app_environment.shared[*].id,
  ))
  app_environment_domain = one(concat(
    azurerm_container_app_environment.env[*].default_domain,
    data.azurerm_container_app_environment.shared[*].default_domain,
  ))
  api_application_id = one(concat(
    azuread_application.api[*].id,
    data.azuread_application.shared_api[*].id,
  ))
  api_application_client_id = one(concat(
    azuread_application.api[*].client_id,
    data.azuread_application.shared_api[*].client_id,
  ))
  api_application_scope_ids = one(concat(
    azuread_application.api[*].oauth2_permission_scope_ids,
    data.azuread_application.shared_api[*].oauth2_permission_scope_ids,
  ))

  search_endpoint = "https://${local.search_name_effective}.search.windows.net"

  # On a shared run that cannot list the shared account's keys, the
  # account data source is skipped entirely and the endpoint and id are
  # composed from names; the values match what the service provisions.
  openai_endpoint = local.owns_shared ? one(azurerm_cognitive_account.ai[*].endpoint) : "https://${var.shared_prefix}-ai.cognitiveservices.azure.com/"
  ai_account_id = local.owns_shared ? one(azurerm_cognitive_account.ai[*].id) : format(
    "/subscriptions/%s/resourceGroups/%s/providers/Microsoft.CognitiveServices/accounts/%s-ai",
    data.azurerm_client_config.current.subscription_id,
    local.resource_group_name,
    var.shared_prefix,
  )

  # The vectorizer's key: the owned account's key when this root owns it;
  # on a shared run either the key passed out of band (which keeps the
  # data source, and with it the key-listing call, out of the plan) or the
  # data source read for an identity that may list keys.
  vectorizer_api_key = local.owns_shared ? one(azurerm_cognitive_account.ai[*].primary_access_key) : (var.shared_ai_account_key != "" ? var.shared_ai_account_key : one(data.azurerm_cognitive_account.shared[*].primary_access_key))

  # The application ID URI is a stable name rather than the client ID,
  # because Terraform cannot reference the client ID a resource generates.
  # The API publishes the matching scope string to the SPA through
  # /api/config, where Terraform supplies it as a setting. The shared
  # application's name wins when one exists; the owning prefix fills in
  # otherwise (coalesce skips the null of the absent data source).
  app_id_uri = "api://${coalesce(one(data.azuread_application.shared_api[*].display_name), var.prefix)}-api"

  # The ingress FQDN of an external container app is its name under the
  # environment's default domain. Composing it here instead of reading the
  # app resource avoids a reference cycle: the Entra app registration's
  # redirect URIs contain this host, while the app's settings contain the
  # registration's client ID.
  app_fqdn   = "${local.container_app_name}.${local.app_environment_domain}"
  public_url = var.public_hostname != "" ? "https://${var.public_hostname}/" : ""

  # The bot's messaging endpoint, registered with Bot Framework: the
  # custom hostname when bound, otherwise the container app's default
  # domain, always at the agent's /api/bot/messages route.
  bot_endpoint = "https://${var.public_hostname != "" ? var.public_hostname : local.app_fqdn}/api/bot/messages"

  # A shared run's host joins the owned application's redirect URIs
  # through the extra list, which the owner carries for it.
  redirect_uris = concat(compact([
    local.public_url,
    "http://localhost:5173/",
    "https://${local.app_fqdn}/",
  ]), var.extra_redirect_uris)

  # Configuration overrides only a shared run passes to its container app:
  # the image reads its own literals on the owner, whose app gains
  # nothing and whose plan stays empty.
  app_config_overrides = local.owns_shared ? {} : {
    "Search__IndexName"      = var.index_name
    "OpenAI__DeploymentName" = var.chat_deployment_name
  }

  servicenow_configured = var.servicenow_instance_url != ""

  servicenow_secrets = local.servicenow_configured ? {
    "servicenow-instance-url" = var.servicenow_instance_url
    "servicenow-username"     = var.servicenow_username
    "servicenow-password"     = var.servicenow_password
  } : {}

  servicenow_env = local.servicenow_configured ? {
    "ServiceNow__InstanceUrl" = "servicenow-instance-url"
    "ServiceNow__Username"    = "servicenow-username"
    "ServiceNow__Password"    = "servicenow-password"
  } : {}
}

data "azurerm_resource_group" "app" {
  name = local.resource_group_name
}

data "azurerm_client_config" "current" {}

data "azurerm_search_service" "shared" {
  count = local.owns_shared ? 0 : 1

  name                = "${var.shared_prefix}-srch"
  resource_group_name = local.resource_group_name
}

data "azurerm_container_app_environment" "shared" {
  count = local.owns_shared ? 0 : 1

  name                = "${var.shared_prefix}-env"
  resource_group_name = local.resource_group_name
}

data "azuread_application" "shared_api" {
  count = local.owns_shared ? 0 : 1

  display_name = "${var.shared_prefix}-api"
}

# Read only when the vectorizer key is not passed in: the read lists the
# shared account's keys, which the local loop's read-only identity may not.
data "azurerm_cognitive_account" "shared" {
  count = (local.owns_shared || var.shared_ai_account_key != "") ? 0 : 1

  name                = "${var.shared_prefix}-ai"
  resource_group_name = local.resource_group_name
}

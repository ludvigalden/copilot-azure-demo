locals {
  resource_group_name = "${var.prefix}-rg"
  environment_name    = "${var.prefix}-env"
  container_app_name  = "${var.prefix}-app"
  identity_name       = "${var.prefix}-identity"
  search_name         = "${var.prefix}-srch"
  ai_name             = "${var.prefix}-ai"
  tickets_account     = "${replace(var.prefix, "-", "")}tickets"

  # Name of the one container in the container app.
  container_name = "it-support"

  search_endpoint = "https://${local.search_name}.search.windows.net"

  # The application ID URI is a stable name rather than the client ID,
  # because Terraform cannot reference the client ID a resource generates.
  # The API publishes the matching scope string to the SPA through
  # /api/config, where Terraform supplies it as a setting.
  app_id_uri = "api://${var.prefix}-api"

  # The ingress FQDN of an external container app is its name under the
  # environment's default domain. Composing it here instead of reading the
  # app resource avoids a reference cycle: the Entra app registration's
  # redirect URIs contain this host, while the app's settings contain the
  # registration's client ID.
  app_fqdn   = "${local.container_app_name}.${azurerm_container_app_environment.env.default_domain}"
  public_url = var.public_hostname != "" ? "https://${var.public_hostname}/" : ""

  # The bot's messaging endpoint, registered with Bot Framework: the
  # custom hostname when bound, otherwise the container app's default
  # domain, always at the agent's /api/bot/messages route.
  bot_endpoint = "https://${var.public_hostname != "" ? var.public_hostname : local.app_fqdn}/api/bot/messages"

  redirect_uris = compact([
    local.public_url,
    "http://localhost:5173/",
    "https://${local.app_fqdn}/",
  ])

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

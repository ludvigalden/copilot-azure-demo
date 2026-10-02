# The workload identity of the running API: data-plane roles on Table
# Storage, AI Search and Azure OpenAI, and the federated credential behind
# the on-behalf-of exchange to Microsoft Graph.
resource "azurerm_user_assigned_identity" "app" {
  name                = local.identity_name
  resource_group_name = data.azurerm_resource_group.app.name
  location            = data.azurerm_resource_group.app.location
}

# One registration plays both parts: the SPA client that signs users in and
# the API resource that accepts their tokens (the access_as_user scope).
resource "azuread_application" "api" {
  display_name     = "${var.prefix}-api"
  sign_in_audience = "AzureADMyOrg"
  identifier_uris  = [local.app_id_uri]

  api {
    # Version 2 lets the application ID URI be a stable name; the tenant's
    # default app policy otherwise only accepts URIs containing the app ID
    # or the tenant ID.
    requested_access_token_version = 2

    oauth2_permission_scope {
      id                         = "a2b1c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d" # stable, referenced by client apps
      admin_consent_display_name = "Access the IT support API"
      admin_consent_description  = "Allows the app to call the IT support API on behalf of the signed-in user."
      user_consent_display_name  = "Access the IT support API"
      user_consent_description   = "Allows the app to call the IT support API on behalf of the signed-in user."
      value                      = "access_as_user"
      type                       = "User"
    }
  }

  single_page_application {
    redirect_uris = local.redirect_uris
  }

  required_resource_access {
    resource_app_id = "00000003-0000-0000-c000-000000000000" # Microsoft Graph
    resource_access {
      id   = "e1fe6dd8-ba31-4d61-89e7-88639da4683d" # User.Read (delegated)
      type = "Scope"
    }
  }
}

# The API is a token resource, so its service principal must exist for
# tokens to be issued against it.
resource "azuread_service_principal" "api" {
  client_id       = azuread_application.api.client_id
  account_enabled = true
}

# Secretless on-behalf-of: the container app's managed identity signs the
# client assertion that Microsoft Identity Web exchanges for a Graph token.
resource "azuread_application_federated_identity_credential" "api_managed_identity" {
  application_id = azuread_application.api.id
  display_name   = "managed-identity"
  description    = "The container app's managed identity acts as this application for on-behalf-of calls."
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://login.microsoftonline.com/${data.azurerm_client_config.current.tenant_id}/v2.0"
  subject        = azurerm_user_assigned_identity.app.principal_id
}

# The client application a Copilot Studio custom connector uses; it calls
# the API with end-user credentials. Its federated credential is added once
# the connector has been saved and has produced its issuer and subject.
resource "azuread_application" "connector_client" {
  display_name     = "${var.prefix}-connector"
  sign_in_audience = "AzureADMyOrg"

  required_resource_access {
    resource_app_id = azuread_application.api.client_id
    resource_access {
      id   = azuread_application.api.oauth2_permission_scope_ids["access_as_user"]
      type = "Scope"
    }
  }
}

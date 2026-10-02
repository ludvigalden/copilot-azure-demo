resource "azurerm_container_app_environment" "env" {
  name                = local.environment_name
  resource_group_name = data.azurerm_resource_group.app.name
  location            = data.azurerm_resource_group.app.location

  # The service provisions this profile by default; declaring it keeps the
  # second plan free of drift.
  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
  }
}

# One container serves the SPA and the API; the image workflow replaces the
# placeholder image on every deploy. Consumption plan, scaled to zero.
resource "azurerm_container_app" "app" {
  name                         = local.container_app_name
  container_app_environment_id = azurerm_container_app_environment.env.id
  resource_group_name          = data.azurerm_resource_group.app.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  template {
    min_replicas = 0
    max_replicas = 1

    container {
      name   = local.container_name
      image  = "mcr.microsoft.com/k8se/quickstart:latest"
      cpu    = 0.5
      memory = "1Gi"

      # DefaultAzureCredential picks the workload identity up through this
      # well-known variable.
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.app.client_id
      }
      env {
        name  = "Tickets__ServiceUri"
        value = azurerm_storage_account.tickets.primary_table_endpoint
      }
      env {
        name  = "OpenAI__Endpoint"
        value = azurerm_cognitive_account.ai.endpoint
      }
      env {
        name  = "Search__Endpoint"
        value = local.search_endpoint
      }
      env {
        name  = "AzureAd__TenantId"
        value = data.azurerm_client_config.current.tenant_id
      }
      env {
        name  = "AzureAd__ClientId"
        value = azuread_application.api.client_id
      }
      # Tokens name this URI as their audience, so the API must accept it.
      env {
        name  = "AzureAd__Audience"
        value = local.app_id_uri
      }
      # The application ID URI is a stable name, so the scope string the SPA
      # requests is published here instead of being derived from the ID.
      env {
        name  = "AzureAd__Scope"
        value = "${local.app_id_uri}/access_as_user"
      }
      # Secretless on-behalf-of to Microsoft Graph: the managed identity is
      # the client credential, via its federated credential on the app.
      env {
        name  = "AzureAd__ClientCredentials__0__SourceType"
        value = "SignedAssertionFromManagedIdentity"
      }
      env {
        name  = "AzureAd__ClientCredentials__0__ManagedIdentityClientId"
        value = azurerm_user_assigned_identity.app.client_id
      }

      dynamic "env" {
        for_each = local.servicenow_env
        content {
          name        = env.key
          secret_name = env.value
        }
      }
    }
  }

  dynamic "secret" {
    for_each = local.servicenow_secrets
    content {
      name  = secret.key
      value = secret.value
    }
  }

  ingress {
    target_port      = 8080
    external_enabled = true
    transport        = "auto"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  lifecycle {
    # Deployments replace the image; Terraform owns the shape around it.
    ignore_changes = [template[0].container[0].image]
  }
}

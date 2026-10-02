# One-time bootstrap: the Terraform state account and the CD identity that
# GitHub Actions assumes over OpenID Connect. Applied locally by the
# repository owner, then migrated to its own remote state.

terraform {
  required_version = ">= 1.9"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.7"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.10"
    }
    azapi = {
      source  = "Azure/azapi"
      version = "~> 2.13"
    }
  }

  # Migrated to remote state after the first local apply. The state resource
  # group and account names are passed on the command line with
  # -backend-config, so the same file drives local and workflow runs.
  backend "azurerm" {
    use_azuread_auth = true
    container_name   = "tfstate"
    key              = "bootstrap.tfstate"
  }
}

provider "azurerm" {
  features {}

  resource_providers_to_register = [
    "Microsoft.App",
    "Microsoft.CognitiveServices",
    "Microsoft.Search",
    "Microsoft.Storage",
    "Microsoft.ManagedIdentity",
  ]
}

locals {
  location      = "swedencentral"
  state_account = "${replace(var.prefix, "-", "")}tfstate"
}

data "azuread_client_config" "current" {}

data "azurerm_client_config" "current" {}

data "azuread_service_principal" "graph" {
  client_id = "00000003-0000-0000-c000-000000000000" # Microsoft Graph
}

# Dynamics CRM has no service principal in a fresh tenant; adopt it if one
# exists and create it otherwise, so the CD identity can hold the delegated
# user_impersonation grant that solution imports need.
resource "azuread_service_principal" "dynamics_crm" {
  client_id    = "00000007-0000-0000-c000-000000000000" # Dynamics CRM
  use_existing = true
}

resource "azurerm_resource_group" "state" {
  name     = "${var.prefix}-tfstate"
  location = local.location
}

# The application resource group is created here so the CD identity's grants
# can be scoped to it; the main root adopts it by data source and owns
# everything inside it.
resource "azurerm_resource_group" "app" {
  name     = "${var.prefix}-rg"
  location = local.location
}

resource "azurerm_storage_account" "state" {
  name                            = local.state_account
  resource_group_name             = azurerm_resource_group.state.name
  location                        = azurerm_resource_group.state.location
  account_kind                    = "StorageV2"
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false
  allow_nested_items_to_be_public = false
}

# Created through the management API because the account rejects shared-key
# access, which is what the data-plane container resource would need.
resource "azapi_resource" "state_container" {
  type      = "Microsoft.Storage/storageAccounts/blobServices/containers@2024-01-01"
  name      = "tfstate"
  parent_id = "${azurerm_storage_account.state.id}/blobServices/default"
}

resource "azuread_application" "cd" {
  display_name     = "${var.prefix}-cd"
  sign_in_audience = "AzureADMyOrg"
}

resource "azuread_service_principal" "cd" {
  client_id       = azuread_application.cd.client_id
  account_enabled = true
}

resource "azuread_application_federated_identity_credential" "cd" {
  application_id = azuread_application.cd.id
  display_name   = "github-actions-demo"
  description    = "GitHub Actions deploys from the demo environment of ${var.github_repository}."
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = "repo:${var.github_repository}:environment:demo"
}

# GitHub's environment-scoped OIDC subjects also carry the immutable owner
# and repository IDs alongside the names; both forms are trusted.
resource "azuread_application_federated_identity_credential" "cd_ids" {
  application_id = azuread_application.cd.id
  display_name   = "github-actions-demo-ids"
  description    = "The numeric subject form GitHub puts on environment-scoped tokens for ${var.github_repository}."
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = "https://token.actions.githubusercontent.com"
  subject        = "repo:ludvigalden@30798446/copilot-azure-demo@1399509581:environment:demo"
}

resource "azurerm_role_assignment" "cd_contributor" {
  scope                = azurerm_resource_group.app.id
  role_definition_name = "Contributor"
  principal_id         = azuread_service_principal.cd.object_id
}

# Lets the CD identity create and change role assignments on the resources
# inside the application resource group, which the main root does.
resource "azurerm_role_assignment" "cd_rbac_admin" {
  scope                = azurerm_resource_group.app.id
  role_definition_name = "Role Based Access Control Administrator"
  principal_id         = azuread_service_principal.cd.object_id
}

resource "azurerm_role_assignment" "cd_state_blob" {
  scope                = "${azurerm_storage_account.state.id}/blobServices/default/containers/tfstate"
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azuread_service_principal.cd.object_id
}

# The owner applies and reads Terraform state locally.
resource "azurerm_role_assignment" "owner_state_blob" {
  scope                = "${azurerm_storage_account.state.id}/blobServices/default/containers/tfstate"
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = data.azuread_client_config.current.object_id
}

# Lets the CD identity manage the app registrations and service principals
# that the main root creates; ownership-based, so it grants nothing beyond
# the applications this pipeline creates itself.
resource "azuread_app_role_assignment" "cd_graph_owned_apps" {
  app_role_id         = data.azuread_service_principal.graph.app_role_ids["Application.ReadWrite.OwnedBy"]
  principal_object_id = azuread_service_principal.cd.object_id
  resource_object_id  = data.azuread_service_principal.graph.object_id
}

resource "azuread_service_principal_delegated_permission_grant" "cd_dynamics_crm" {
  claim_values                         = ["user_impersonation"]
  service_principal_object_id          = azuread_service_principal.cd.object_id
  resource_service_principal_object_id = azuread_service_principal.dynamics_crm.object_id
}

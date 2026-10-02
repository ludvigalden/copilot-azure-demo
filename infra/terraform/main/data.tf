# Ticket store: one small dedicated account, separate from the Terraform
# state account. The table itself is created by the API on first use.
resource "azurerm_storage_account" "tickets" {
  name                            = local.tickets_account
  resource_group_name             = data.azurerm_resource_group.app.name
  location                        = data.azurerm_resource_group.app.location
  account_kind                    = "StorageV2"
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false
  allow_nested_items_to_be_public = false
}

resource "azurerm_role_assignment" "tickets_table_app" {
  scope                = azurerm_storage_account.tickets.id
  role_definition_name = "Storage Table Data Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

resource "azurerm_search_service" "search" {
  name                = local.search_name
  resource_group_name = data.azurerm_resource_group.app.name
  location            = data.azurerm_resource_group.app.location
  sku                 = var.search_sku
  replica_count       = 1

  # Accepts Entra identities on the data plane (role assignments below) and
  # keeps API keys accepted for the connections that still need one.
  authentication_failure_mode = "http401WithBearerChallenge"
}

resource "azurerm_role_assignment" "search_documents_app" {
  scope                = azurerm_search_service.search.id
  role_definition_name = "Search Index Data Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

resource "azurerm_role_assignment" "search_documents_cd" {
  scope                = azurerm_search_service.search.id
  role_definition_name = "Search Index Data Contributor"
  principal_id         = var.cd_principal_object_id
}

# Azure OpenAI (Foundry). Key access stays enabled for exactly one consumer:
# the index vectorizer below, whose key Terraform moves into the index
# definition itself. Every other caller uses Entra identities and RBAC.
resource "azurerm_cognitive_account" "ai" {
  name                  = local.ai_name
  resource_group_name   = data.azurerm_resource_group.app.name
  location              = data.azurerm_resource_group.app.location
  kind                  = "AIServices"
  sku_name              = "S0"
  custom_subdomain_name = local.ai_name
  local_auth_enabled    = true
}

resource "azurerm_cognitive_deployment" "chat" {
  name                 = "chat"
  cognitive_account_id = azurerm_cognitive_account.ai.id

  model {
    format  = "OpenAI"
    name    = "gpt-4.1-mini"
    version = "2025-04-14"
  }

  sku {
    # Regional Standard rather than DataZoneStandard: the data-zone quota
    # for this model is 0 on the subscription, while regional capacity is
    # available. Residency is unchanged for a Sweden Central deployment.
    name     = "Standard"
    capacity = 1
  }
}

resource "azurerm_cognitive_deployment" "embedding" {
  name                 = "embedding"
  cognitive_account_id = azurerm_cognitive_account.ai.id

  model {
    format  = "OpenAI"
    name    = "text-embedding-3-small"
    version = "1"
  }

  sku {
    name     = "DataZoneStandard"
    capacity = 1
  }
}

resource "azurerm_role_assignment" "openai_user_app" {
  scope                = azurerm_cognitive_account.ai.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

resource "azurerm_role_assignment" "openai_user_cd" {
  scope                = azurerm_cognitive_account.ai.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = var.cd_principal_object_id
}

# The owner manages the index data plane locally (the apply identity reads
# and writes the index directly) and can verify the data-plane paths. In
# GitHub Actions the CD identity holds the same roles instead.
resource "azurerm_role_assignment" "search_documents_owner" {
  scope                = azurerm_search_service.search.id
  role_definition_name = "Search Index Data Contributor"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_role_assignment" "openai_user_owner" {
  scope                = azurerm_cognitive_account.ai.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = data.azurerm_client_config.current.object_id
}

# The index is Terraform-owned data plane. The vectorizer and its key live
# in the write-only sensitive body, merge-patched onto the request at apply
# time: the key reaches the search service directly from the AI Services
# account and is never stored in state, printed as output, or handled by a
# person. The ingester only pushes and deletes documents.
resource "azapi_data_plane_resource" "index" {
  type      = "Microsoft.Search/searchServices/indexes@2024-07-01"
  parent_id = "${azurerm_search_service.search.name}.search.windows.net"
  name      = "kb"

  body = {
    name = "kb"
    fields = [
      {
        name = "id"
        type = "Edm.String"
        key  = true
      },
      {
        name       = "title"
        type       = "Edm.String"
        searchable = true
        filterable = true
      },
      {
        name       = "content"
        type       = "Edm.String"
        searchable = true
      },
      {
        name        = "url"
        type        = "Edm.String"
        retrievable = true
      },
      {
        name                = "embedding"
        type                = "Collection(Edm.Single)"
        searchable          = true
        dimensions          = 1536
        vectorSearchProfile = "kb-profile"
      },
    ]
    vectorSearch = {
      algorithms = [
        {
          name = "kb-algorithm"
          kind = "hnsw"
          hnswParameters = {
            metric = "cosine"
          }
        },
      ]
      profiles = [
        {
          name       = "kb-profile"
          algorithm  = "kb-algorithm"
          vectorizer = "openai-vectorizer"
        },
      ]
    }
    semantic = {
      configurations = [
        {
          name = "kb-semantic"
          prioritizedFields = {
            titleField               = { fieldName = "title" }
            prioritizedContentFields = [{ fieldName = "content" }]
          }
        },
      ]
    }
  }

  sensitive_body = {
    vectorSearch = {
      vectorizers = [
        {
          name = "openai-vectorizer"
          kind = "azureOpenAI"
          azureOpenAIParameters = {
            resourceUri  = azurerm_cognitive_account.ai.endpoint
            deploymentId = azurerm_cognitive_deployment.embedding.name
            modelName    = "text-embedding-3-small"
            apiKey       = azurerm_cognitive_account.ai.primary_access_key
          }
        },
      ]
    }
  }

  depends_on = [azurerm_cognitive_deployment.embedding]
}

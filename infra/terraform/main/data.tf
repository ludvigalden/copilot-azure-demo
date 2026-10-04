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
  count = local.owns_shared ? 1 : 0

  name                = local.search_name
  resource_group_name = data.azurerm_resource_group.app.name
  location            = data.azurerm_resource_group.app.location
  sku                 = var.search_sku
  replica_count       = 1

  # Accepts Entra identities on the data plane (role assignments below) and
  # keeps API keys accepted for the connections that still need one.
  authentication_failure_mode = "http401WithBearerChallenge"

  # azurerm refuses to configure the semantic ranker on a free-tier
  # service, yet records the value read back from the API and would reset
  # it to disabled on every apply. The capability is owned by the azapi
  # update below; ignore it here.
  lifecycle {
    ignore_changes = [semantic_search_sku]
  }
}

# The semantic ranker is a service-level capability that defaults to
# disabled; with it disabled, every semantic query fails at runtime. The
# free plan carries a monthly request allowance, which suits the tiny
# knowledge base. azurerm cannot set this property on a free-tier service,
# so the update goes through the management API directly.
resource "azapi_update_resource" "search_semantic_plan" {
  type        = "Microsoft.Search/searchServices@2025-05-01"
  resource_id = local.search_service_id

  body = {
    properties = {
      semanticSearch = "free"
    }
  }
}

resource "azurerm_role_assignment" "search_documents_app" {
  scope                = local.search_service_id
  role_definition_name = "Search Index Data Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# The CD identity's index-data roles live in the owner state; a shared run
# skips them so the same assignment is never created twice.
resource "azurerm_role_assignment" "search_documents_cd" {
  count = local.owns_shared ? 1 : 0

  scope                = local.search_service_id
  role_definition_name = "Search Index Data Contributor"
  principal_id         = var.cd_principal_object_id
}

# Azure OpenAI (Foundry). Key access stays enabled for exactly one consumer:
# the index vectorizer below, whose key Terraform moves into the index
# definition itself. Every other caller uses Entra identities and RBAC.
resource "azurerm_cognitive_account" "ai" {
  count = local.owns_shared ? 1 : 0

  name                  = local.ai_name
  resource_group_name   = data.azurerm_resource_group.app.name
  location              = data.azurerm_resource_group.app.location
  kind                  = "AIServices"
  sku_name              = "S0"
  custom_subdomain_name = local.ai_name
  local_auth_enabled    = true
}

resource "azurerm_cognitive_deployment" "chat" {
  name                 = var.chat_deployment_name
  cognitive_account_id = local.ai_account_id

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
  name                 = var.embedding_deployment_name
  cognitive_account_id = local.ai_account_id

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
  scope                = local.ai_account_id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

resource "azurerm_role_assignment" "openai_user_cd" {
  count = local.owns_shared ? 1 : 0

  scope                = local.ai_account_id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = var.cd_principal_object_id
}

# The owner manages the index data plane locally (the apply identity reads
# and writes the index directly) and can verify the data-plane paths. The
# grants are off in CD, where the CD identity holds the same roles.
resource "azurerm_role_assignment" "search_documents_owner" {
  count = var.owner_principal_object_id != "" ? 1 : 0

  scope                = local.search_service_id
  role_definition_name = "Search Index Data Contributor"
  principal_id         = var.owner_principal_object_id
}

resource "azurerm_role_assignment" "openai_user_owner" {
  count = var.owner_principal_object_id != "" ? 1 : 0

  scope                = local.ai_account_id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = var.owner_principal_object_id
}

# The local loop's principal fills the staging index and reads the staging
# tickets back with its own narrow identity; the staging apply grants it
# exactly those two data planes and nothing on the management plane.
resource "azurerm_role_assignment" "act_index_documents" {
  count = var.act_principal_object_id != "" ? 1 : 0

  scope                = local.search_service_id
  role_definition_name = "Search Index Data Contributor"
  principal_id         = var.act_principal_object_id
}

resource "azurerm_role_assignment" "act_tickets_rows" {
  count = var.act_principal_object_id != "" ? 1 : 0

  scope                = azurerm_storage_account.tickets.id
  role_definition_name = "Storage Table Data Contributor"
  principal_id         = var.act_principal_object_id
}

# The index is Terraform-owned data plane. The vectorizer and its key live
# in the write-only sensitive body, merge-patched onto the request at apply
# time: the key reaches the search service directly from the AI Services
# account and is never stored in state, printed as output, or handled by a
# person. The ingester only pushes and deletes documents. Every
# index-internal name follows the index name, so a shared run's copy never
# collides with the owner's.
resource "azapi_data_plane_resource" "index" {
  type      = "Microsoft.Search/searchServices/indexes@2024-07-01"
  parent_id = "${local.search_name_effective}.search.windows.net"
  name      = var.index_name

  body = {
    name = var.index_name
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
        vectorSearchProfile = "${var.index_name}-profile"
      },
    ]
    vectorSearch = {
      algorithms = [
        {
          name = "${var.index_name}-algorithm"
          kind = "hnsw"
          hnswParameters = {
            metric = "cosine"
          }
        },
      ]
      profiles = [
        {
          name       = "${var.index_name}-profile"
          algorithm  = "${var.index_name}-algorithm"
          vectorizer = "openai-vectorizer"
        },
      ]
    }
    semantic = {
      configurations = [
        {
          name = "${var.index_name}-semantic"
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
            resourceUri  = local.openai_endpoint
            deploymentId = azurerm_cognitive_deployment.embedding.name
            modelName    = "text-embedding-3-small"
            apiKey       = local.vectorizer_api_key
          }
        },
      ]
    }
  }

  depends_on = [azurerm_cognitive_deployment.embedding]
}

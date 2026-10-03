# The Azure Bot registration that lets Bot Framework channels reach the
# agent endpoint on the container app. Its Microsoft App identity is the
# application's user-assigned managed identity — the bot's app ID is that
# identity's client ID, and the registration points at the identity
# resource — so channel traffic authenticates with the same identity the
# API already uses outbound and no client secret exists anywhere.

resource "azurerm_bot_service_azure_bot" "bot" {
  name                = "${var.prefix}-bot"
  resource_group_name = data.azurerm_resource_group.app.name
  location            = "global"
  sku                 = "F0"

  microsoft_app_type      = "UserAssignedMSI"
  microsoft_app_id        = azurerm_user_assigned_identity.app.client_id
  microsoft_app_msi_id    = azurerm_user_assigned_identity.app.id
  microsoft_app_tenant_id = data.azurerm_client_config.current.tenant_id

  # Bot Framework posts channel messages here: the custom hostname when
  # bound, otherwise the container app's default domain.
  endpoint                     = local.bot_endpoint
  local_authentication_enabled = false
}

# The Direct Line channel gives headless clients a signed REST surface to
# the bot; the Web Chat channel's site secret only serves the web chat
# token server, so scripted verification and tooling talk Direct Line.
resource "azurerm_bot_channel_directline" "directline" {
  bot_name            = azurerm_bot_service_azure_bot.bot.name
  resource_group_name = data.azurerm_resource_group.app.name
  location            = azurerm_bot_service_azure_bot.bot.location

  site {
    name       = "headless-default"
    v3_allowed = true
  }
}

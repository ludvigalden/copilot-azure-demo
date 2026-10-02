output "client_id" {
  description = "Application (client) ID of the CD app registration; becomes the AZURE_CLIENT_ID environment variable in GitHub."
  value       = azuread_application.cd.client_id
}

output "principal_object_id" {
  description = "Object ID of the CD service principal; becomes the CD_PRINCIPAL_OBJECT_ID environment variable in GitHub."
  value       = azuread_service_principal.cd.object_id
}

output "tenant_id" {
  description = "Entra tenant ID; becomes the AZURE_TENANT_ID environment variable in GitHub."
  value       = data.azuread_client_config.current.tenant_id
}

output "subscription_id" {
  description = "Azure subscription ID; becomes the AZURE_SUBSCRIPTION_ID environment variable in GitHub."
  value       = data.azurerm_client_config.current.subscription_id
}

output "state_resource_group_name" {
  value = azurerm_resource_group.state.name
}

output "state_storage_account_name" {
  value = azurerm_storage_account.state.name
}

output "state_container_name" {
  value = "tfstate"
}

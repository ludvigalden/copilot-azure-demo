output "app_fqdn" {
  description = "Default public host of the container app."
  value       = azurerm_container_app.app.ingress[0].fqdn
}

# The two values the custom domain's DNS records are made from.
output "custom_domain_cname_target" {
  value = azurerm_container_app.app.ingress[0].fqdn
}

output "custom_domain_verification_id" {
  value     = azurerm_container_app.app.custom_domain_verification_id
  sensitive = true
}

output "search_endpoint" {
  value = local.search_endpoint
}

output "openai_endpoint" {
  value = azurerm_cognitive_account.ai.endpoint
}

output "resource_group_name" {
  value = data.azurerm_resource_group.app.name
}

output "container_app_name" {
  value = local.container_app_name
}

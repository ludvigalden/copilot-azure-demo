# Custom domain, bound in a later step once its DNS records exist. Both
# resources stay at count zero while the hostname is empty, so provisioning
# never waits on DNS.

locals {
  bind_domain = var.public_hostname != "" ? 1 : 0
}

resource "azurerm_container_app_custom_domain" "public" {
  count = local.bind_domain

  name             = var.public_hostname
  container_app_id = azurerm_container_app.app.id

  lifecycle {
    # The managed certificate binds out of band once DNS validates.
    ignore_changes = [certificate_binding_type, container_app_environment_certificate_id]
  }
}

resource "azurerm_container_app_environment_managed_certificate" "public" {
  count = local.bind_domain

  name                         = "${var.prefix}-cert"
  container_app_environment_id = azurerm_container_app_environment.env.id
  subject_name                 = var.public_hostname
  domain_control_validation    = "CNAME"

  depends_on = [azurerm_container_app_custom_domain.public]
}

# Custom domain, served from a free managed certificate. Three steps, in
# order: the domain registers on the container app, the certificate issues
# once DNS validates it, and the certificate binds to the domain. Both
# domain resources stay at count zero while the hostname is empty, so
# provisioning never waits on DNS.
#
# The binding is its own step because of a genuine cycle: a managed
# certificate cannot be created before its custom domain exists, and the
# domain cannot name the certificate's id before the certificate exists.
# The azurerm pair below therefore registers the domain with the binding
# unset and issues the certificate; the azapi update then attaches the
# issued certificate to the registered domain.

locals {
  bind_domain = var.public_hostname != "" ? 1 : 0
}

resource "azurerm_container_app_custom_domain" "public" {
  count = local.bind_domain

  name             = var.public_hostname
  container_app_id = azurerm_container_app.app.id

  lifecycle {
    # The azapi update below owns the binding; ignore it here, or the
    # provider would reset the certificate attachment it cannot express.
    ignore_changes = [certificate_binding_type, container_app_environment_certificate_id]
  }
}

resource "azurerm_container_app_environment_managed_certificate" "public" {
  count = local.bind_domain

  name                         = "${var.prefix}-cert"
  container_app_environment_id = local.app_environment_id
  subject_name                 = var.public_hostname
  domain_control_validation    = "CNAME"

  depends_on = [azurerm_container_app_custom_domain.public]
}

resource "azapi_update_resource" "domain_binding" {
  count = local.bind_domain

  type        = "Microsoft.App/containerApps@2024-03-01"
  resource_id = azurerm_container_app.app.id

  body = {
    properties = {
      configuration = {
        ingress = {
          customDomains = [
            {
              name          = var.public_hostname
              bindingType   = "SniEnabled"
              certificateId = azurerm_container_app_environment_managed_certificate.public[0].id
            }
          ]
        }
      }
    }
  }

  depends_on = [azurerm_container_app_environment_managed_certificate.public]
}

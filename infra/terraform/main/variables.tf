variable "prefix" {
  description = "Name prefix; every Azure resource name derives from it."
  type        = string
}

variable "location" {
  description = "Azure region for every resource."
  type        = string
  default     = "swedencentral"
}

variable "search_sku" {
  description = "Azure AI Search tier. Free holds one service per subscription with a 50 MB cap; Basic is the paid fallback."
  type        = string
  default     = "free"
}

# Empty until the custom domain is bound; both domain resources stay at
# count zero while it is empty, so provisioning never waits on DNS.
variable "public_hostname" {
  description = "Custom domain served by the container app."
  type        = string
  default     = ""
  sensitive   = true
}

# Object ID of the CD service principal created by the bootstrap root. The
# CD identity itself cannot look it up: it may only read the applications
# it owns, and its own registration is owned by the person who ran the
# bootstrap apply.
variable "cd_principal_object_id" {
  description = "Object ID of the CD service principal."
  type        = string
}

variable "servicenow_instance_url" {
  description = "ServiceNow instance URL; presence selects the ServiceNow ticket store over the built-in one."
  type        = string
  default     = ""
  sensitive   = true
}

variable "servicenow_username" {
  description = "ServiceNow integration user name."
  type        = string
  default     = ""
  sensitive   = true
}

variable "servicenow_password" {
  description = "ServiceNow integration user password."
  type        = string
  default     = ""
  sensitive   = true
}

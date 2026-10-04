variable "prefix" {
  description = "Name prefix; every Azure resource name derives from it."
  type        = string
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

# Object ID of the person who runs Terraform locally. Empty in CD, which
# turns the owner's two data-plane grants off; the workflow identity holds
# the same roles through the CD assignments instead.
variable "owner_principal_object_id" {
  description = "Object ID of the local Terraform runner, if any."
  type        = string
  default     = ""
}

# Prefix of the environment whose shared resources this root consumes.
# Empty, the default, means this root owns its search service, AI Services
# account, container apps environment, and Entra API application outright;
# the production apply runs that way. A non-empty value turns those four
# into data sources of the named environment and scopes this root's own
# resources (container app, identity, tickets account, bot, index,
# deployments) to them, which is how staging rides on production's shared
# services with its own resource group, state, and prefix.
variable "shared_prefix" {
  description = "Prefix of the environment whose shared resources this root consumes; empty when this root owns them."
  type        = string
  default     = ""
}

# Names of the index and deployments this root creates. The defaults are
# the production literals; a shared-resources run renames its copies so
# both environments fit one search service (three-index cap) and one AI
# account. Derived index-internal names follow the index name.
variable "index_name" {
  description = "Name of the AI Search index this root owns."
  type        = string
  default     = "kb"
}

variable "chat_deployment_name" {
  description = "Name of the chat-completion deployment this root owns."
  type        = string
  default     = "chat"
}

variable "embedding_deployment_name" {
  description = "Name of the embedding deployment this root owns and the index vectorizer points at."
  type        = string
  default     = "embedding"
}

# Redirect URIs appended to the API application when this root owns it.
# A shared-resources run cannot add its own: the application is owned by
# the owner root, which carries the shared run's host here. Empty by
# default, so the owner plan is unchanged until a value is set.
variable "extra_redirect_uris" {
  description = "Additional SPA redirect URIs for the owned API application; empty in the default apply."
  type        = list(string)
  default     = []
}

# Object ID of the local-loop service principal, when one exists. The
# staging apply turns its two data-plane grants on (documents on the
# shared search service, rows on the staging ticket store) so a local
# ingest run and a local ticket read-back work with the same narrow
# identity. Empty — the default, and always in the owner apply — turns
# both off, and the owner's plan never mentions the principal.
variable "act_principal_object_id" {
  description = "Object ID of the local loop's service principal, if any."
  type        = string
  default     = ""
}

# Key of the shared AI Services account, passed out of band by a run that
# consumes the account without being able to list its keys (the local
# loop's apply identity is read-only on the owner group). The CI identity
# lists the key itself and never sets this. Empty in the owner apply.
variable "shared_ai_account_key" {
  description = "Access key of the shared AI Services account; empty when the root can list keys itself."
  type        = string
  default     = ""
  sensitive   = true
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

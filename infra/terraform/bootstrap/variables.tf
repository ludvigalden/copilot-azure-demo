# Name prefix; every Azure resource name derives from it. Globally unique
# per deployer, because storage account names and the AI services subdomain
# are global namespaces.
variable "prefix" {
  type = string
}

# GitHub repository (owner/name) that CD deploys from; it is the trust
# subject of the federated credential.
variable "github_repository" {
  type = string
}

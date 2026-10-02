terraform {
  required_version = ">= 1.9"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.7"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.10"
    }
    azapi = {
      source  = "Azure/azapi"
      version = "~> 2.13"
    }
  }

  # Remote state in the bootstrap account. The state resource group and
  # account names are passed on the command line with -backend-config; runs
  # from GitHub Actions add -backend-config="use_oidc=true".
  backend "azurerm" {
    use_azuread_auth = true
    container_name   = "tfstate"
    key              = "main.tfstate"
  }
}

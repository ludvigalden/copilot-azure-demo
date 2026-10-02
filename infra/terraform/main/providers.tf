provider "azurerm" {
  features {}

  resource_providers_to_register = [
    "Microsoft.App",
    "Microsoft.CognitiveServices",
    "Microsoft.Search",
    "Microsoft.Storage",
    "Microsoft.ManagedIdentity",
  ]
}

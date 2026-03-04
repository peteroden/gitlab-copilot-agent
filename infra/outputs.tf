output "resource_group_name" {
  description = "Name of the resource group"
  value       = azurerm_resource_group.main.name
}

output "container_apps_environment_id" {
  description = "ID of the Container Apps Environment"
  value       = azurerm_container_app_environment.main.id
}

output "acr_login_server" {
  description = "ACR login server URL"
  value       = azurerm_container_registry.main.login_server
}

output "controller_fqdn" {
  description = "FQDN of the controller Container App"
  value       = azurerm_container_app.controller.latest_revision_fqdn
}

output "storage_account_name" {
  description = "Azure Storage Account name for task dispatch"
  value       = azurerm_storage_account.tasks.name
}

output "key_vault_uri" {
  description = "Azure Key Vault URI"
  value       = azurerm_key_vault.main.vault_uri
}

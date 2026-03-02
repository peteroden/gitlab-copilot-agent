data "azurerm_client_config" "current" {}

resource "azurerm_key_vault" "main" {
  name                = "kv-${replace(var.resource_group_name, "rg-", "")}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tenant_id           = data.azurerm_client_config.current.tenant_id
  sku_name            = "standard"

  rbac_authorization_enabled = true

  tags = var.tags
}

# S4: Controller identity — ACR pull, Key Vault read (all secrets), Job trigger
resource "azurerm_user_assigned_identity" "controller" {
  name                = "id-controller-${var.resource_group_name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
}

# S4: Job identity — Key Vault read (task secrets only), Redis data access
resource "azurerm_user_assigned_identity" "job" {
  name                = "id-job-${var.resource_group_name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
}

# Controller: Key Vault Secrets User
resource "azurerm_role_assignment" "controller_kv" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.controller.principal_id
}

# Job: Key Vault Secrets User (scoped to vault; per-secret RBAC is not yet GA)
resource "azurerm_role_assignment" "job_kv" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.job.principal_id
}

# Controller: Redis data access via Entra ID
resource "azurerm_role_assignment" "controller_redis" {
  scope                = azurerm_redis_cache.main.id
  role_definition_name = "Redis Cache Data Contributor"
  principal_id         = azurerm_user_assigned_identity.controller.principal_id
}

# Job: Redis data access via Entra ID
resource "azurerm_role_assignment" "job_redis" {
  scope                = azurerm_redis_cache.main.id
  role_definition_name = "Redis Cache Data Contributor"
  principal_id         = azurerm_user_assigned_identity.job.principal_id
}

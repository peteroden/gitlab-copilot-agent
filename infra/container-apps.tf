resource "azurerm_container_registry" "main" {
  name                = replace("acr${var.resource_group_name}", "-", "")
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = "Basic"
  admin_enabled       = false

  tags = var.tags
}

# Controller identity: ACR pull
resource "azurerm_role_assignment" "controller_acr" {
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.controller.principal_id
}

# Job identity: ACR pull
resource "azurerm_role_assignment" "job_acr" {
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.job.principal_id
}

# --- Container Apps Environment ---

resource "azurerm_container_app_environment" "main" {
  name                       = "cae-${var.resource_group_name}"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  infrastructure_subnet_id   = azurerm_subnet.infra.id

  tags = var.tags
}

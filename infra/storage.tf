# Azure Storage Account for task dispatch (Queue + Blob, Claim Check pattern)
resource "azurerm_storage_account" "tasks" {
  name                       = "st${replace(var.resource_group_name, "-", "")}tasks"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  account_tier               = "Standard"
  account_replication_type   = "LRS"
  min_tls_version            = "TLS1_2"
  shared_access_key_enabled  = false
  public_network_access_enabled = true

  tags = var.tags
}

resource "azurerm_storage_queue" "tasks" {
  name                 = "task-queue"
  storage_account_name = azurerm_storage_account.tasks.name
}

resource "azurerm_storage_container" "task_data" {
  name                  = "task-data"
  storage_account_name  = azurerm_storage_account.tasks.name
  container_access_type = "private"
}

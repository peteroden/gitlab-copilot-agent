# Azure Storage Account for task dispatch (Queue + Blob, Claim Check pattern)
locals {
  storage_name_raw = "strg${replace(var.resource_group_name, "-", "")}"
  storage_name     = substr(local.storage_name_raw, 0, min(24, length(local.storage_name_raw)))
}

resource "azurerm_storage_account" "tasks" {
  name                          = local.storage_name
  location                      = azurerm_resource_group.main.location
  resource_group_name           = azurerm_resource_group.main.name
  account_tier                  = "Standard"
  account_replication_type      = "LRS"
  min_tls_version               = "TLS1_2"
  shared_access_key_enabled     = false
  public_network_access_enabled = false

  tags = var.tags
}

resource "azurerm_storage_queue" "tasks" {
  name               = "task-queue"
  storage_account_id = azurerm_storage_account.tasks.id
}

resource "azurerm_storage_container" "task_data" {
  name               = "task-data"
  storage_account_id = azurerm_storage_account.tasks.id
}

# --- Storage Private DNS Zones ---

resource "azurerm_private_dns_zone" "blob" {
  name                = "privatelink.blob.core.windows.net"
  resource_group_name = azurerm_resource_group.main.name
  tags                = var.tags
}

resource "azurerm_private_dns_zone" "queue" {
  name                = "privatelink.queue.core.windows.net"
  resource_group_name = azurerm_resource_group.main.name
  tags                = var.tags
}

resource "azurerm_private_dns_zone" "table" {
  name                = "privatelink.table.core.windows.net"
  resource_group_name = azurerm_resource_group.main.name
  tags                = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "blob" {
  name                  = "blob-vnet-link"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = azurerm_private_dns_zone.blob.name
  virtual_network_id    = azurerm_virtual_network.main.id
}

resource "azurerm_private_dns_zone_virtual_network_link" "queue" {
  name                  = "queue-vnet-link"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = azurerm_private_dns_zone.queue.name
  virtual_network_id    = azurerm_virtual_network.main.id
}

resource "azurerm_private_dns_zone_virtual_network_link" "table" {
  name                  = "table-vnet-link"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = azurerm_private_dns_zone.table.name
  virtual_network_id    = azurerm_virtual_network.main.id
}

# --- Storage Private Endpoints ---

resource "azurerm_private_endpoint" "blob" {
  name                = "pe-blob-${var.resource_group_name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.storage.id

  private_service_connection {
    name                           = "blob-connection"
    private_connection_resource_id = azurerm_storage_account.tasks.id
    subresource_names              = ["blob"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "blob-dns"
    private_dns_zone_ids = [azurerm_private_dns_zone.blob.id]
  }

  tags = var.tags
}

resource "azurerm_private_endpoint" "queue" {
  name                = "pe-queue-${var.resource_group_name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.storage.id

  private_service_connection {
    name                           = "queue-connection"
    private_connection_resource_id = azurerm_storage_account.tasks.id
    subresource_names              = ["queue"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "queue-dns"
    private_dns_zone_ids = [azurerm_private_dns_zone.queue.id]
  }

  tags = var.tags
}

resource "azurerm_private_endpoint" "table" {
  name                = "pe-table-${var.resource_group_name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.storage.id

  private_service_connection {
    name                           = "table-connection"
    private_connection_resource_id = azurerm_storage_account.tasks.id
    subresource_names              = ["table"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "table-dns"
    private_dns_zone_ids = [azurerm_private_dns_zone.table.id]
  }

  tags = var.tags
}

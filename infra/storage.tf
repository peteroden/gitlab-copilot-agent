# --- Storage Account for task dispatch (Queue + Blob + Table) ---

resource "azurerm_storage_account" "tasks" {
  name                          = substr(replace(lower("st${var.resource_group_name}"), "-", ""), 0, 24)
  resource_group_name           = azurerm_resource_group.main.name
  location                      = azurerm_resource_group.main.location
  account_tier                  = "Standard"
  account_replication_type      = "LRS"
  min_tls_version               = "TLS1_2"
  shared_access_key_enabled     = false # Azure Policy enforces this; KEDA needs MI auth (Phase 3)
  public_network_access_enabled = true  # Terraform needs data plane access; tighten in Phase 3

  tags = var.tags
}

resource "azurerm_storage_queue" "tasks" {
  name               = "task-queue"
  storage_account_id = azurerm_storage_account.tasks.id
}

resource "azurerm_storage_container" "tasks" {
  name               = "task-data"
  storage_account_id = azurerm_storage_account.tasks.id
}

# azapi used because azurerm_storage_table makes data-plane ACL calls that
# fail when shared_access_key_enabled=false (Azure Policy).
resource "azapi_resource" "dedup_table" {
  type      = "Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01"
  name      = "dedup"
  parent_id = "${azurerm_storage_account.tasks.id}/tableServices/default"
}

# Auto-delete blobs after 7 days (results + claim-check params)
resource "azurerm_storage_management_policy" "task_cleanup" {
  storage_account_id = azurerm_storage_account.tasks.id

  rule {
    name    = "delete-old-task-data"
    enabled = true

    filters {
      prefix_match = ["task-data/"]
      blob_types   = ["blockBlob"]
    }

    actions {
      base_blob {
        delete_after_days_since_modification_greater_than = 7
      }
    }
  }
}

# --- Private Endpoints (blob, queue, table) ---

locals {
  storage_sub_resources = {
    blob  = "privatelink.blob.core.windows.net"
    queue = "privatelink.queue.core.windows.net"
    table = "privatelink.table.core.windows.net"
  }
}

resource "azurerm_private_dns_zone" "storage" {
  for_each            = local.storage_sub_resources
  name                = each.value
  resource_group_name = azurerm_resource_group.main.name
  tags                = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "storage" {
  for_each              = local.storage_sub_resources
  name                  = "${each.key}-vnet-link"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = azurerm_private_dns_zone.storage[each.key].name
  virtual_network_id    = azurerm_virtual_network.main.id
}

resource "azurerm_private_endpoint" "storage" {
  for_each            = local.storage_sub_resources
  name                = "pe-${each.key}-${var.resource_group_name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.storage.id

  private_service_connection {
    name                           = "${each.key}-connection"
    private_connection_resource_id = azurerm_storage_account.tasks.id
    subresource_names              = [each.key]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "${each.key}-dns"
    private_dns_zone_ids = [azurerm_private_dns_zone.storage[each.key].id]
  }

  tags = var.tags
}

# --- RBAC: Controller identity ---

resource "azurerm_role_assignment" "controller_queue_sender" {
  scope                = azurerm_storage_account.tasks.id
  role_definition_name = "Storage Queue Data Message Sender"
  principal_id         = azurerm_user_assigned_identity.controller.principal_id
}

resource "azurerm_role_assignment" "controller_blob" {
  scope                = azurerm_storage_account.tasks.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.controller.principal_id
}

resource "azurerm_role_assignment" "controller_table" {
  scope                = azurerm_storage_account.tasks.id
  role_definition_name = "Storage Table Data Contributor"
  principal_id         = azurerm_user_assigned_identity.controller.principal_id
}

# --- RBAC: Job identity ---

resource "azurerm_role_assignment" "job_queue_processor" {
  scope                = azurerm_storage_account.tasks.id
  role_definition_name = "Storage Queue Data Message Processor"
  principal_id         = azurerm_user_assigned_identity.job.principal_id
}

resource "azurerm_role_assignment" "job_blob" {
  scope                = azurerm_storage_account.tasks.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.job.principal_id
}

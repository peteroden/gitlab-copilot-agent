resource "azurerm_log_analytics_workspace" "main" {
  name                = "log-${var.resource_group_name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = 30

  internet_ingestion_enabled = false
  internet_query_enabled     = false

  tags = var.tags
}

resource "azurerm_application_insights" "main" {
  name                = "ai-${var.resource_group_name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  workspace_id        = azurerm_log_analytics_workspace.main.id
  application_type    = "web"

  internet_ingestion_enabled = false
  internet_query_enabled     = false

  tags = var.tags
}

# --- Azure Monitor Private Link Scope (AMPLS) ---

resource "azurerm_monitor_private_link_scope" "main" {
  name                  = "ampls-${var.resource_group_name}"
  resource_group_name   = azurerm_resource_group.main.name
  ingestion_access_mode = "PrivateOnly"
  query_access_mode     = "PrivateOnly"

  tags = var.tags
}

resource "azurerm_monitor_private_link_scoped_service" "law" {
  name                = "ampls-law"
  resource_group_name = azurerm_resource_group.main.name
  scope_name          = azurerm_monitor_private_link_scope.main.name
  linked_resource_id  = azurerm_log_analytics_workspace.main.id
}

resource "azurerm_monitor_private_link_scoped_service" "ai" {
  name                = "ampls-ai"
  resource_group_name = azurerm_resource_group.main.name
  scope_name          = azurerm_monitor_private_link_scope.main.name
  linked_resource_id  = azurerm_application_insights.main.id
}

# Private DNS zones required for AMPLS
resource "azurerm_private_dns_zone" "monitor" {
  name                = "privatelink.monitor.azure.com"
  resource_group_name = azurerm_resource_group.main.name
  tags                = var.tags
}

resource "azurerm_private_dns_zone" "oms" {
  name                = "privatelink.oms.opinsights.azure.com"
  resource_group_name = azurerm_resource_group.main.name
  tags                = var.tags
}

resource "azurerm_private_dns_zone" "ods" {
  name                = "privatelink.ods.opinsights.azure.com"
  resource_group_name = azurerm_resource_group.main.name
  tags                = var.tags
}

resource "azurerm_private_dns_zone" "agentsvc" {
  name                = "privatelink.agentsvc.azure-automation.net"
  resource_group_name = azurerm_resource_group.main.name
  tags                = var.tags
}

# VNet links for each DNS zone
resource "azurerm_private_dns_zone_virtual_network_link" "monitor" {
  name                  = "monitor-vnet-link"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = azurerm_private_dns_zone.monitor.name
  virtual_network_id    = azurerm_virtual_network.main.id
}

resource "azurerm_private_dns_zone_virtual_network_link" "oms" {
  name                  = "oms-vnet-link"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = azurerm_private_dns_zone.oms.name
  virtual_network_id    = azurerm_virtual_network.main.id
}

resource "azurerm_private_dns_zone_virtual_network_link" "ods" {
  name                  = "ods-vnet-link"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = azurerm_private_dns_zone.ods.name
  virtual_network_id    = azurerm_virtual_network.main.id
}

resource "azurerm_private_dns_zone_virtual_network_link" "agentsvc" {
  name                  = "agentsvc-vnet-link"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = azurerm_private_dns_zone.agentsvc.name
  virtual_network_id    = azurerm_virtual_network.main.id
}

# AMPLS private endpoint
resource "azurerm_private_endpoint" "ampls" {
  name                = "pe-ampls-${var.resource_group_name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.monitoring.id

  private_service_connection {
    name                           = "ampls-connection"
    private_connection_resource_id = azurerm_monitor_private_link_scope.main.id
    subresource_names              = ["azuremonitor"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name = "ampls-dns"
    private_dns_zone_ids = [
      azurerm_private_dns_zone.monitor.id,
      azurerm_private_dns_zone.oms.id,
      azurerm_private_dns_zone.ods.id,
      azurerm_private_dns_zone.agentsvc.id,
    ]
  }

  tags = var.tags

  depends_on = [
    azurerm_monitor_private_link_scoped_service.law,
    azurerm_monitor_private_link_scoped_service.ai,
    azurerm_private_dns_zone_virtual_network_link.monitor,
    azurerm_private_dns_zone_virtual_network_link.oms,
    azurerm_private_dns_zone_virtual_network_link.ods,
    azurerm_private_dns_zone_virtual_network_link.agentsvc,
  ]
}

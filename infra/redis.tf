# S6: Basic tier for dev (no encryption at rest). Use Standard/Premium for prod.
resource "azurerm_redis_cache" "main" {
  name                 = "redis-${var.resource_group_name}"
  location             = azurerm_resource_group.main.location
  resource_group_name  = azurerm_resource_group.main.name
  capacity             = var.redis_capacity
  family               = var.redis_sku == "Basic" ? "C" : "C"
  sku_name             = var.redis_sku
  minimum_tls_version  = "1.2"
  non_ssl_port_enabled = false

  # Entra ID auth — no access keys needed
  access_keys_authentication_enabled = false

  # Disable public access — traffic goes through private endpoint only
  public_network_access_enabled = false

  redis_configuration {
    active_directory_authentication_enabled = true
  }

  tags = var.tags
}

# --- Private Endpoint (keeps Redis traffic on VNet) ---

resource "azurerm_private_dns_zone" "redis" {
  name                = "privatelink.redis.cache.windows.net"
  resource_group_name = azurerm_resource_group.main.name

  tags = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "redis" {
  name                  = "redis-vnet-link"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = azurerm_private_dns_zone.redis.name
  virtual_network_id    = azurerm_virtual_network.main.id
}

resource "azurerm_private_endpoint" "redis" {
  name                = "pe-redis-${var.resource_group_name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.redis.id

  private_service_connection {
    name                           = "redis-connection"
    private_connection_resource_id = azurerm_redis_cache.main.id
    subresource_names              = ["redisCache"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "redis-dns"
    private_dns_zone_ids = [azurerm_private_dns_zone.redis.id]
  }

  tags = var.tags
}

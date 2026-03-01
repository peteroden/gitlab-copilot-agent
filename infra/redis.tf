# S6: Basic tier for dev (no encryption at rest). Use Standard/Premium for prod.
resource "azurerm_redis_cache" "main" {
  name                = "redis-${var.resource_group_name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  capacity            = var.redis_capacity
  family              = var.redis_sku == "Basic" ? "C" : "C"
  sku_name            = var.redis_sku
  minimum_tls_version = "1.2"
  non_ssl_port_enabled = false

  redis_configuration {}

  tags = var.tags
}

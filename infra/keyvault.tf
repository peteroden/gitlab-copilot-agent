data "azurerm_client_config" "current" {}

resource "random_string" "kv_suffix" {
  length  = 4
  special = false
  upper   = false
}

resource "azurerm_key_vault" "main" {
  # Key Vault names: 3-24 chars, globally unique. Truncate base to fit suffix.
  name                = "kv-${trimsuffix(substr(replace(var.resource_group_name, "rg-", ""), 0, 16), "-")}-${random_string.kv_suffix.result}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tenant_id           = data.azurerm_client_config.current.tenant_id
  sku_name            = "standard"

  rbac_authorization_enabled    = true
  public_network_access_enabled = false

  tags = var.tags
}

# --- KV Private Endpoint (keeps secret traffic on VNet) ---

resource "azurerm_private_dns_zone" "keyvault" {
  name                = "privatelink.vaultcore.azure.net"
  resource_group_name = azurerm_resource_group.main.name

  tags = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "keyvault" {
  name                  = "keyvault-vnet-link"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = azurerm_private_dns_zone.keyvault.name
  virtual_network_id    = azurerm_virtual_network.main.id
}

resource "azurerm_private_endpoint" "keyvault" {
  name                = "pe-kv-${var.resource_group_name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.keyvault.id

  private_service_connection {
    name                           = "keyvault-connection"
    private_connection_resource_id = azurerm_key_vault.main.id
    subresource_names              = ["vault"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "keyvault-dns"
    private_dns_zone_ids = [azurerm_private_dns_zone.keyvault.id]
  }

  tags = var.tags
}

# Deployer: Key Vault Secrets Officer (for bootstrap seeding and rotation)
resource "azurerm_role_assignment" "deployer_kv" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

# --- KV Bootstrap: single-apply chain ---
# open public access → seed secrets → (apps deploy via depends_on) → close public access

resource "null_resource" "kv_bootstrap_open" {
  count = var.kv_bootstrap ? 1 : 0

  triggers = {
    vault_name   = azurerm_key_vault.main.name
    secrets_hash = sha256(jsonencode(var.kv_bootstrap_secrets))
  }

  provisioner "local-exec" {
    command     = <<-EOT
      az keyvault update --name "$VAULT_NAME" --public-network-access Enabled -o none
      echo "⏳ Waiting for propagation..."
      sleep 15
      echo "✓ KV public access enabled"
    EOT
    interpreter = ["bash", "-c"]
    environment = {
      VAULT_NAME = azurerm_key_vault.main.name
    }
  }

  depends_on = [azurerm_key_vault.main, azurerm_role_assignment.deployer_kv]
}

resource "null_resource" "kv_seed_secrets" {
  count = var.kv_bootstrap && length(var.kv_bootstrap_secrets) > 0 ? 1 : 0

  triggers = {
    vault_name   = azurerm_key_vault.main.name
    secret_names = join(",", keys(nonsensitive(var.kv_bootstrap_secrets)))
    secrets_hash = sha256(jsonencode(var.kv_bootstrap_secrets))
  }

  provisioner "local-exec" {
    command     = <<-EOT
      for name in $(echo "$SECRET_NAMES" | tr ',' ' '); do
        az keyvault secret set --vault-name "$VAULT_NAME" --name "$name" \
          --value "$(echo "$SECRETS_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['$name'])")" \
          -o none && echo "✓ $name"
      done
    EOT
    interpreter = ["bash", "-c"]
    environment = {
      VAULT_NAME   = azurerm_key_vault.main.name
      SECRET_NAMES = join(",", keys(nonsensitive(var.kv_bootstrap_secrets)))
      SECRETS_JSON = jsonencode(var.kv_bootstrap_secrets)
    }
  }

  depends_on = [null_resource.kv_bootstrap_open]
}

# Container Apps depend on kv_seed_secrets (see container-apps.tf).
# Close public access immediately after seeding — apps use private endpoint.
resource "null_resource" "kv_bootstrap_close" {
  count = var.kv_bootstrap ? 1 : 0

  triggers = {
    vault_name   = azurerm_key_vault.main.name
    secrets_hash = sha256(jsonencode(var.kv_bootstrap_secrets))
  }

  provisioner "local-exec" {
    command     = <<-EOT
      az keyvault update --name "$VAULT_NAME" --public-network-access Disabled -o none
      echo "✓ KV public access disabled"
    EOT
    interpreter = ["bash", "-c"]
    environment = {
      VAULT_NAME = azurerm_key_vault.main.name
    }
  }

  depends_on = [null_resource.kv_bootstrap_open, null_resource.kv_seed_secrets]
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

# Controller: Redis data access via Entra ID (data access policy, not RBAC)
resource "azurerm_redis_cache_access_policy_assignment" "controller_redis" {
  name               = "controller-data-contributor"
  redis_cache_id     = azurerm_redis_cache.main.id
  access_policy_name = "Data Contributor"
  object_id          = azurerm_user_assigned_identity.controller.principal_id
  object_id_alias    = azurerm_user_assigned_identity.controller.name
}

# Job: Redis data access via Entra ID (data access policy, not RBAC)
resource "azurerm_redis_cache_access_policy_assignment" "job_redis" {
  name               = "job-data-contributor"
  redis_cache_id     = azurerm_redis_cache.main.id
  access_policy_name = "Data Contributor"
  object_id          = azurerm_user_assigned_identity.job.principal_id
  object_id_alias    = azurerm_user_assigned_identity.job.name
}

resource "azurerm_container_registry" "main" {
  name                          = replace("acr${var.resource_group_name}", "-", "")
  resource_group_name           = azurerm_resource_group.main.name
  location                      = azurerm_resource_group.main.location
  sku                           = "Premium"
  admin_enabled                 = false
  public_network_access_enabled = false

  tags = var.tags
}

# ACR private DNS + endpoint
resource "azurerm_private_dns_zone" "acr" {
  name                = "privatelink.azurecr.io"
  resource_group_name = azurerm_resource_group.main.name
  tags                = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "acr" {
  name                  = "acr-vnet-link"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = azurerm_private_dns_zone.acr.name
  virtual_network_id    = azurerm_virtual_network.main.id
}

resource "azurerm_private_endpoint" "acr" {
  name                = "pe-acr-${var.resource_group_name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.storage.id

  private_service_connection {
    name                           = "acr-connection"
    private_connection_resource_id = azurerm_container_registry.main.id
    subresource_names              = ["registry"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "acr-dns"
    private_dns_zone_ids = [azurerm_private_dns_zone.acr.id]
  }

  tags = var.tags
}

# Import image from GHCR into ACR. Runs after ACR is created.
# ACR import is a server-side copy — no Docker daemon needed.
resource "null_resource" "acr_import" {
  triggers = {
    image_digest = var.image_digest
    acr_name     = azurerm_container_registry.main.name
  }

  provisioner "local-exec" {
    # az acr import is ARM control-plane — works with public access disabled
    command     = <<-EOT
      set -euo pipefail
      az acr import -n "$ACR_NAME" \
        --source "ghcr.io/$GHCR_IMAGE@$IMAGE_DIGEST" \
        --image "gitlab-copilot-agent:$IMAGE_TAG" \
        --force
      echo "Imported ghcr.io/$GHCR_IMAGE@$IMAGE_DIGEST → gitlab-copilot-agent:$IMAGE_TAG"
    EOT
    interpreter = ["bash", "-c"]
    environment = {
      ACR_NAME     = azurerm_container_registry.main.name
      GHCR_IMAGE   = var.ghcr_image
      IMAGE_TAG    = var.image_tag
      IMAGE_DIGEST = var.image_digest
    }
  }

  depends_on = [
    azurerm_container_registry.main,
    azurerm_private_endpoint.acr,
    azurerm_role_assignment.deployer_acr,
  ]
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

# Deployer (CI/CD SP): ACR import (server-side copy from GHCR)
resource "azurerm_role_assignment" "deployer_acr" {
  scope                = azurerm_container_registry.main.id
  role_definition_name = "Container Registry Data Importer and Data Reader"
  principal_id         = data.azurerm_client_config.current.object_id
}

locals {
  acr_image = "${azurerm_container_registry.main.login_server}/gitlab-copilot-agent:${var.image_tag}@${var.image_digest}"
}

# --- Container Apps Environment ---

resource "azurerm_container_app_environment" "main" {
  name                       = "cae-${var.resource_group_name}"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  infrastructure_subnet_id   = azurerm_subnet.infra.id

  tags = var.tags

  lifecycle {
    ignore_changes = [infrastructure_resource_group_name]
  }
}

# Managed OTLP agent — forwards traces/metrics/logs to App Insights.
# Not yet supported by azurerm; use azapi to patch the environment.
# Must include logAnalyticsConfiguration to avoid API validation error on PATCH.
resource "azapi_update_resource" "cae_otlp" {
  type        = "Microsoft.App/managedEnvironments@2024-08-02-preview"
  resource_id = azurerm_container_app_environment.main.id

  body = {
    properties = {
      appLogsConfiguration = {
        destination = "log-analytics"
        logAnalyticsConfiguration = {
          customerId = azurerm_log_analytics_workspace.main.workspace_id
          sharedKey  = azurerm_log_analytics_workspace.main.primary_shared_key
        }
      }
      appInsightsConfiguration = {
        connectionString = azurerm_application_insights.main.connection_string
      }
      openTelemetryConfiguration = {
        destinationsConfiguration = {
          appInsightsConfiguration = {
            connectionString = azurerm_application_insights.main.connection_string
          }
        }
        tracesConfiguration = {
          destinations = ["appInsights"]
        }
        logsConfiguration = {
          destinations = ["appInsights"]
        }
      }
    }
  }
}

# S1: Key Vault secret refs — derived from copilot_auth mode and Jira config
locals {
  kv_secrets_runner = merge(
    var.copilot_auth == "github_token" ? { "github-token" = "github-token" } : {},
    var.copilot_auth == "byok" ? { "copilot-api-key" = "copilot-api-key" } : {},
  )
  kv_secrets_controller = merge(
    local.kv_secrets_runner,
    { "gitlab-token" = "gitlab-token" },
    { for k, _ in var.kv_bootstrap_secrets : k => k if startswith(k, "gitlab-token--") },
    var.jira_url != "" ? { "jira-api-token" = "jira-api-token" } : {}
  )
}

# --- Controller Container App ---

resource "azurerm_container_app" "controller" {
  name                         = "ca-controller"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.controller.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.controller.id
  }

  template {
    min_replicas = var.controller_min_replicas
    max_replicas = var.controller_max_replicas

    container {
      name   = "controller"
      image  = local.acr_image
      cpu    = 0.5
      memory = "1Gi"

      env {
        name  = "GITLAB_URL"
        value = var.gitlab_url
      }
      env {
        name  = "GITLAB_POLL"
        value = "true"
      }
      env {
        name  = "GITLAB_PROJECTS"
        value = var.gitlab_projects
      }
      env {
        name  = "TASK_EXECUTOR"
        value = "container_apps"
      }
      env {
        name  = "DISPATCH_BACKEND"
        value = "azure_storage"
      }
      env {
        name  = "AZURE_STORAGE_ACCOUNT_URL"
        value = azurerm_storage_account.tasks.primary_blob_endpoint
      }
      env {
        name  = "AZURE_STORAGE_QUEUE_URL"
        value = azurerm_storage_account.tasks.primary_queue_endpoint
      }
      env {
        name  = "COPILOT_MODEL"
        value = var.copilot_model
      }
      env {
        name  = "ACA_SUBSCRIPTION_ID"
        value = var.subscription_id
      }
      env {
        name  = "ACA_RESOURCE_GROUP"
        value = var.resource_group_name
      }
      env {
        name  = "ACA_JOB_NAME"
        value = "job-task-runner"
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.controller.client_id
      }
      env {
        name  = "OTEL_SERVICE_NAME"
        value = "controller"
      }
      env {
        name  = "DEPLOYMENT_ENV"
        value = var.deployment_env
      }

      # Jira (non-secret env vars — only set when jira_url is provided)
      dynamic "env" {
        for_each = var.jira_url != "" ? [1] : []
        content {
          name  = "JIRA_URL"
          value = var.jira_url
        }
      }
      dynamic "env" {
        for_each = var.jira_url != "" ? [1] : []
        content {
          name  = "JIRA_EMAIL"
          value = var.jira_email
        }
      }
      dynamic "env" {
        for_each = var.jira_url != "" ? [1] : []
        content {
          name  = "JIRA_PROJECT_MAP"
          value = var.jira_project_map
        }
      }
      dynamic "env" {
        for_each = var.jira_url != "" ? [1] : []
        content {
          name  = "JIRA_TRIGGER_STATUS"
          value = var.jira_trigger_status
        }
      }
      dynamic "env" {
        for_each = var.jira_url != "" ? [1] : []
        content {
          name  = "JIRA_IN_REVIEW_STATUS"
          value = var.jira_in_review_status
        }
      }

      # BYOK provider config (only set when copilot_auth='byok')
      dynamic "env" {
        for_each = var.copilot_auth == "byok" ? [1] : []
        content {
          name  = "COPILOT_PROVIDER_TYPE"
          value = var.copilot_provider_type
        }
      }
      dynamic "env" {
        for_each = var.copilot_auth == "byok" ? [1] : []
        content {
          name  = "COPILOT_PROVIDER_BASE_URL"
          value = var.copilot_provider_base_url
        }
      }

      # S1: Secrets via Key Vault references
      dynamic "env" {
        for_each = local.kv_secrets_controller
        content {
          name        = upper(replace(env.key, "-", "_"))
          secret_name = env.key
        }
      }

      liveness_probe {
        transport = "HTTP"
        path      = "/health"
        port      = 8000
      }
    }
  }

  dynamic "secret" {
    for_each = local.kv_secrets_controller
    content {
      name                = secret.key
      key_vault_secret_id = "${azurerm_key_vault.main.vault_uri}secrets/${secret.value}"
      identity            = azurerm_user_assigned_identity.controller.id
    }
  }

  depends_on = [null_resource.kv_seed_secrets, null_resource.acr_import]

  tags = var.tags
}

# --- Task Runner Container Apps Job ---

resource "azurerm_container_app_job" "task_runner" {
  name                         = "job-task-runner"
  location                     = azurerm_resource_group.main.location
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  replica_timeout_in_seconds   = var.job_timeout

  event_trigger_config {
    parallelism              = 1
    replica_completion_count = 1

    scale {
      min_executions              = 0
      max_executions              = 10
      polling_interval_in_seconds = 10

      rules {
        name             = "queue-trigger"
        custom_rule_type = "azure-queue"
        metadata = {
          queueName   = "task-queue"
          queueLength = "1"
          accountName = azurerm_storage_account.tasks.name
        }
      }
    }
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.job.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.job.id
  }

  template {
    container {
      name    = "task"
      image   = local.acr_image
      cpu     = var.job_cpu
      memory  = var.job_memory
      command = [".venv/bin/python", "-m", "gitlab_copilot_agent.task_runner"]

      env {
        name  = "COPILOT_MODEL"
        value = var.copilot_model
      }
      env {
        name  = "DISPATCH_BACKEND"
        value = "azure_storage"
      }
      env {
        name  = "AZURE_STORAGE_ACCOUNT_URL"
        value = azurerm_storage_account.tasks.primary_blob_endpoint
      }
      env {
        name  = "AZURE_STORAGE_QUEUE_URL"
        value = azurerm_storage_account.tasks.primary_queue_endpoint
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.job.client_id
      }
      env {
        name  = "OTEL_SERVICE_NAME"
        value = "task-runner"
      }
      env {
        name  = "DEPLOYMENT_ENV"
        value = var.deployment_env
      }

      # BYOK provider config (only set when copilot_auth='byok')
      dynamic "env" {
        for_each = var.copilot_auth == "byok" ? [1] : []
        content {
          name  = "COPILOT_PROVIDER_TYPE"
          value = var.copilot_provider_type
        }
      }
      dynamic "env" {
        for_each = var.copilot_auth == "byok" ? [1] : []
        content {
          name  = "COPILOT_PROVIDER_BASE_URL"
          value = var.copilot_provider_base_url
        }
      }

      # S1: Secrets via Key Vault references
      dynamic "env" {
        for_each = local.kv_secrets_runner
        content {
          name        = upper(replace(env.key, "-", "_"))
          secret_name = env.key
        }
      }
    }
  }

  dynamic "secret" {
    for_each = local.kv_secrets_runner
    content {
      name                = secret.key
      key_vault_secret_id = "${azurerm_key_vault.main.vault_uri}secrets/${secret.value}"
      identity            = azurerm_user_assigned_identity.job.id
    }
  }

  depends_on = [null_resource.kv_seed_secrets, null_resource.acr_import]

  tags = var.tags
}

# Controller: Storage Queue and Blob data access
resource "azurerm_role_assignment" "controller_queue_sender" {
  scope                = azurerm_storage_account.tasks.id
  role_definition_name = "Storage Queue Data Contributor"
  principal_id         = azurerm_user_assigned_identity.controller.principal_id
}

resource "azurerm_role_assignment" "controller_blob_contributor" {
  scope                = azurerm_storage_account.tasks.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.controller.principal_id
}

# Job: Storage Queue and Blob data access
resource "azurerm_role_assignment" "job_queue_processor" {
  scope                = azurerm_storage_account.tasks.id
  role_definition_name = "Storage Queue Data Contributor"
  principal_id         = azurerm_user_assigned_identity.job.principal_id
}

resource "azurerm_role_assignment" "job_blob_contributor" {
  scope                = azurerm_storage_account.tasks.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.job.principal_id
}

# Patch KEDA scale rule with managed identity for queue auth.
# The azurerm provider doesn't yet support the `identity` field on scale rules,
# so we use azapi_update_resource to add it after the job is created.
resource "azapi_update_resource" "job_keda_identity" {
  type        = "Microsoft.App/jobs@2024-08-02-preview"
  resource_id = azurerm_container_app_job.task_runner.id

  body = {
    properties = {
      configuration = {
        eventTriggerConfig = {
          scale = {
            rules = [
              {
                name = "queue-trigger"
                type = "azure-queue"
                metadata = {
                  queueName   = "task-queue"
                  queueLength = "1"
                  accountName = azurerm_storage_account.tasks.name
                }
                identity = azurerm_user_assigned_identity.job.id
              }
            ]
          }
        }
      }
    }
  }
}

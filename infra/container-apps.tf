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

# S1: Key Vault secret refs — base set shared by all workloads, plus controller-only
locals {
  kv_secrets_base = {
    "gitlab-token"    = "gitlab-token"
    "github-token"    = "github-token"
    "copilot-api-key" = "copilot-api-key"
  }
  kv_secrets_controller = merge(
    local.kv_secrets_base,
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
      image  = var.controller_image
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
        name  = "STATE_BACKEND"
        value = "redis"
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
        name  = "REDIS_HOST"
        value = azurerm_redis_cache.main.hostname
      }
      env {
        name  = "REDIS_PORT"
        value = tostring(azurerm_redis_cache.main.ssl_port)
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
        value = "dev"
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

  depends_on = [null_resource.kv_seed_secrets]

  tags = var.tags
}

# --- Task Runner Container Apps Job ---

resource "azurerm_container_app_job" "task_runner" {
  name                         = "job-task-runner"
  location                     = azurerm_resource_group.main.location
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  replica_timeout_in_seconds   = var.job_timeout

  manual_trigger_config {
    parallelism              = 1
    replica_completion_count = 1
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
      image   = var.job_image
      cpu     = var.job_cpu
      memory  = var.job_memory
      command = [".venv/bin/python", "-m", "gitlab_copilot_agent.task_runner"]

      env {
        name  = "GITLAB_URL"
        value = var.gitlab_url
      }
      env {
        name  = "COPILOT_MODEL"
        value = var.copilot_model
      }
      env {
        name  = "REDIS_HOST"
        value = azurerm_redis_cache.main.hostname
      }
      env {
        name  = "REDIS_PORT"
        value = tostring(azurerm_redis_cache.main.ssl_port)
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
        value = "dev"
      }

      # S1: Secrets via Key Vault references
      dynamic "env" {
        for_each = local.kv_secrets_base
        content {
          name        = upper(replace(env.key, "-", "_"))
          secret_name = env.key
        }
      }
    }
  }

  dynamic "secret" {
    for_each = local.kv_secrets_base
    content {
      name                = secret.key
      key_vault_secret_id = "${azurerm_key_vault.main.vault_uri}secrets/${secret.value}"
      identity            = azurerm_user_assigned_identity.job.id
    }
  }

  depends_on = [null_resource.kv_seed_secrets]

  tags = var.tags
}

# Controller: permission to start job executions via ARM API
resource "azurerm_role_assignment" "controller_job_start" {
  scope                = azurerm_container_app_job.task_runner.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.controller.principal_id
}

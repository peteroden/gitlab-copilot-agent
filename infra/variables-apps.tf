# App-specific variables (extends variables.tf)

# --- Networking ---

variable "vnet_address_space" {
  description = "Address space for the VNet"
  type        = list(string)
  default     = ["10.0.0.0/16"]
}

variable "infra_subnet_prefix" {
  description = "CIDR for the Container Apps infrastructure subnet"
  type        = string
  default     = "10.0.0.0/23"
}

variable "kv_subnet_prefix" {
  description = "CIDR for the Key Vault private endpoint subnet"
  type        = string
  default     = "10.0.3.0/24"
}

variable "storage_subnet_prefix" {
  description = "CIDR for the Storage private endpoint subnet"
  type        = string
  default     = "10.0.4.0/24"
}

# --- Container Apps ---

variable "image_tag" {
  description = "Image tag to deploy (must exist in GHCR)"
  type        = string
}

variable "ghcr_image" {
  description = "GHCR image path (without tag), e.g. peteroden/gitlab-copilot-agent"
  type        = string
  default     = "peteroden/gitlab-copilot-agent"
}

variable "job_cpu" {
  description = "CPU cores for job executions"
  type        = number
  default     = 1.0
}

variable "job_memory" {
  description = "Memory (Gi) for job executions"
  type        = string
  default     = "2Gi"
}

variable "job_timeout" {
  description = "Job execution timeout in seconds"
  type        = number
  default     = 600
}

variable "controller_min_replicas" {
  description = "Minimum replicas for the controller (0 = scale to zero)"
  type        = number
  default     = 0
}

variable "controller_max_replicas" {
  description = "Maximum replicas for the controller"
  type        = number
  default     = 1
}

# --- GitLab / Copilot ---

variable "gitlab_url" {
  description = "GitLab instance URL"
  type        = string
}

variable "gitlab_projects" {
  description = "Comma-separated GitLab project paths or IDs to scope poller"
  type        = string
}

variable "copilot_model" {
  description = "LLM model name for Copilot sessions"
  type        = string
  default     = "gpt-4"
}

variable "copilot_auth" {
  description = "LLM authentication mode: 'github_token' (GitHub PAT via Copilot SDK) or 'byok' (bring-your-own-key via COPILOT_PROVIDER_API_KEY)"
  type        = string
  default     = "github_token"

  validation {
    condition     = contains(["github_token", "byok"], var.copilot_auth)
    error_message = "copilot_auth must be 'github_token' or 'byok'"
  }
}

variable "copilot_provider_type" {
  description = "BYOK provider type (e.g. 'azure_openai', 'openai'). Required when copilot_auth='byok'."
  type        = string
  default     = ""
}

variable "copilot_provider_base_url" {
  description = "BYOK provider base URL. Required when copilot_auth='byok'."
  type        = string
  default     = ""
}

# --- Jira (optional — leave empty to skip) ---

variable "jira_url" {
  description = "Jira instance URL (empty to disable Jira integration)"
  type        = string
  default     = ""
}

variable "jira_email" {
  description = "Jira user email for basic auth"
  type        = string
  default     = ""
}

variable "jira_project_map" {
  description = "JSON mapping Jira project keys to GitLab project config"
  type        = string
  default     = ""
}

# --- Key Vault Bootstrap ---

variable "kv_bootstrap" {
  description = "Enable to seed KV secrets in a single apply. Opens public access, seeds secrets, deploys apps, then closes public access."
  type        = bool
  default     = false
}

variable "kv_bootstrap_secrets" {
  description = "Map of secret-name → value to seed into Key Vault. Only used when kv_bootstrap=true."
  type        = map(string)
  default     = {}
  sensitive   = true
}

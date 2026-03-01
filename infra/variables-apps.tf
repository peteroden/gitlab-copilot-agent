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

variable "redis_subnet_prefix" {
  description = "CIDR for the Redis private endpoint subnet"
  type        = string
  default     = "10.0.2.0/24"
}

# --- Redis ---

variable "redis_sku" {
  description = "Redis tier (Basic for dev, Standard/Premium for prod)"
  type        = string
  default     = "Basic"
}

variable "redis_capacity" {
  description = "Redis cache size (0=250MB, 1=1GB, etc.)"
  type        = number
  default     = 0
}

# --- Container Apps ---

variable "controller_image" {
  description = "Container image for the controller"
  type        = string
}

variable "job_image" {
  description = "Container image for the task runner job"
  type        = string
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

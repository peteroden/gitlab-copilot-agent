# App-specific variables (extends variables.tf from PR5)

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

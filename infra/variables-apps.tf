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

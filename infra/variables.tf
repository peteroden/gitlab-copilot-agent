# --- Required ---

variable "subscription_id" {
  description = "Azure subscription ID"
  type        = string
}

variable "resource_group_name" {
  description = "Name of the resource group to create"
  type        = string
}

variable "location" {
  description = "Azure region (e.g., eastus2)"
  type        = string
  default     = "eastus2"
}

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

# --- Tags ---

variable "tags" {
  description = "Tags applied to all resources"
  type        = map(string)
  default = {
    project    = "gitlab-copilot-agent"
    managed_by = "terraform"
  }
}

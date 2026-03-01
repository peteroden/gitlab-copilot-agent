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

# --- Tags ---

variable "tags" {
  description = "Tags applied to all resources"
  type        = map(string)
  default = {
    project    = "gitlab-copilot-agent"
    managed_by = "terraform"
  }
}

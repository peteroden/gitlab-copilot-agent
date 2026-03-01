# Azure Container Apps Infrastructure

Terraform modules for deploying the GitLab Copilot Agent to Azure Container Apps.

## Prerequisites

- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.5
- Azure CLI (`az login` authenticated)
- Azure subscription with Contributor access

## Quick Start

```bash
# 1. Create a backend storage account (one-time)
az group create -n rg-tfstate -l eastus2
az storage account create -n <unique-name> -g rg-tfstate --sku Standard_LRS
az storage container create -n tfstate --account-name <unique-name>

# 2. Create your tfvars file
cp dev.tfvars.example dev.tfvars
# Edit dev.tfvars with your values

# 3. Initialize and apply
terraform init \
  -backend-config="resource_group_name=rg-tfstate" \
  -backend-config="storage_account_name=<unique-name>" \
  -backend-config="container_name=tfstate" \
  -backend-config="key=gitlab-copilot-agent.tfstate"

terraform plan -var-file=dev.tfvars
terraform apply -var-file=dev.tfvars
```

## Files

| File | Purpose |
|------|---------|
| `main.tf` | Provider config, resource group, backend |
| `backend.tf` | Remote state backend documentation |
| `variables.tf` | Input variables with defaults |
| `outputs.tf` | Resource outputs (FQDN, ACR URL, etc.) |
| `networking.tf` | VNet, subnets, NSGs |
| `redis.tf` | Azure Cache for Redis |
| `keyvault.tf` | Key Vault + access policies |
| `container-apps.tf` | Environment, Controller App, Job |
| `monitoring.tf` | Log Analytics workspace |

## Security

See [ADR-0004](../docs/adr/0004-azure-container-apps-migration.md) for architecture
decisions and security requirements (S1–S6).

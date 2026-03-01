# S2: Terraform state backend — Azure Storage Account with encryption at rest.
#
# Initialize with:
#   terraform init \
#     -backend-config="resource_group_name=<state-rg>" \
#     -backend-config="storage_account_name=<state-sa>" \
#     -backend-config="container_name=tfstate" \
#     -backend-config="key=gitlab-copilot-agent.tfstate"
#
# Prerequisites (created manually or via bootstrap script):
#   - Storage account with blob versioning enabled
#   - Container named "tfstate"
#   - State locking via blob lease (automatic with azurerm backend)

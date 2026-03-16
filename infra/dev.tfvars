subscription_id     = "__AZURE_SUBSCRIPTION_ID__"
resource_group_name = "rg-copilot-dev"
location            = "eastus2"
gitlab_url          = "https://gitlab.com"
gitlab_projects     = "peteroden/copilot-demo,peteroden/e2e-storage-test"
jira_url            = "https://peteroden.atlassian.net"
jira_email          = "__JIRA_EMAIL__"
jira_project_map    = "{\"mappings\":{\"DEMO\":{\"repo\":\"peteroden/copilot-demo\",\"target_branch\":\"main\",\"credential_ref\":\"default\"},\"E2ETEST\":{\"repo\":\"peteroden/e2e-storage-test\",\"target_branch\":\"main\",\"credential_ref\":\"default\"}}}"
kv_bootstrap        = true
# kv_bootstrap_secrets: omitted here — pipeline injects via TF_VAR_kv_bootstrap_secrets
# to avoid var-file precedence overriding the env var.

subscription_id       = "__AZURE_SUBSCRIPTION_ID__"
resource_group_name   = "rg-copilot-staging"
location              = "eastus2"
deployment_env        = "staging"
gitlab_url            = "https://gitlab.com"
gitlab_projects       = "peteroden/copilot-demo,peteroden/e2e-storage-test"
jira_url              = "https://peteroden.atlassian.net"
jira_email            = "__JIRA_EMAIL__"
jira_project_map      = ""
kv_bootstrap          = true
# kv_bootstrap_secrets: omitted here — pipeline injects via TF_VAR_kv_bootstrap_secrets
# to avoid var-file precedence overriding the env var.

subscription_id       = "__AZURE_SUBSCRIPTION_ID__"
resource_group_name   = "rg-copilot-staging"
location              = "eastus2"
deployment_env        = "staging"
gitlab_url            = "https://gitlab.com"
gitlab_projects       = "peteroden/gitlab-copilot-staging"
jira_url              = "https://peteroden.atlassian.net"
jira_email            = "__JIRA_EMAIL__"
jira_project_map      = "{\"mappings\":{\"STAGING\":{\"repo\":\"peteroden/gitlab-copilot-staging\",\"target_branch\":\"main\",\"credential_ref\":\"default\"}}}"
jira_trigger_status   = "Selected for Development"
jira_in_review_status = "Done"
kv_bootstrap          = true
kv_secret_names       = ["gitlab-token", "github-token", "jira-api-token"]
# kv_bootstrap_secrets: omitted here — pipeline injects via TF_VAR_kv_bootstrap_secrets
# to avoid var-file precedence overriding the env var.

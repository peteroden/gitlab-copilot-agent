subscription_id       = "__AZURE_SUBSCRIPTION_ID__"
resource_group_name   = "rg-copilot-dev"
location              = "eastus2"
deployment_env        = "dev"
gitlab_url            = "https://gitlab.com"
gitlab_projects       = "peteroden/gitlab-copilot-dev,peteroden/e2e-aca-test"
jira_url              = "https://peteroden.atlassian.net"
jira_email            = "__JIRA_EMAIL__"
jira_project_map      = "{\"mappings\":{\"DEV\":{\"repo\":\"peteroden/gitlab-copilot-dev\",\"target_branch\":\"main\",\"credential_ref\":\"default\"},\"E2EACA\":{\"repo\":\"peteroden/e2e-aca-test\",\"target_branch\":\"main\",\"credential_ref\":\"default\"}}}"
jira_trigger_status   = "Selected for Development"
jira_in_review_status = "Done"
kv_bootstrap          = true
# kv_bootstrap_secrets: omitted here — pipeline injects via TF_VAR_kv_bootstrap_secrets
# to avoid var-file precedence overriding the env var.

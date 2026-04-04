subscription_id       = "__AZURE_SUBSCRIPTION_ID__"
resource_group_name   = "rg-copilot-staging"
location              = "eastus2"
deployment_env        = "staging"
gitlab_url            = "https://gitlab.com"
gitlab_projects       = "peteroden/gitlab-copilot-staging,peteroden/copilot-demo"
jira_url              = "https://peteroden.atlassian.net"
jira_email            = "__JIRA_EMAIL__"
jira_project_map      = "{\"mappings\":{\"STAGING\":{\"repo\":\"peteroden/gitlab-copilot-staging\",\"target_branch\":\"main\",\"credential_ref\":\"default\"},\"DEMO\":{\"repo\":\"peteroden/copilot-demo\",\"target_branch\":\"main\",\"credential_ref\":\"copilot_demo\"}}}"
jira_trigger_status   = "Selected for Development"
jira_in_review_status = "Done"
kv_bootstrap          = true
# kv_bootstrap_secrets: injected via TF_VAR_kv_bootstrap_secrets in deploy.yml.
# See docs/wiki/configuration-reference.md "Adding a per-project GitLab token".

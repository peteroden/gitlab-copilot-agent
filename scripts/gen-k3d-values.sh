#!/usr/bin/env bash
# Generate Helm values YAML for k3d deployment from a .env.k3d file.
# Usage: gen-k3d-values.sh [env-file]
set -euo pipefail
ENV_FILE="${1:-.env.k3d}"
# shellcheck source=/dev/null
. "./${ENV_FILE}"
cat <<EOF
image: {repository: gitlab-copilot-agent, tag: local}
gitlab: {url: "${GITLAB_URL}", token: "${GITLAB_TOKEN}", webhookSecret: "${GITLAB_WEBHOOK_SECRET}"}
github: {token: "${GITHUB_TOKEN:-}"}
controller: {copilotProviderType: "${COPILOT_PROVIDER_TYPE:-}", copilotProviderBaseUrl: "${COPILOT_PROVIDER_BASE_URL:-}", copilotProviderApiKey: "${COPILOT_PROVIDER_API_KEY:-}"}
EOF

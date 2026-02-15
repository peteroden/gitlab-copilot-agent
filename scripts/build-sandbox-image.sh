#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# Extract CLI version from Dockerfile.sandbox
CLI_VERSION=$(grep '@github/copilot@' "$REPO_ROOT/Dockerfile.sandbox" | sed 's/.*@github\/copilot@\([^ ]*\).*/\1/')

echo "Building sandbox image (CLI ${CLI_VERSION})"
docker build \
  -t "copilot-cli-sandbox:${CLI_VERSION}" \
  -t "copilot-cli-sandbox:latest" \
  -f "$REPO_ROOT/Dockerfile.sandbox" "$REPO_ROOT"
echo "Tagged: copilot-cli-sandbox:${CLI_VERSION}, copilot-cli-sandbox:latest"

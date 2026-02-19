#!/usr/bin/env bash
# E2E test: deploy agent to k3d, send webhook, verify review comment posted.
# Prerequisites: k3d cluster running, agent deployed, mock services running on host.
# Usage: ./tests/e2e/run.sh [agent-url] [mock-gitlab-url]
set -euo pipefail

AGENT_URL="${1:-http://localhost:8080}"
MOCK_GITLAB_URL="${2:-http://localhost:9999}"
WEBHOOK_SECRET="e2e-test-secret"
TIMEOUT=120
POLL_INTERVAL=3

echo "=== E2E Test ==="
echo "Agent:       $AGENT_URL"
echo "Mock GitLab: $MOCK_GITLAB_URL"

# 0. Wait for mock services to be ready
echo -n "Waiting for mock services..."
for i in $(seq 1 20); do
    if curl -sf "http://localhost:9999/health" > /dev/null 2>&1 && \
       curl -sf "http://localhost:9998/health" > /dev/null 2>&1; then
        echo " ✅"
        break
    fi
    if [ "$i" -eq 20 ]; then
        echo " ❌ mock services not ready"
        exit 1
    fi
    sleep 1
    echo -n "."
done

# 1. Wait for agent health
echo -n "Waiting for agent health..."
for i in $(seq 1 $((TIMEOUT / POLL_INTERVAL))); do
    if curl -sf "$AGENT_URL/health" > /dev/null 2>&1; then
        echo " ✅"
        break
    fi
    if [ "$i" -eq $((TIMEOUT / POLL_INTERVAL)) ]; then
        echo " ❌ timeout"
        exit 1
    fi
    sleep "$POLL_INTERVAL"
    echo -n "."
done

# 2. Clear any previous discussions
curl -sf -X DELETE "$MOCK_GITLAB_URL/discussions" > /dev/null

# 3. Send MR webhook
echo -n "Sending webhook..."
RESPONSE=$(curl -sf -X POST "$AGENT_URL/webhook" \
    -H "Content-Type: application/json" \
    -H "X-Gitlab-Token: $WEBHOOK_SECRET" \
    -d '{
        "object_kind": "merge_request",
        "user": {"id": 1, "username": "e2e-test"},
        "project": {
            "id": 999,
            "path_with_namespace": "test/e2e-repo",
            "git_http_url": "http://host.k3d.internal:9999/repo.git"
        },
        "object_attributes": {
            "iid": 1,
            "title": "E2E test MR",
            "description": "E2E test",
            "action": "open",
            "source_branch": "main",
            "target_branch": "main",
            "last_commit": {"id": "abc123", "message": "test"},
            "url": "http://mock/mr/1",
            "oldrev": null
        }
    }')

STATUS=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
if [ "$STATUS" = "queued" ]; then
    echo " ✅ queued"
else
    echo " ❌ unexpected: $RESPONSE"
    exit 1
fi

# 4. Poll mock GitLab for posted discussions
echo -n "Waiting for review comments..."
for i in $(seq 1 $((TIMEOUT / POLL_INTERVAL))); do
    COUNT=$(curl -sf "$MOCK_GITLAB_URL/discussions" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
    if [ "$COUNT" -gt 0 ]; then
        echo " ✅ ($COUNT comments posted)"
        echo ""
        echo "=== E2E PASSED ==="
        exit 0
    fi
    sleep "$POLL_INTERVAL"
    echo -n "."
done

echo " ❌ timeout — no comments posted in ${TIMEOUT}s"
echo ""
echo "=== Agent logs ==="
kubectl logs -l app.kubernetes.io/name=gitlab-copilot-agent --tail=50 2>/dev/null || true
exit 1

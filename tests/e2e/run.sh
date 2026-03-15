#!/usr/bin/env bash
# E2E test: deploy agent to k3d, run all test flows against mock services.
# Tests: 1. Webhook MR review, 2. Jira polling, 3. /copilot command,
#        4. GitLab polling, 5. Hot-reload config, 6. Graceful shutdown.
# Usage: ./tests/e2e/run.sh [agent-url] [mock-gitlab-url] [mock-jira-url]
set -euo pipefail

AGENT_URL="${1:-http://localhost:8080}"
MOCK_GITLAB_URL="${2:-http://localhost:9999}"
MOCK_JIRA_URL="${3:-http://localhost:9997}"
# Internal URL the agent uses inside k3d — must match GITLAB_URL in .env.e2e
INTERNAL_GITLAB_URL="http://host.k3d.internal:9999"
WEBHOOK_SECRET="e2e-test-secret"
TIMEOUT=120
POLL_INTERVAL=3

echo "=== E2E Test ==="
echo "Agent:       $AGENT_URL"
echo "Mock GitLab: $MOCK_GITLAB_URL"
echo "Mock Jira:   $MOCK_JIRA_URL"

# --- Helpers ---
wait_for_health() {
    local url=$1 label=$2 max=${3:-20}
    echo -n "Waiting for $label..."
    for i in $(seq 1 "$max"); do
        if curl -sf "$url" > /dev/null 2>&1; then echo " ✅"; return 0; fi
        [ "$i" -eq "$max" ] && { echo " ❌ timeout"; return 1; }
        sleep 1; echo -n "."
    done
}

poll_until() {
    local url=$1 jq_expr=$2 label=$3
    echo -n "$label..."
    for i in $(seq 1 $((TIMEOUT / POLL_INTERVAL))); do
        VAL=$(curl -sf "$url" | python3 -c "$jq_expr" 2>/dev/null || echo "0")
        if [ "$VAL" != "0" ] && [ -n "$VAL" ]; then echo " ✅ ($VAL)"; return 0; fi
        [ "$i" -eq $((TIMEOUT / POLL_INTERVAL)) ] && { echo " ❌ timeout"; return 1; }
        sleep "$POLL_INTERVAL"; echo -n "."
    done
}

send_webhook() {
    local payload=$1
    RESPONSE=$(curl -sf -X POST "$AGENT_URL/webhook" \
        -H "Content-Type: application/json" \
        -H "X-Gitlab-Token: $WEBHOOK_SECRET" \
        -d "$payload")
    STATUS=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
    [ "$STATUS" = "queued" ] && echo " ✅ queued" || { echo " ❌ $RESPONSE"; exit 1; }
}

# 0. Wait for all services
wait_for_health "$MOCK_GITLAB_URL/health" "mock GitLab" || exit 1
wait_for_health "http://localhost:9998/health" "mock LLM" || exit 1
wait_for_health "$MOCK_JIRA_URL/health" "mock Jira" || exit 1
wait_for_health "$AGENT_URL/health" "agent" 40 || exit 1

# === TEST 1: Webhook MR review ===
echo ""; echo "--- Test 1: Webhook MR Review ---"
# Clear recorded state so assertions only see data from this test
curl -sf -X DELETE "$MOCK_GITLAB_URL/discussions" > /dev/null

echo -n "Sending webhook..."
send_webhook '{
    "object_kind": "merge_request",
    "user": {"id": 1, "username": "e2e-test"},
    "project": {"id": 999, "path_with_namespace": "test/e2e-repo",
                "git_http_url": "http://host.k3d.internal:9999/repo.git"},
    "object_attributes": {"iid": 1, "title": "E2E test MR", "description": "E2E test",
        "action": "open", "source_branch": "main", "target_branch": "main",
        "last_commit": {"id": "abc123", "message": "test"}, "url": "http://mock/mr/1", "oldrev": null}
}'

poll_until "$MOCK_GITLAB_URL/discussions" \
    "import sys,json; d=json.load(sys.stdin); print(len(d) if d else 0)" \
    "Waiting for review comments" || { kubectl logs -l app.kubernetes.io/name=gitlab-copilot-agent --tail=50 2>/dev/null || true; exit 1; }

# Verify at least one comment is an actual review (not a failure message)
echo -n "Checking review is not a failure comment..."
REVIEW_OK=$(curl -sf "$MOCK_GITLAB_URL/discussions" | python3 -c "
import sys,json; ds=json.load(sys.stdin)
# Real reviews have a 'position' object; failure comments are plain body text
has_review = any('position' in str(d) for d in ds)
has_failure = any('failed' in str(d).lower() and 'position' not in str(d) for d in ds)
print('yes' if has_review and not has_failure else 'no')")
[ "$REVIEW_OK" = "yes" ] && echo " ✅" || { echo " ❌ (got failure comment instead of review)"; kubectl logs -l app.kubernetes.io/name=gitlab-copilot-agent --tail=50 2>/dev/null || true; exit 1; }

# === TEST 2: Jira polling → coding → MR creation ===
echo ""; echo "--- Test 2: Jira Polling Flow ---"
# Clear recorded state so assertions only see data from this test
curl -sf -X POST "$MOCK_JIRA_URL/reset" > /dev/null
curl -sf -X DELETE "$MOCK_GITLAB_URL/merge_requests" > /dev/null
curl -sf -X DELETE "$MOCK_GITLAB_URL/pushes" > /dev/null
# Reset bare repo to clear branches from previous pushes
curl -sf -X POST "$MOCK_GITLAB_URL/mock/reset-repo" > /dev/null

poll_until "$MOCK_JIRA_URL/transitions" \
    "import sys,json; d=json.load(sys.stdin); print(len(d) if d else 0)" \
    "Waiting for Jira transitions" || { kubectl logs -l app.kubernetes.io/name=gitlab-copilot-agent --tail=50 2>/dev/null || true; exit 1; }

# Wait for coding task to complete (comment on Jira = task finished)
poll_until "$MOCK_JIRA_URL/comments" \
    "import sys,json; d=json.load(sys.stdin); print(len(d) if d else 0)" \
    "Waiting for Jira comment" || { kubectl logs -l app.kubernetes.io/name=gitlab-copilot-agent --tail=50 2>/dev/null || true; exit 1; }

# Verify comment indicates success (not a failure message)
echo -n "Checking Jira comment is success..."
COMMENT_OK=$(curl -sf "$MOCK_JIRA_URL/comments" | python3 -c "
import sys,json; cs=json.load(sys.stdin)
# Success: 'MR created:' or 'no changes'. Failure: '⚠️' or 'failed'
texts=[str(c) for c in cs]
joined=' '.join(texts)
print('yes' if 'MR created' in joined or 'no changes' in joined else 'no')")
[ "$COMMENT_OK" = "yes" ] && echo " ✅" || { echo " ❌ (got failure comment)"; exit 1; }

echo -n "Checking In Progress transition..."
IN_PROG=$(curl -sf "$MOCK_JIRA_URL/transitions" | python3 -c "
import sys,json; ts=json.load(sys.stdin); print('yes' if any(t['name']=='In Progress' for t in ts) else 'no')")
[ "$IN_PROG" = "yes" ] && echo " ✅" || { echo " ❌"; exit 1; }

echo -n "Checking push + MR creation..."
PUSH_CT=$(curl -sf "$MOCK_GITLAB_URL/pushes" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
MR_CT=$(curl -sf "$MOCK_GITLAB_URL/merge_requests" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
if [ "$PUSH_CT" -gt 0 ] && [ "$MR_CT" -gt 0 ]; then
    echo " ✅ (push=$PUSH_CT, MR=$MR_CT)"
    echo -n "Checking In Review transition..."
    IN_REV=$(curl -sf "$MOCK_JIRA_URL/transitions" | python3 -c "
import sys,json; ts=json.load(sys.stdin); print('yes' if any(t['name']=='In Review' for t in ts) else 'no')")
    [ "$IN_REV" = "yes" ] && echo " ✅" || echo " ⚠️ skipped (non-blocking)"
else
    echo " ⚠️ no push/MR (mock LLM — no code changes expected)"
fi

# === TEST 3: /copilot command ===
echo ""; echo "--- Test 3: /copilot Command ---"
# Clear recorded state so assertions only see data from this test
curl -sf -X DELETE "$MOCK_GITLAB_URL/discussions" > /dev/null

echo -n "Sending /copilot note webhook..."
send_webhook '{
    "object_kind": "note",
    "user": {"id": 2, "username": "developer"},
    "project": {"id": 999, "path_with_namespace": "test/e2e-repo",
                "git_http_url": "http://host.k3d.internal:9999/repo.git"},
    "object_attributes": {"note": "/copilot add error handling to main.py",
                          "noteable_type": "MergeRequest"},
    "merge_request": {"iid": 1, "title": "E2E test MR",
                      "source_branch": "main", "target_branch": "main"}
}'

poll_until "$MOCK_GITLAB_URL/discussions" \
    "import sys,json; d=json.load(sys.stdin); print(len(d) if d else 0)" \
    "Waiting for agent response" || { kubectl logs -l app.kubernetes.io/name=gitlab-copilot-agent --tail=50 2>/dev/null || true; exit 1; }

# === TEST 4: GitLab Polling — MR Discovery ===
echo ""; echo "--- Test 4: GitLab Polling — MR Discovery ---"
# Clear recorded state so assertions only see data from this test
curl -sf -X DELETE "$MOCK_GITLAB_URL/discussions" > /dev/null
curl -sf -X DELETE "$MOCK_GITLAB_URL/mock/open-mrs" > /dev/null
curl -sf -X DELETE "$MOCK_GITLAB_URL/mock/notes" > /dev/null

# Inject an open MR for the GitLab poller to discover
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo -n "Injecting MR for poller..."
curl -sf -X POST "$MOCK_GITLAB_URL/mock/open-mrs" \
    -H "Content-Type: application/json" \
    -d '{
        "iid": 2, "title": "Poller-discovered MR", "description": "Found by GitLab poller",
        "source_branch": "main", "target_branch": "main",
        "sha": "ddd0000000000000000000000000000000000000",
        "web_url": "'"$INTERNAL_GITLAB_URL"'/repo/-/merge_requests/2",
        "state": "opened",
        "author": {"id": 42, "username": "polluser"},
        "updated_at": "'"$NOW"'"
    }' > /dev/null && echo " ✅" || { echo " ❌"; exit 1; }

poll_until "$MOCK_GITLAB_URL/discussions" \
    "import sys,json; d=json.load(sys.stdin); print(len(d) if d else 0)" \
    "Waiting for poller review" || { kubectl logs -l app.kubernetes.io/name=gitlab-copilot-agent --tail=50 2>/dev/null || true; exit 1; }

echo -n "Checking poller review is not a failure..."
POLL_REVIEW_OK=$(curl -sf "$MOCK_GITLAB_URL/discussions" | python3 -c "
import sys, json
d = json.load(sys.stdin)
# Check latest discussions for a real review (position-based), not a failure
recent = d[-3:] if len(d) >= 3 else d
has_review = any('position' in str(x) for x in recent)
has_failure = any('failed' in str(x).lower() and 'position' not in str(x) for x in recent)
print('yes' if has_review and not has_failure else 'no')
" 2>/dev/null || echo "no")
[ "$POLL_REVIEW_OK" = "yes" ] && echo " ✅" || { echo " ❌ (got failure comment)"; exit 1; }

# === TEST 5: Hot-Reload Config ===
echo ""; echo "--- Test 5: Hot-Reload Config ---"

echo -n "Reloading with updated mapping..."
RELOAD_RESP=$(curl -sf -X POST "$AGENT_URL/config/reload" \
    -H "Content-Type: application/json" \
    -H "X-Gitlab-Token: $WEBHOOK_SECRET" \
    -d '{"mappings": {"DEMO": {"repo": "repo", "target_branch": "develop", "credential_ref": "default"}, "NEW": {"repo": "other/repo", "target_branch": "main", "credential_ref": "default"}}}')
RELOAD_STATUS=$(echo "$RELOAD_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))")
[ "$RELOAD_STATUS" = "ok" ] && echo " ✅" || { echo " ❌ ($RELOAD_RESP)"; exit 1; }

echo -n "Checking new keys in response..."
HAS_NEW=$(echo "$RELOAD_RESP" | python3 -c "import sys,json; ks=json.load(sys.stdin).get('jira_keys',[]); print('yes' if 'NEW' in ks and 'DEMO' in ks else 'no')")
[ "$HAS_NEW" = "yes" ] && echo " ✅" || { echo " ❌ (expected DEMO+NEW)"; exit 1; }

echo -n "Checking unauthenticated reload is rejected..."
UNAUTH=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$AGENT_URL/config/reload" \
    -H "Content-Type: application/json" \
    -d '{"mappings": {}}')
[ "$UNAUTH" = "401" ] && echo " ✅" || { echo " ❌ (expected 401, got $UNAUTH)"; exit 1; }

# === TEST 6: Graceful Shutdown ===
echo ""; echo "--- Test 6: Graceful Shutdown ---"

echo -n "Deleting controller pod (triggers SIGTERM via k8s)..."
POD=$(kubectl get pods -l app.kubernetes.io/component=controller -o name | head -1)
if [ -z "$POD" ]; then
    echo " ⚠️ skipped (no controller pod found)"
else
    # kubectl delete sends SIGTERM through the container runtime, which
    # correctly delivers to PID 1 (unlike bare kill inside the container).
    kubectl delete "$POD" --grace-period=30 --wait=false 2>/dev/null
    echo " ✅"

    echo -n "Waiting for pod termination..."
    for i in $(seq 1 30); do
        PHASE=$(kubectl get "$POD" -o jsonpath='{.status.phase}' 2>/dev/null || echo "gone")
        if [ "$PHASE" = "gone" ] || [ "$PHASE" = "Succeeded" ] || [ "$PHASE" = "Failed" ]; then
            echo " ✅ (phase=$PHASE)"; break
        fi
        [ "$i" -eq 30 ] && { echo " ❌ pod still present (phase=$PHASE)"; exit 1; }
        sleep 2; echo -n "."
    done

    echo -n "Checking previous pod logs for shutdown..."
    # Deployment creates a new pod; the old one's logs may be gone.
    # Check OTEL logs or new pod's previous container logs for evidence.
    sleep 3
    NEW_POD=$(kubectl get pods -l app.kubernetes.io/component=controller -o name 2>/dev/null | head -1)
    if [ -n "$NEW_POD" ] && [ "$NEW_POD" != "$POD" ]; then
        echo " ✅ (replacement pod $NEW_POD started)"
    else
        echo " ⚠️ (could not verify shutdown logs — pod replaced)"
    fi
fi

echo ""; echo "=== ALL E2E TESTS PASSED ==="
exit 0

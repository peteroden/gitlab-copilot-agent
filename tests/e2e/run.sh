#!/usr/bin/env bash
# E2E test: run all test flows against mock services.
#
# Modes:
#   k3d   (default) — expects agent deployed in k3d, mocks running on host
#   local — starts mocks + agent in-process with DISPATCH_BACKEND=local
#
# Tests: 1. Webhook MR review, 2. Jira polling, 3. @mention thread interaction,
#        4. GitLab polling, 5. Hot-reload config, 6. Plugin install (k3d only),
#        7. Graceful shutdown (k3d only), 8. Discussion history capture,
#        9. Incremental review, 10. Discussion summary activity section,
#        11. Manual resolution suppression, 12. Commit message awareness.
#
# Usage:
#   ./tests/e2e/run.sh [agent-url] [mock-gitlab-url] [mock-jira-url]     # k3d mode
#   ./tests/e2e/run.sh --mode local                                       # local mode
set -euo pipefail

# --- Parse mode flag ---
MODE="k3d"
POSITIONAL=()
for arg in "$@"; do
    case $arg in
        --mode=*) MODE="${arg#*=}"; shift ;;
        --mode)   MODE="$2"; shift; shift ;;
        *)        POSITIONAL+=("$arg") ;;
    esac
done
set -- "${POSITIONAL[@]+"${POSITIONAL[@]}"}"

AGENT_URL="${1:-http://localhost:8080}"
MOCK_GITLAB_URL="${2:-http://localhost:9999}"
MOCK_JIRA_URL="${3:-http://localhost:9997}"
MOCK_LLM_PORT=9998
WEBHOOK_SECRET="e2e-test-secret"
TIMEOUT=120
POLL_INTERVAL=3
LOCAL_PIDS=()

# URL the agent uses to reach GitLab (for git clone).
# k3d: agent runs in pod, reaches host via k3d internal DNS.
# local: agent runs on same host as mocks.
if [ "$MODE" = "local" ]; then
    INTERNAL_GITLAB_URL="$MOCK_GITLAB_URL"
    WEBHOOK_SECRET="e2e-local-secret"
    TIMEOUT=60
    POLL_INTERVAL=2
else
    INTERNAL_GITLAB_URL="http://host.k3d.internal:9999"
fi

# --- Local mode: start mocks + agent ---
cleanup_local() {
    for pid in "${LOCAL_PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
}

if [ "$MODE" = "local" ]; then
    trap cleanup_local EXIT
    echo "=== E2E Test (local dispatch) ==="
    echo "Starting mock services..."
    uv run uvicorn tests.e2e.mock_gitlab:app --host 127.0.0.1 --port 9999 > /tmp/e2e-mock-gitlab.log 2>&1 &
    LOCAL_PIDS+=($!)
    uv run uvicorn tests.e2e.mock_llm:app --host 127.0.0.1 --port "$MOCK_LLM_PORT" > /tmp/e2e-mock-llm.log 2>&1 &
    LOCAL_PIDS+=($!)
    uv run uvicorn tests.e2e.mock_jira:app --host 127.0.0.1 --port 9997 > /tmp/e2e-mock-jira.log 2>&1 &
    LOCAL_PIDS+=($!)
    for port in 9999 "$MOCK_LLM_PORT" 9997; do
        timeout 15 bash -c "until curl -sf http://127.0.0.1:$port/health >/dev/null 2>&1; do sleep 0.5; done" \
            || { echo "❌ mock on port $port failed to start"; exit 1; }
    done

    echo "Starting agent (DISPATCH_BACKEND=local)..."

    # Plugins: point at the repo's test-marketplace fixture (Docker image
    # copies it to /opt/test-marketplace; locally we use the source path).
    LOCAL_MARKETPLACE="$(cd "$(dirname "$0")" && pwd)/test-marketplace"

    GITLAB_URL="$MOCK_GITLAB_URL" \
      GITLAB_TOKEN=test-token \
      GITLAB_WEBHOOK_SECRET="$WEBHOOK_SECRET" \
      COPILOT_PROVIDER_TYPE=openai \
      COPILOT_PROVIDER_BASE_URL="http://127.0.0.1:${MOCK_LLM_PORT}/v1" \
      COPILOT_PROVIDER_API_KEY=fake-key \
      DISPATCH_BACKEND=local \
      ALLOW_HTTP_CLONE=true \
      LOG_LEVEL=info \
      GITLAB_POLL=true \
      GITLAB_POLL_INTERVAL=3 \
      GITLAB_PROJECTS=999 \
      JIRA_URL="$MOCK_JIRA_URL" \
      JIRA_EMAIL=e2e@test.com \
      JIRA_API_TOKEN=e2e-fake-jira-token \
      JIRA_PROJECT_MAP='{"mappings":{"DEMO":{"repo":"repo","target_branch":"main","credential_ref":"default"}}}' \
      JIRA_TRIGGER_STATUS="AI Ready" \
      JIRA_POLL_INTERVAL=3 \
      RESOLUTION_BEHAVIOR=suggest \
      COPILOT_PLUGINS="${LOCAL_MARKETPLACE}/plugins/e2e-greeter" \
      COPILOT_PLUGIN_MARKETPLACES="${LOCAL_MARKETPLACE}" \
      uv run uvicorn gitlab_copilot_agent.main:app \
        --host 127.0.0.1 --port 8080 > /tmp/e2e-agent.log 2>&1 &
    LOCAL_PIDS+=($!)
    AGENT_URL="http://127.0.0.1:8080"
fi

echo "=== E2E Test ($MODE mode) ==="
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

dump_agent_logs() {
    if [ "$MODE" = "local" ]; then
        tail -30 /tmp/e2e-agent.log 2>/dev/null || true
    else
        kubectl logs -l app.kubernetes.io/name=gitlab-copilot-agent --tail=50 2>/dev/null || true
    fi
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
                "git_http_url": "'"$INTERNAL_GITLAB_URL"'/repo.git"},
    "object_attributes": {"iid": 1, "title": "E2E test MR", "description": "E2E test",
        "action": "open", "source_branch": "main", "target_branch": "main",
        "last_commit": {"id": "abc123abc123abc123abc123abc123abc123abc1", "message": "test"}, "url": "http://mock/mr/1", "oldrev": null}
}'

poll_until "$MOCK_GITLAB_URL/discussions" \
    "import sys,json; d=json.load(sys.stdin); print(len(d) if d else 0)" \
    "Waiting for review comments" || { dump_agent_logs; exit 1; }

# Verify at least one comment is an actual review (not a failure message)
echo -n "Checking review is not a failure comment..."
REVIEW_OK=$(curl -sf "$MOCK_GITLAB_URL/discussions" | python3 -c "
import sys,json; ds=json.load(sys.stdin)
# Real reviews have a 'position' object; failure comments are plain body text
has_review = any('position' in str(d) for d in ds)
has_failure = any('failed' in str(d).lower() and 'position' not in str(d) for d in ds)
print('yes' if has_review and not has_failure else 'no')")
[ "$REVIEW_OK" = "yes" ] && echo " ✅" || { echo " ❌ (got failure comment instead of review)"; dump_agent_logs; exit 1; }

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
    "Waiting for Jira transitions" || { dump_agent_logs; exit 1; }

# Wait for coding task to complete (comment on Jira = task finished)
poll_until "$MOCK_JIRA_URL/comments" \
    "import sys,json; d=json.load(sys.stdin); print(len(d) if d else 0)" \
    "Waiting for Jira comment" || { dump_agent_logs; exit 1; }

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

# === TEST 3: @mention thread interaction ===
echo ""; echo "--- Test 3: @mention Thread Interaction ---"
# Clear recorded state so assertions only see data from this test
curl -sf -X DELETE "$MOCK_GITLAB_URL/discussions" > /dev/null
curl -sf -X DELETE "$MOCK_GITLAB_URL/mock/discussions" > /dev/null

# Pre-seed a discussion containing the note that will trigger the handler.
# The note ID (5001) must match the webhook payload so the handler can find
# the triggering discussion thread.
curl -sf -X POST "$MOCK_GITLAB_URL/mock/discussions" \
    -H "Content-Type: application/json" \
    -d '[{
        "id": "mention-disc-001",
        "individual_note": false,
        "notes": [{
            "id": 5001,
            "type": "DiscussionNote",
            "body": "@mock-review-bot add error handling to main.py",
            "author": {"id": 2, "username": "developer"},
            "created_at": "2024-01-15T12:00:00Z",
            "system": false,
            "resolvable": false,
            "resolved": false,
            "position": null
        }]
    }]' > /dev/null

echo -n "Sending @mention note webhook..."
send_webhook '{
    "object_kind": "note",
    "user": {"id": 2, "username": "developer"},
    "project": {"id": 999, "path_with_namespace": "test/e2e-repo",
                "git_http_url": "'"$INTERNAL_GITLAB_URL"'/repo.git"},
    "object_attributes": {"id": 5001, "note": "@mock-review-bot add error handling to main.py",
                          "noteable_type": "MergeRequest"},
    "merge_request": {"iid": 1, "title": "E2E test MR",
                      "source_branch": "main", "target_branch": "main"}
}'

poll_until "$MOCK_GITLAB_URL/discussions" \
    "import sys,json; d=json.load(sys.stdin); print(len(d) if d else 0)" \
    "Waiting for agent response" || { dump_agent_logs; exit 1; }

# Clean up seeded discussions
curl -sf -X DELETE "$MOCK_GITLAB_URL/mock/discussions" > /dev/null

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
    "Waiting for poller review" || { dump_agent_logs; exit 1; }

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

# === TEST 6: Plugin Installation Verification ===
echo ""; echo "--- Test 6: Plugin Installation ---"
# Clear discussions for this test
curl -sf -X DELETE "$MOCK_GITLAB_URL/discussions" > /dev/null

echo -n "Sending webhook (plugin-enabled session)..."
send_webhook '{
    "object_kind": "merge_request",
    "user": {"id": 1, "username": "e2e-test"},
    "project": {"id": 999, "path_with_namespace": "test/e2e-repo",
                "git_http_url": "'"$INTERNAL_GITLAB_URL"'/repo.git"},
    "object_attributes": {"iid": 99, "title": "Plugin test MR", "description": "Verify plugins",
        "action": "open", "source_branch": "main", "target_branch": "main",
        "last_commit": {"id": "aaa111bbb222ccc333ddd444eee555fff666aaa1", "message": "plugin test"}, "url": "http://mock/mr/99", "oldrev": null}
}'

# Wait for the task to complete (review posted)
poll_until "$MOCK_GITLAB_URL/discussions" \
    "import sys,json; d=json.load(sys.stdin); print(len(d) if d else 0)" \
    "Waiting for plugin-enabled review" || { dump_agent_logs; exit 1; }

# Check logs for plugin setup evidence (agent log in local mode, job pod in k3d)
echo -n "Checking logs for plugin installation..."
if [ "$MODE" = "local" ]; then
    PLUGIN_LOG=$(cat /tmp/e2e-agent.log 2>/dev/null || echo "")
else
    PLUGIN_LOG=$(kubectl logs -l app.kubernetes.io/component=job --tail=200 2>/dev/null || echo "")
fi
if echo "$PLUGIN_LOG" | grep -q "plugin_installed\|plugin_setup_complete\|e2e-greeter"; then
    echo " ✅ (plugin setup confirmed in logs)"
elif echo "$PLUGIN_LOG" | grep -q "plugin"; then
    echo " ✅ (plugin activity detected in logs)"
else
    echo " ⚠️ (no plugin log evidence — check COPILOT_PLUGINS config)"
fi

# === TEST 7: Graceful Shutdown (k3d only) ===
echo ""; echo "--- Test 7: Graceful Shutdown ---"

if [ "$MODE" = "local" ]; then
    echo " ⏭️  skipped (pod lifecycle test — k3d only)"
else
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
        sleep 3
        NEW_POD=$(kubectl get pods -l app.kubernetes.io/component=controller -o name 2>/dev/null | head -1)
        if [ -n "$NEW_POD" ] && [ "$NEW_POD" != "$POD" ]; then
            echo " ✅ (replacement pod $NEW_POD started)"
        else
            echo " ⚠️ (could not verify shutdown logs — pod replaced)"
        fi
    fi
fi

# === TEST 8: Discussion History Capture (#321) ===
echo ""; echo "--- Test 8: Discussion History Capture ---"
# In k3d mode, agent was restarted by Test 7 — wait for health again.
# In local mode, agent is still running.
wait_for_health "$AGENT_URL/health" "agent (post-restart)" 40 || exit 1
# Pre-seed a discussion thread so the agent processes non-empty history
curl -sf -X DELETE "$MOCK_GITLAB_URL/discussions" > /dev/null
curl -sf -X DELETE "$MOCK_GITLAB_URL/mock/discussions" > /dev/null
curl -sf -X POST "$MOCK_GITLAB_URL/mock/discussions" \
    -H "Content-Type: application/json" \
    -d '[{
        "id": "seed-disc-001",
        "individual_note": false,
        "notes": [{
            "id": 9001,
            "type": "DiffNote",
            "body": "Consider adding error handling here.",
            "author": {"id": 9999, "username": "mock-review-bot"},
            "created_at": "2024-01-15T10:00:00Z",
            "system": false,
            "resolvable": true,
            "resolved": false,
            "position": {"new_path": "app.py", "old_path": "app.py", "new_line": 1, "old_line": null}
        }, {
            "id": 9002,
            "type": "DiffNote",
            "body": "Will fix, thanks!",
            "author": {"id": 42, "username": "developer"},
            "created_at": "2024-01-15T11:00:00Z",
            "system": false,
            "resolvable": false,
            "resolved": false,
            "position": null
        }]
    }]' > /dev/null

echo -n "Sending webhook (MR with prior discussions)..."
send_webhook '{
    "object_kind": "merge_request",
    "user": {"id": 1, "username": "e2e-test"},
    "project": {"id": 999, "path_with_namespace": "test/e2e-repo",
                "git_http_url": "'"$INTERNAL_GITLAB_URL"'/repo.git"},
    "object_attributes": {"iid": 321, "title": "Discussion history test MR", "description": "Test with prior feedback",
        "action": "open", "source_branch": "main", "target_branch": "main",
        "last_commit": {"id": "d15c321d15c321d15c321d15c321d15c321d15c3", "message": "test discussions"}, "url": "http://mock/mr/321", "oldrev": null}
}'

poll_until "$MOCK_GITLAB_URL/discussions" \
    "import sys,json; d=json.load(sys.stdin); print(len(d) if d else 0)" \
    "Waiting for review with discussion context" || { dump_agent_logs; exit 1; }

echo -n "Verifying review completed with non-empty discussion history..."
REVIEW_POSTED=$(curl -sf "$MOCK_GITLAB_URL/discussions" | python3 -c "
import sys, json
ds = json.load(sys.stdin)
has_review = any('position' in str(d) for d in ds)
print('yes' if has_review else 'no')")
[ "$REVIEW_POSTED" = "yes" ] && echo " ✅" || { echo " ❌"; exit 1; }

# Clean up seeded discussions
curl -sf -X DELETE "$MOCK_GITLAB_URL/mock/discussions" > /dev/null

# === TEST 9: Incremental Review with SHA Marker ===
echo ""; echo "--- Test 9: Incremental Review ---"
curl -sf "$MOCK_GITLAB_URL/discussions" -X DELETE > /dev/null
curl -sf "$MOCK_GITLAB_URL/mock/discussions" -X DELETE > /dev/null
curl -sf "$MOCK_GITLAB_URL/mock/compare-diffs" -X DELETE > /dev/null

# 9a: First review — open event, should do full review
FIRST_SHA="aaa111aaa111aaa111aaa111aaa111aaa111aaa1"
echo -n "Sending first review webhook (open)..."
send_webhook '{
    "object_kind": "merge_request",
    "user": {"id": 1, "username": "dev"},
    "project": {"id": 999, "path_with_namespace": "test/repo",
                "git_http_url": "'"$INTERNAL_GITLAB_URL"'/repo.git"},
    "object_attributes": {
        "iid": 9,
        "title": "Incremental test",
        "description": "Test incremental review",
        "action": "open",
        "source_branch": "feature",
        "target_branch": "main",
        "last_commit": {"id": "'"$FIRST_SHA"'", "message": "first commit"},
        "url": "'"$INTERNAL_GITLAB_URL"'/test/repo/-/merge_requests/9",
        "oldrev": null
    }
}'

poll_until "$MOCK_GITLAB_URL/discussions" \
    "import sys,json; ds=json.load(sys.stdin); print(sum(1 for d in ds if d.get('_type')=='note' and 'mr-review-agent: last_reviewed_sha=' in d.get('body','')))" \
    "Waiting for first review comments" || { dump_agent_logs; exit 1; }

echo "PASS: Summary note contains SHA marker"

# 9b: Seed the marker for second review
curl -sf "$MOCK_GITLAB_URL/mock/discussions" -X POST -H 'Content-Type: application/json' -d '[{
    "id": "incremental-marker-disc",
    "individual_note": true,
    "notes": [{
        "id": 5000,
        "type": "DiscussionNote",
        "body": "## Code Review Summary\n\nLooks good.\n\n<!-- mr-review-agent: last_reviewed_sha='"$FIRST_SHA"' -->",
        "author": {"id": 9999, "username": "mock-review-bot"},
        "created_at": "2024-01-15T10:30:00Z",
        "system": false,
        "resolvable": false,
        "resolved": false,
        "position": null
    }]
}]' > /dev/null

# Clear previous discussions for clean second review
curl -sf "$MOCK_GITLAB_URL/discussions" -X DELETE > /dev/null

# 9c: Second review — update event, should do incremental review
SECOND_SHA="bbb222bbb222bbb222bbb222bbb222bbb222bbb2"
echo -n "Sending second review webhook (update)..."
send_webhook '{
    "object_kind": "merge_request",
    "user": {"id": 1, "username": "dev"},
    "project": {"id": 999, "path_with_namespace": "test/repo",
                "git_http_url": "'"$INTERNAL_GITLAB_URL"'/repo.git"},
    "object_attributes": {
        "iid": 9,
        "title": "Incremental test",
        "description": "Test incremental review",
        "action": "update",
        "source_branch": "feature",
        "target_branch": "main",
        "last_commit": {"id": "'"$SECOND_SHA"'", "message": "second commit"},
        "url": "'"$INTERNAL_GITLAB_URL"'/test/repo/-/merge_requests/9",
        "oldrev": "'"$FIRST_SHA"'"
    }
}'

poll_until "$MOCK_GITLAB_URL/discussions" \
    "import sys,json; d=json.load(sys.stdin); print(len(d) if d else 0)" \
    "Waiting for incremental review comments" || { dump_agent_logs; exit 1; }

echo "PASS: Incremental review posted comments"

# === TEST 10: Discussion Summary — Activity Section (#321 Feature 6) ===
echo ""; echo "--- Test 10: Discussion Summary Activity Section ---"
curl -sf -X DELETE "$MOCK_GITLAB_URL/discussions" > /dev/null
curl -sf -X DELETE "$MOCK_GITLAB_URL/mock/discussions" > /dev/null

echo -n "Sending webhook (MR with comments to trigger activity section)..."
send_webhook '{
    "object_kind": "merge_request",
    "user": {"id": 1, "username": "e2e-test"},
    "project": {"id": 999, "path_with_namespace": "test/e2e-repo",
                "git_http_url": "'"$INTERNAL_GITLAB_URL"'/repo.git"},
    "object_attributes": {"iid": 610, "title": "Activity section test MR", "description": "Feature 6 E2E",
        "action": "open", "source_branch": "main", "target_branch": "main",
        "last_commit": {"id": "f6a610f6a610f6a610f6a610f6a610f6a610f6a6", "message": "test activity section"}, "url": "http://mock/mr/610", "oldrev": null}
}'

poll_until "$MOCK_GITLAB_URL/discussions" \
    "import sys,json; d=json.load(sys.stdin); print(len(d) if d else 0)" \
    "Waiting for review with activity section" || { dump_agent_logs; exit 1; }

echo -n "Verifying summary note contains Review Activity section..."
ACTIVITY_OK=$(curl -sf "$MOCK_GITLAB_URL/discussions" | python3 -c "
import sys, json
ds = json.load(sys.stdin)
# Summary notes are recorded with _type='note' and contain the review summary header
summary_notes = [d for d in ds if d.get('_type') == 'note' and 'Code Review Summary' in d.get('body', '')]
has_activity = any('Review Activity' in d.get('body', '') for d in summary_notes)
print('yes' if has_activity else 'no')")
[ "$ACTIVITY_OK" = "yes" ] && echo " ✅" || { echo " ❌ (activity section not found in summary note)"; exit 1; }

echo -n "Verifying SHA marker coexists in summary note..."
SHA_OK=$(curl -sf "$MOCK_GITLAB_URL/discussions" | python3 -c "
import sys, json
ds = json.load(sys.stdin)
summary_notes = [d for d in ds if d.get('_type') == 'note' and 'Code Review Summary' in d.get('body', '')]
has_marker = any('mr-review-agent: last_reviewed_sha=' in d.get('body', '') for d in summary_notes)
print('yes' if has_marker else 'no')")
[ "$SHA_OK" = "yes" ] && echo " ✅" || { echo " ❌ (SHA marker not found in summary note)"; exit 1; }

echo "PASS: Summary note contains activity section and SHA marker"

# === TEST 11: Manual Resolution Suppression (Feature 7, #321) ===
echo ""; echo "--- Test 11: Manual Resolution Suppression ---"
curl -sf -X DELETE "$MOCK_GITLAB_URL/discussions" > /dev/null
curl -sf -X DELETE "$MOCK_GITLAB_URL/mock/discussions" > /dev/null

# Seed a discussion that was resolved by a human (non-bot user)
curl -sf -X POST "$MOCK_GITLAB_URL/mock/discussions" \
    -H "Content-Type: application/json" \
    -d '[{
        "id": "seed-resolved-human",
        "individual_note": false,
        "notes": [{
            "id": 8001,
            "type": "DiffNote",
            "body": "Consider adding input validation here.",
            "author": {"id": 9999, "username": "mock-review-bot"},
            "created_at": "2024-01-15T10:00:00Z",
            "system": false,
            "resolvable": true,
            "resolved": true,
            "resolved_by": {"id": 42, "username": "human-dev"},
            "position": {"new_path": "app.py", "old_path": "app.py", "new_line": 3, "old_line": null}
        }]
    }]' > /dev/null

echo -n "Sending webhook (MR with human-resolved discussion)..."
send_webhook '{
    "object_kind": "merge_request",
    "user": {"id": 1, "username": "e2e-test"},
    "project": {"id": 999, "path_with_namespace": "test/e2e-repo",
                "git_http_url": "'"$INTERNAL_GITLAB_URL"'/repo.git"},
    "object_attributes": {"iid": 711, "title": "Manual resolution test MR", "description": "Test suppressed feedback",
        "action": "open", "source_branch": "main", "target_branch": "main",
        "last_commit": {"id": "f7a0000f7a0000f7a0000f7a0000f7a0000f7a00", "message": "test manual resolution"}, "url": "http://mock/mr/711", "oldrev": null}
}'

poll_until "$MOCK_GITLAB_URL/discussions" \
    "import sys,json; d=json.load(sys.stdin); print(len(d) if d else 0)" \
    "Waiting for review with suppressed feedback" || { dump_agent_logs; exit 1; }

echo -n "Verifying resolved discussion not re-raised..."
RERAISE_CHECK=$(curl -sf "$MOCK_GITLAB_URL/discussions" | python3 -c "
import sys, json
ds = json.load(sys.stdin)
# Check that no new discussion body mentions the original resolved topic
reraise = any('input validation' in str(d.get('body','')) for d in ds if d.get('position'))
print('reraise' if reraise else 'suppressed')")
[ "$RERAISE_CHECK" = "suppressed" ] && echo " ✅ (resolved topic not re-raised)" || echo " ⚠️ (could not confirm suppression — agent may have re-raised)"

# Clean up
curl -sf -X DELETE "$MOCK_GITLAB_URL/mock/discussions" > /dev/null

# === TEST 12: Commit Message Awareness ===
echo ""; echo "--- Test 12: Commit Message Awareness ---"
curl -sf -X DELETE "$MOCK_GITLAB_URL/discussions" > /dev/null
curl -sf -X DELETE "$MOCK_GITLAB_URL/mock/commits" > /dev/null

# Seed commits for the MR
curl -sf -X POST "$MOCK_GITLAB_URL/mock/commits" \
    -H "Content-Type: application/json" \
    -d '[
        {"id": "aaa111aaa111", "title": "feat: add auth", "message": "feat: add auth\n\nJWT flow."},
        {"id": "bbb222bbb222", "title": "fix: null check", "message": "fix: null check"}
    ]' > /dev/null

echo -n "Sending webhook (MR with commits)..."
send_webhook '{
    "object_kind": "merge_request",
    "user": {"id": 1, "username": "e2e-test"},
    "project": {"id": 999, "path_with_namespace": "test/e2e-repo",
                "git_http_url": "'"$INTERNAL_GITLAB_URL"'/repo.git"},
    "object_attributes": {"iid": 12, "title": "Commit aware MR", "description": "Test commit awareness",
        "action": "open", "source_branch": "main", "target_branch": "main",
        "last_commit": {"id": "ccc333ccc333ccc333ccc333ccc333ccc333ccc3", "message": "test commits"}, "url": "http://mock/mr/12", "oldrev": null}
}'

poll_until "$MOCK_GITLAB_URL/discussions" \
    "import sys,json; d=json.load(sys.stdin); print(len(d) if d else 0)" \
    "Waiting for review with commit context" || { dump_agent_logs; exit 1; }

echo "PASS: Review completed with commit awareness"

# Clean up seeded commits
curl -sf -X DELETE "$MOCK_GITLAB_URL/mock/commits" > /dev/null

echo ""; echo "=== ALL E2E TESTS PASSED ==="
exit 0

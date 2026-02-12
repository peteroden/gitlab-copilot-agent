# GitLab API v4 — Reference for MR Review Service

Reference for the GitLab REST API endpoints and webhook payloads used in this project.

## Webhook: Merge Request Events

**Trigger**: Configure a project webhook in GitLab with the "Merge request events" checkbox. Set a secret token for validation.

**Headers sent by GitLab:**
- `X-Gitlab-Token` — The secret token configured on the webhook (used for validation)
- `X-Gitlab-Event: Merge Request Hook`
- `Content-Type: application/json`

### Payload Structure (relevant fields)

```json
{
  "object_kind": "merge_request",
  "event_type": "merge_request",
  "user": {
    "id": 1,
    "name": "Jane Doe",
    "username": "jdoe"
  },
  "project": {
    "id": 42,
    "name": "my-project",
    "web_url": "https://gitlab.com/group/my-project",
    "path_with_namespace": "group/my-project",
    "git_http_url": "https://gitlab.com/group/my-project.git",
    "git_ssh_url": "git@gitlab.com:group/my-project.git"
  },
  "object_attributes": {
    "id": 12345,
    "iid": 7,
    "title": "Add new feature",
    "description": "Implements feature X",
    "state": "opened",
    "action": "open",
    "source_branch": "feature/new-feature",
    "target_branch": "main",
    "source_project_id": 42,
    "target_project_id": 42,
    "last_commit": {
      "id": "abc123def456",
      "message": "feat: implement feature X",
      "author_name": "Jane Doe"
    },
    "merge_status": "can_be_merged",
    "url": "https://gitlab.com/group/my-project/-/merge_requests/7",
    "work_in_progress": false
  },
  "labels": [],
  "changes": {}
}
```

### Key Fields

- `object_kind` — Always `"merge_request"` for MR events
- `object_attributes.action` — The trigger action:
  - `"open"` — MR created
  - `"update"` — MR updated (new commits, description change, etc.)
  - `"merge"` — MR merged
  - `"close"` — MR closed
  - `"approved"` — MR approved
  - `"unapproved"` — MR approval revoked
- `object_attributes.iid` — MR number within the project (use this for API calls)
- `project.id` — Numeric project ID (use this for API calls)
- `project.git_http_url` — Clone URL

**For this service, we handle `action in ["open", "update"]` only.**

## GET Merge Request Changes (Diff)

```
GET /api/v4/projects/:id/merge_requests/:merge_request_iid/changes
```

**Headers:** `PRIVATE-TOKEN: <token>`

### Response (relevant fields)

```json
{
  "id": 12345,
  "iid": 7,
  "title": "Add new feature",
  "description": "Implements feature X",
  "state": "opened",
  "diff_refs": {
    "base_sha": "aaa111",
    "head_sha": "bbb222",
    "start_sha": "ccc333"
  },
  "changes": [
    {
      "old_path": "src/main.py",
      "new_path": "src/main.py",
      "a_mode": "100644",
      "b_mode": "100644",
      "new_file": false,
      "renamed_file": false,
      "deleted_file": false,
      "diff": "@@ -10,6 +10,8 @@ def main():\n     print(\"hello\")\n+    print(\"world\")\n+    return 0\n     pass\n"
    }
  ]
}
```

### Key Fields

- `diff_refs.base_sha`, `diff_refs.head_sha`, `diff_refs.start_sha` — Required for inline comment positioning
- `changes[].diff` — Unified diff hunk (no file header, just `@@` hunks)
- `changes[].old_path` / `changes[].new_path` — File paths
- `changes[].new_file` / `changes[].deleted_file` / `changes[].renamed_file` — File status flags

**Note:** The `diff` field contains only hunks, not full unified diff headers (`--- a/file` / `+++ b/file`).

## POST Merge Request Note (General Comment)

```
POST /api/v4/projects/:id/merge_requests/:merge_request_iid/notes
```

**Headers:** `PRIVATE-TOKEN: <token>`

**Body:**
```json
{
  "body": "## Code Review Summary\n\nOverall the changes look good..."
}
```

**Response:** Returns the created note object with `id`, `body`, `created_at`, etc.

## POST Merge Request Discussion (Inline Comment)

```
POST /api/v4/projects/:id/merge_requests/:merge_request_iid/discussions
```

**Headers:** `PRIVATE-TOKEN: <token>`

**Body:**
```json
{
  "body": "Consider using a constant here instead of a magic number.",
  "position": {
    "base_sha": "aaa111",
    "start_sha": "ccc333",
    "head_sha": "bbb222",
    "position_type": "text",
    "old_path": "src/main.py",
    "new_path": "src/main.py",
    "new_line": 12
  }
}
```

### Position Object Fields

| Field | Type | Description |
|-------|------|-------------|
| `base_sha` | string | SHA of the base commit (from `diff_refs`) |
| `start_sha` | string | SHA of the start commit (from `diff_refs`) |
| `head_sha` | string | SHA of the head commit (from `diff_refs`) |
| `position_type` | string | Always `"text"` for line comments |
| `old_path` | string | File path in the base version |
| `new_path` | string | File path in the head version |
| `new_line` | int | Line number in the new file (for added/modified lines) |
| `old_line` | int | Line number in the old file (for removed lines) |

**Rules:**
- For a comment on an **added line**: set `new_line`, omit `old_line`
- For a comment on a **removed line**: set `old_line`, omit `new_line`
- For a comment on an **unchanged context line**: set both `old_line` and `new_line`
- SHAs must match the `diff_refs` from the MR changes endpoint exactly

## python-gitlab Library

```python
import gitlab

gl = gitlab.Gitlab("https://gitlab.com", private_token="TOKEN")
project = gl.projects.get(PROJECT_ID)
mr = project.mergerequests.get(MR_IID)

# Get MR changes (diff)
changes = mr.changes()  # Returns dict with "changes" key

# Post general comment
mr.notes.create({"body": "Review summary here"})

# Post inline discussion
mr.discussions.create({
    "body": "Consider refactoring this",
    "position": {
        "base_sha": "aaa111",
        "start_sha": "ccc333",
        "head_sha": "bbb222",
        "position_type": "text",
        "new_path": "src/main.py",
        "old_path": "src/main.py",
        "new_line": 12,
    },
})
```

## Authentication

- **API calls**: `PRIVATE-TOKEN` header or `python-gitlab` client with `private_token`
- **Git clone**: Use `https://oauth2:<token>@gitlab.com/group/project.git` for HTTPS clone with token auth
- **Webhook validation**: Compare `X-Gitlab-Token` header against configured secret (constant-time comparison)

## Rate Limits

GitLab.com default: 300 requests per minute per user. Self-hosted instances may differ. The service should handle 429 responses with retry-after backoff.

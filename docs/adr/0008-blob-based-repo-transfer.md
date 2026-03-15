# 0008. Blob-Based Repo Transfer for Task Runner Pods

## Status

Accepted

## Context

In the K8s/ACA task execution flow, the controller clones a GitLab repo and then dispatches work to a Job pod. Previously, the Job pod re-cloned the repo using `GITLAB_TOKEN` passed as an environment variable. This had two problems:

1. **Security**: Every Job pod received `GITLAB_TOKEN`, expanding the blast radius if a pod were compromised (e.g., via prompt injection leading to env var exfiltration).
2. **Redundancy**: The repo was cloned twice — once by the controller and once by the runner — wasting network bandwidth and increasing latency.

With the introduction of per-project credentials (ADR-0007), passing the correct token to each pod became more complex and error-prone.

## Options Considered

### Option A: Pass per-project token to each Job pod

- Pros: Minimal architecture change.
- Cons: Requires dynamic secret injection per job. Increases token exposure surface. Still clones twice.

### Option B: Controller uploads repo tarball to blob; runner downloads from blob

- Pros: Runner needs zero GitLab credentials. Single clone. Reuses existing Azure Storage infrastructure (same `task-data` blob container). Tarball transfer is faster than a second git clone for large repos.
- Cons: Additional blob storage usage (tarballs are ephemeral, cleaned up by TTL). Slightly more complex executor code.

### Option C: Shared volume mount (PVC or emptyDir)

- Pros: No network transfer. Fastest option.
- Cons: Requires co-scheduling controller and runner on the same node (or ReadWriteMany PVC). Not compatible with ACA. Breaks separation between controller and runner lifecycles.

## Decision

Option B. The controller tars the already-cloned repo, uploads it to Azure Blob Storage under `repos/{task_id}.tar.gz`, and includes `repo_blob_key` in the task parameters. The runner downloads and extracts the tarball instead of cloning.

Key implementation details:

- **Blob operations** added to `TaskQueue` protocol (`upload_blob`/`download_blob`), reusing the existing `task-data` container alongside `params/` and `results/` prefixes.
- **Tarball security**: `.git/config` is excluded via a custom `tarfile` filter (`_exclude_git_credentials`) to prevent clone-URL token leakage. Extraction uses `tarfile.extractall(filter="data")` (Python 3.12+) which strips device nodes, setuid bits, and symlinks.
- **Blob key validation**: Runner validates `repo_blob_key` starts with `repos/` prefix (defense-in-depth against path traversal).
- **Helm secret isolation**: `scaledjob.yaml` uses explicit `secretKeyRef` entries instead of `envFrom: secretRef`, so runner pods receive only `AZURE_STORAGE_CONNECTION_STRING`, `GITHUB_TOKEN`, and `COPILOT_PROVIDER_API_KEY`.

## Consequences

- **Positive**: Runner pods have zero GitLab credentials. Eliminates redundant git clone. Simplifies credential management for multi-project setups.
- **Positive**: Reduced network egress — blob transfer stays within Azure (or in-cluster Azurite for dev), while git clone required external GitLab API access from every pod. A private endpoint can be added for production hardening.
- **Negative**: Blob storage cost for ephemeral tarballs (mitigated by TTL cleanup).
- **Negative**: Tarball size for very large repos may be significant (mitigated by shallow clones at the controller level).
- **Future**: Issue #289 tracks splitting into slim container images so the runner image excludes GitLab client dependencies entirely.

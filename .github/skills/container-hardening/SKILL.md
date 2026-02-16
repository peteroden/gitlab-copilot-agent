---
name: container-hardening
description: Production Dockerfile and container runtime security checklist. Use this when building Docker/Podman images or configuring container deployments.
---

## Production Dockerfile Checklist

- [ ] Multi-stage build (build deps don't ship in final image)
- [ ] Base image pinned to digest or specific version (not `latest`)
- [ ] `ca-certificates` installed (slim/minimal images often lack them — HTTPS fails silently)
- [ ] Non-root user created and set with `USER`
- [ ] `apt-get clean && rm -rf /var/lib/apt/lists/*` after installs
- [ ] `--no-install-recommends` on all `apt-get install` commands
- [ ] No secrets in build args or layers (use runtime env vars or mounted secrets)

## Runtime Hardening Flags

Apply defense-in-depth when running containers:

| Flag | Purpose |
|------|---------|
| `--read-only` | Immutable root filesystem — prevents writes outside designated paths |
| `--tmpfs /tmp` | Writable temp space on an otherwise read-only container |
| `--cap-drop=ALL` | Drop all Linux capabilities — add back only what's needed |
| `--security-opt=no-new-privileges` | Prevent privilege escalation via setuid/setgid |
| `--cpus=N --memory=Xg` | Resource limits — prevent noisy neighbors and runaway processes |
| `--pids-limit=N` | Fork bomb protection |
| `--pull=never` | Only use pre-built images — no surprise pulls in production |

## Common Gotchas

These are easy to miss and hard to debug:

| Problem | Symptom | Fix |
|---------|---------|-----|
| `--read-only` blocks app writes | App crashes at startup writing to home/cache dir | Add targeted `--tmpfs /path` for specific writable paths |
| `--tmpfs` defaults to `noexec` | Native binaries or shared objects fail to load from tmpfs | Use `--tmpfs /path:exec` to allow execution |
| Missing `-i` flag on `docker run` | stdin closed — any pipe-based IPC (JSON-RPC, language servers) hangs | Always add `-i` when the container process reads from stdin |
| Slim base image missing `ca-certificates` | HTTPS/TLS connections fail with certificate errors | Install `ca-certificates` package in Dockerfile |
| Env vars propagated to child containers | Service secrets (API keys, tokens) leak to sandboxed processes | Maintain an explicit allowlist of env vars passed to child processes |

## Nested Containers (Docker-in-Docker)

When you need containers inside containers (CI/CD, sandboxing, testing):

### Approaches

| Approach | How | Tradeoff |
|----------|-----|----------|
| **DinD sidecar** | Run `docker:dind` as a separate container, connect via `DOCKER_HOST` | `--privileged` required on sidecar; service and sidecar have separate filesystems |
| **Podman-in-Podman** | Install podman in the service container, runs children directly | `--privileged` required; paths are local (no filesystem divergence) |
| **Socket mount** | Mount host's `/var/run/docker.sock` | Simplest, but **grants host root access** — not suitable for untrusted workloads |

**Both DinD and Podman-in-Podman require `--privileged`.** This is a fundamental constraint of nested containerization, not a workaround.

### Filesystem Path Divergence (DinD)

When using a DinD sidecar, the service container and the sandbox container run on **different Docker daemons**. A bind-mount path like `-v /tmp/work:/workspace` resolves on the daemon's filesystem, not the caller's. If the file only exists in the service container, the sandbox gets an empty directory.

**Fix**: Use a shared Docker volume mounted at the same path in both containers.

### Platform Compatibility

| Host runtime | Docker DinD | Podman-in-Podman |
|-------------|-------------|-----------------|
| Docker Desktop (LinuxKit VM) | ✅ | ❌ No nested user namespaces |
| Podman Machine (Fedora CoreOS) | ✅ | ✅ |
| Linux host (native) | ✅ | ✅ |

Document these constraints early — debugging nested container failures on an unsupported platform wastes significant time.

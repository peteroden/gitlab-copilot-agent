#!/bin/sh
set -e

IMAGE="${SANDBOX_IMAGE:-copilot-cli-sandbox:latest}"
METHOD="${SANDBOX_METHOD:-bwrap}"

# Build sandbox image into the container runtime's local store
if [ "$METHOD" = "podman" ] && command -v podman >/dev/null 2>&1; then
  if ! podman image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "Building sandbox image ($METHOD): $IMAGE"
    podman build -t "$IMAGE" -f /opt/sandbox/Dockerfile.sandbox /opt/sandbox/
  fi
elif [ "$METHOD" = "docker" ] && command -v docker >/dev/null 2>&1; then
  if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "Building sandbox image ($METHOD): $IMAGE"
    docker build -t "$IMAGE" -f /opt/sandbox/Dockerfile.sandbox /opt/sandbox/
  fi
fi

exec "$@"

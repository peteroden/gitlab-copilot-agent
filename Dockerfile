FROM node:22-slim AS node-base
FROM docker:27-cli AS docker-cli

FROM python:3.12-slim

ARG SANDBOX_METHODS="docker podman bwrap"

COPY --from=node-base /usr/local/bin/node /usr/local/bin/node
COPY --from=node-base /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm

# Stage docker CLI for conditional install
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker.staged

# Install sandbox runtimes based on build arg
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    if echo "$SANDBOX_METHODS" | grep -q "docker"; then \
      mv /usr/local/bin/docker.staged /usr/local/bin/docker; \
    else rm -f /usr/local/bin/docker.staged; fi && \
    if echo "$SANDBOX_METHODS" | grep -q "podman"; then \
      apt-get install -y --no-install-recommends podman slirp4netns uidmap; \
    fi && \
    if echo "$SANDBOX_METHODS" | grep -q "bwrap"; then \
      apt-get install -y --no-install-recommends bubblewrap; \
    fi && \
    npm install -g @github/copilot && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

RUN useradd -m -u 1000 app && \
    echo "app:100000:65536" >> /etc/subuid && \
    echo "app:100000:65536" >> /etc/subgid

# Configure podman: vfs driver (no fuse needed), host networking (no nftables)
RUN mkdir -p /home/app/.config/containers /etc/containers && \
    printf '[storage]\ndriver = "vfs"\n' \
      > /etc/containers/storage.conf && \
    printf '[containers]\nnetns = "host"\n' \
      > /etc/containers/containers.conf && \
    cp /etc/containers/storage.conf /home/app/.config/containers/storage.conf && \
    cp /etc/containers/containers.conf /home/app/.config/containers/containers.conf && \
    chown -R app:app /home/app/.config

# Copy sandbox Dockerfile for on-demand image building
COPY Dockerfile.sandbox /opt/sandbox/Dockerfile.sandbox

USER app

WORKDIR /home/app/app
COPY --chown=app:app pyproject.toml uv.lock ./

RUN uv sync --no-dev --frozen && \
    find .venv -name "copilot" -path "*/bin/copilot" -exec chmod +x {} \;

COPY --chown=app:app src/ src/
COPY --chown=app:app scripts/entrypoint.sh /opt/entrypoint.sh
EXPOSE 8000
ENTRYPOINT ["/opt/entrypoint.sh"]
CMD ["uv", "run", "uvicorn", "gitlab_copilot_agent.main:app", "--host", "0.0.0.0", "--port", "8000"]

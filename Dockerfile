FROM node:22-slim@sha256:80fdb3f57c815e1b638d221f30a826823467c4a56c8f6a8d7aa091cd9b1675ea AS node-base

FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

COPY --from=node-base /usr/local/bin/node /usr/local/bin/node
COPY --from=node-base /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm

RUN apt-get update && apt-get install -y --no-install-recommends git && \
    npm install -g @github/copilot && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

RUN useradd -m -u 1000 app && mkdir -p /home/app/app && chown app:app /home/app/app

USER app

WORKDIR /home/app/app
COPY --chown=app:app pyproject.toml uv.lock ./

RUN uv sync --no-dev --extra kubernetes --extra azure --frozen && \
    find .venv -name "copilot" -path "*/bin/copilot" -exec chmod +x {} \;

COPY --chown=app:app src/ src/
RUN uv sync --no-dev --extra kubernetes --extra azure --frozen

COPY --chown=app:app scripts/entrypoint.sh /opt/entrypoint.sh
COPY --chown=app:app tests/e2e/test-marketplace/ /opt/test-marketplace/
EXPOSE 8000
ENTRYPOINT ["/opt/entrypoint.sh"]
CMD ["uv", "run", "python", "-m", "gitlab_copilot_agent.main"]

FROM node:22-slim@sha256:dd9d21971ec4395903fa6143c2b9267d048ae01ca6d3ea96f16cb30df6187d94 AS node-base

FROM python:3.12-slim@sha256:9e01bf1ae5db7649a236da7be1e94ffbbbdd7a93f867dd0d8d5720d9e1f89fab

COPY --from=node-base /usr/local/bin/node /usr/local/bin/node
COPY --from=node-base /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm

RUN apt-get update && apt-get install -y --no-install-recommends git && \
    npm install -g @github/copilot && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

RUN useradd -m -u 1000 app

USER app

WORKDIR /home/app/app
COPY --chown=app:app pyproject.toml uv.lock ./

RUN uv sync --no-dev --extra kubernetes --frozen && \
    find .venv -name "copilot" -path "*/bin/copilot" -exec chmod +x {} \;

COPY --chown=app:app src/ src/
COPY --chown=app:app scripts/entrypoint.sh /opt/entrypoint.sh
EXPOSE 8000
ENTRYPOINT ["/opt/entrypoint.sh"]
CMD ["uv", "run", "python", "-m", "gitlab_copilot_agent.main"]

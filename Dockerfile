FROM node:22-slim@sha256:9c2c405e3ff9b9afb2873232d24bb06367d649aa3e6259cbe314da59578e81e9 AS node-base

FROM python:3.12-slim@sha256:ccc7089399c8bb65dd1fb3ed6d55efa538a3f5e7fca3f5988ac3b5b87e593bf0

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
EXPOSE 8000
ENTRYPOINT ["/opt/entrypoint.sh"]
CMD ["uv", "run", "python", "-m", "gitlab_copilot_agent.main"]

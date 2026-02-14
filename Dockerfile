FROM node:22-slim AS node-base

FROM python:3.12-slim

COPY --from=node-base /usr/local/bin/node /usr/local/bin/node
COPY --from=node-base /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm

RUN apt-get update && apt-get install -y --no-install-recommends git bubblewrap && \
    npm install -g @github/copilot && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

RUN useradd -m -u 1000 app

WORKDIR /app
RUN chown app:app /app
COPY --chown=app:app pyproject.toml uv.lock ./

USER app
RUN uv sync --no-dev --frozen && \
    find /app/.venv -name "copilot" -path "*/bin/copilot" -exec chmod +x {} \;

COPY --chown=app:app src/ src/
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "gitlab_copilot_agent.main:app", "--host", "0.0.0.0", "--port", "8000"]

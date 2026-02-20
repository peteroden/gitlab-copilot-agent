FROM node:22-slim@sha256:5373f1906319b3a1f291da5d102f4ce5c77ccbe29eb637f072b6c7b70443fc36 AS node-base

FROM python:3.14-slim@sha256:486b8092bfb12997e10d4920897213a06563449c951c5506c2a2cfaf591c599f

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
CMD ["uv", "run", "uvicorn", "gitlab_copilot_agent.main:app", "--host", "0.0.0.0", "--port", "8000"]

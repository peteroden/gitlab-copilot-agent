FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git curl && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    npm install -g @github/copilot && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen

COPY src/ src/

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "gitlab_copilot_agent.main:app", "--host", "0.0.0.0", "--port", "8000"]

.PHONY: lint test build sandbox-image

lint:
	uv run ruff check src/ tests/
	uv run ruff format --check src/ tests/
	uv run mypy src/

test:
	uv run pytest tests/ --cov --cov-report=term-missing --cov-fail-under=90

build:
	docker build -t gitlab-copilot-agent .

sandbox-image:
	./scripts/build-sandbox-image.sh

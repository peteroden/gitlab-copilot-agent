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

# --- k3d local development ---
K3D_CLUSTER    := copilot-agent-dev
K3D_IMAGE      := gitlab-copilot-agent:local
K3D_HOST_PORT  := 8080
HELM_RELEASE   := copilot-agent
HELM_CHART     := helm/gitlab-copilot-agent
HELM_NS        := default
K3D_ENV_FILE   := .env.k3d

.PHONY: k3d-up k3d-down k3d-build k3d-deploy k3d-redeploy k3d-logs k3d-status

k3d-up:
	k3d cluster create $(K3D_CLUSTER) -p "$(K3D_HOST_PORT):8000@loadbalancer" --wait

k3d-down:
	k3d cluster delete $(K3D_CLUSTER)

k3d-build:
	docker build -t $(K3D_IMAGE) .
	k3d image import $(K3D_IMAGE) -c $(K3D_CLUSTER)

k3d-deploy:
	@test -f $(K3D_ENV_FILE) || { echo "Create $(K3D_ENV_FILE) from .env.k3d.example first"; exit 1; }
	@. ./$(K3D_ENV_FILE) && printf '\
	image: {repository: gitlab-copilot-agent, tag: local}\n\
	gitlab: {url: "%s", token: "%s", webhookSecret: "%s"}\n\
	github: {token: "%s"}\n\
	controller: {copilotProviderType: "%s", copilotProviderBaseUrl: "%s", copilotProviderApiKey: "%s"}\n' \
		"$$GITLAB_URL" "$$GITLAB_TOKEN" "$$GITLAB_WEBHOOK_SECRET" \
		"$$GITHUB_TOKEN" \
		"$$COPILOT_PROVIDER_TYPE" "$$COPILOT_PROVIDER_BASE_URL" "$$COPILOT_PROVIDER_API_KEY" \
		> /tmp/k3d-values.yaml
	helm upgrade --install $(HELM_RELEASE) $(HELM_CHART) \
		-f $(HELM_CHART)/values-local.yaml \
		-f /tmp/k3d-values.yaml \
		-n $(HELM_NS) --wait --timeout 60s
	@rm -f /tmp/k3d-values.yaml

k3d-redeploy: k3d-build k3d-deploy

k3d-logs:
	kubectl logs -l app.kubernetes.io/name=gitlab-copilot-agent -f --tail=100

k3d-status:
	@echo "=== Pods ==="
	@kubectl get pods -n $(HELM_NS) -l app.kubernetes.io/instance=$(HELM_RELEASE)
	@echo "\n=== Jobs ==="
	@kubectl get jobs -n $(HELM_NS) --sort-by=.metadata.creationTimestamp
	@echo "\n=== Services ==="
	@kubectl get svc -n $(HELM_NS) -l app.kubernetes.io/instance=$(HELM_RELEASE)

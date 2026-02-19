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
	@./scripts/gen-k3d-values.sh $(K3D_ENV_FILE) > /tmp/k3d-values.yaml
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

# --- E2E integration tests ---
E2E_CLUSTER := copilot-e2e
E2E_ENV     := tests/e2e/.env.e2e

.PHONY: e2e-test e2e-up e2e-down

e2e-up:
	k3d cluster create $(E2E_CLUSTER) -p "8080:8000@loadbalancer" --wait

e2e-down:
	-k3d cluster delete $(E2E_CLUSTER) 2>/dev/null

e2e-test: K3D_CLUSTER := $(E2E_CLUSTER)
e2e-test: K3D_ENV_FILE := $(E2E_ENV)
e2e-test:
	@echo "=== Starting mock services ==="
	@uv run python tests/e2e/mock_gitlab.py &
	@uv run python tests/e2e/mock_llm.py &
	@echo "=== Building and deploying ==="
	$(MAKE) k3d-build K3D_CLUSTER=$(E2E_CLUSTER)
	@./scripts/gen-k3d-values.sh $(E2E_ENV) > /tmp/k3d-values.yaml
	@HOST_IP=$$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}' k3d-$(E2E_CLUSTER)-server-0 | head -1) && \
		helm upgrade --install $(HELM_RELEASE) $(HELM_CHART) \
			-f $(HELM_CHART)/values-local.yaml \
			-f /tmp/k3d-values.yaml \
			--set 'extraEnv.ALLOW_HTTP_CLONE=true' \
			--set "hostAliases[0].ip=$$HOST_IP" \
			--set 'hostAliases[0].hostnames[0]=host.k3d.internal' \
			-n $(HELM_NS) --wait --timeout 120s
	@rm -f /tmp/k3d-values.yaml
	@echo "=== Running E2E tests ==="
	@./tests/e2e/run.sh http://localhost:8080 http://localhost:9999

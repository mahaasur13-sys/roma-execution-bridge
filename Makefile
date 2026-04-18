.PHONY: install run bridge cli test lint clean

install:
	pip install -e ".[dev]"

run-bridge:
	uvicorn main:app --host 0.0.0.0 --port 8080 --reload

cli:
	python roma_cli.py run "$(TASK)"

status:
	python roma_cli.py status $(JOB_ID)

logs:
	python roma_cli.py logs $(JOB_ID) -n $(LINES)

list:
	python roma_cli.py list

health:
	python roma_cli.py health

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete

# =============================================================================
# Kubernetes Deployment
# =============================================================================

.PHONY: k8s-deploy k8s-deploy-home k8s-deploy-prod k8s-deploy-all \
        k8s-delete k8s-logs k8s-status k8s-portforward \
        k8s-crd k8s-allinone k8s-kustomize-build

# Prerequisites
k8s-crd:
	@echo "Applying ROMA CRD..."
	kubectl apply -f k8s/roma-crd.yaml
	kubectl apply -f k8s/rbac-roma-executor.yaml
	@echo "CRD installed. RomaTask kind available."

# Deploy to k3s (home cluster) — flat YAML
k8s-deploy-home: k8s-crd
	@echo "Deploying ROMA to home cluster..."
	kubectl apply -f deploy/manifests/all-in-one.yaml
	kubectl -n roma-system get pods,svc,pvc

# Deploy via Kustomize (home cluster overlay)
k8s-deploy-kustomize-home:
	kubectl apply -k deploy/overlays/home-cluster

# Deploy via Kustomize (production overlay)
k8s-deploy-kustomize-prod:
	kubectl apply -k deploy/overlays/production

# All-in-one manifest (single file, no kustomize)
k8s-allinone:
	@echo "Deploying all-in-one manifest..."
	kubectl apply -f deploy/manifests/all-in-one.yaml

# Build kustomize (dry-run)
k8s-kustomize-build:
	kustomize build deploy/overlays/home-cluster
	kustomize build deploy/overlays/production

# Delete
k8s-delete:
	@echo "Deleting ROMA from cluster..."
	kubectl delete -f deploy/manifests/all-in-one.yaml --ignore-not-found=true
	kubectl delete -f k8s/roma-crd.yaml --ignore-not-found=true

# Logs
k8s-logs:
	kubectl -n roma-system logs -l app.kubernetes.io/component=api-server --tail=$(LINES) -f

k8s-logs-gpu:
	kubectl -n roma-system logs -l app.kubernetes.io/component=gpu-worker --tail=$(LINES) -f

# Status
k8s-status:
	@echo "=== ROMA Status ==="
	@kubectl -n roma-system get all,configmap,secret,pvc
	@echo ""
	@echo "=== Pods ==="
	@kubectl -n roma-system get pods -o wide
	@echo ""
	@echo "=== Services ==="
	@kubectl -n roma-system get svc -o wide

# Port-forward (default: localhost:8080)
k8s-portforward:
	kubectl port-forward -n roma-system svc/roma-api-server 8080:8080 &

# =============================================================================
# Sprint 2 — Production Hardening
# =============================================================================

.PHONY: sprint2 sprint2-task1 sprint2-task2 sprint2-task3 sprint2-task4 sprint2-task5

# Sprint 2 — Run all tasks in sequence
sprint2: sprint2-task1 sprint2-task2 sprint2-task3 sprint2-task4 sprint2-task5
	@echo "✅ Sprint 2 complete"

# Task 1: Vault / Sealed Secrets
sprint2-task1:
	@echo "=== Sprint 2 Task 1: Vault + Sealed Secrets ==="
	@mkdir -p deploy/vault deploy/sealed-secrets
	@echo "TODO: implement deploy/vault/values.yaml + deploy/sealed-secrets/"
	@echo "✅ Task 1 placeholder created in deploy/vault/"

# Task 2: API Gateway (rate limiting, tenant routing, branding)
sprint2-task2:
	@echo "=== Sprint 2 Task 2: API Gateway ==="
	@mkdir -p deploy/gateway
	@echo "TODO: implement rate limiting + tenant routing in deploy/gateway/"
	@echo "✅ Task 2 placeholder created in deploy/gateway/"

# Task 3: Stripe Webhook (production-ready)
sprint2-task3:
	@echo "=== Sprint 2 Task 3: Stripe Webhook ==="
	@mkdir -p deploy/stripe-webhook
	@echo "TODO: implement deploy/stripe-webhook/ with idempotent processing"
	@echo "✅ Task 3 placeholder created in deploy/stripe-webhook/"

# Task 4: TLS + cert-manager
sprint2-task4:
	@echo "=== Sprint 2 Task 4: TLS + cert-manager ==="
	@mkdir -p deploy/cert-manager
	@echo "TODO: implement cert-manager + Let's Encrypt in deploy/cert-manager/"
	@echo "✅ Task 4 placeholder created in deploy/cert-manager/"

# Task 5: ROMA CRD + Controller
sprint2-task5:
	@echo "=== Sprint 2 Task 5: ROMA CRD + Controller ==="
	@mkdir -p config/crd/bases config/samples
	@echo "TODO: implement ROMA CRD + Controller in config/crd/"
	@echo "✅ Task 5 placeholder created in config/crd/"

# =============================================================================
# Helm
# =============================================================================

.PHONY: helm-template helm-diff

helm-template:
	helm template roma charts/roma-execution-bridge \
	  --namespace roma-system \
	  --values charts/roma-execution-bridge/values.yaml

helm-diff:
	helm diff upgrade roma charts/roma-execution-bridge \
	  --namespace roma-system \
	  --values charts/roma-execution-bridge/values.yaml || true
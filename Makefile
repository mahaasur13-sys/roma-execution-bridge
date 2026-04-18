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
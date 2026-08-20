#!/bin/bash
# deploy_dcgm.sh — развёртывание DCGM Exporter на GPU-нодах K8s
# Использование: ./deploy_dcgm.sh [--dry-run]
set -euo pipefail

MANIFEST="${MANIFEST:-deploy/manifests/dcgm-exporter.yaml}"
NAMESPACE="${NAMESPACE:-roma}"
PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:9090}"
DRY_RUN="${1:-}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${GREEN}[DCGM]${NC} $*"; }
warn() { echo -e "${YELLOW}[DCGM]${NC} $*"; }
err()  { echo -e "${RED}[DCGM]${NC} $*"; }

echo "╔══════════════════════════════════════════╗"
echo "║  DCGM Exporter Deployment Script        ║"
echo "╚══════════════════════════════════════════╝"

# ── Step 1: Check kubectl ──────────────────────────
if ! command -v kubectl &> /dev/null; then
    err "kubectl not found — cannot deploy to cluster"
    err "Install: https://kubernetes.io/docs/tasks/tools/"
    exit 1
fi
log "✓ kubectl found: $(kubectl version --client --short 2>/dev/null | head -1 || echo 'ok')"

# ── Step 2: Check GPU nodes ────────────────────────
GPU_NODES=$(kubectl get nodes -l nvidia.com/gpu=true -o name 2>/dev/null | wc -l)
if [ "$GPU_NODES" -eq 0 ]; then
    warn "No GPU nodes found (label: nvidia.com/gpu=true)"
    warn "DCGM Exporter will be deployed but won't schedule until GPU nodes join."
    warn "Add GPU nodes: kubectl label node <name> nvidia.com/gpu=true"
else
    log "✓ GPU nodes: $GPU_NODES"
fi

# ── Step 3: Create namespace if needed ─────────────
if ! kubectl get namespace "$NAMESPACE" &> /dev/null; then
    kubectl create namespace "$NAMESPACE"
    log "✓ Namespace created: $NAMESPACE"
fi

# ── Step 4: Apply manifest ─────────────────────────
if [ "$DRY_RUN" = "--dry-run" ]; then
    log "DRY RUN — would apply: $MANIFEST"
    kubectl apply -f "$MANIFEST" --dry-run=client
    exit 0
fi

kubectl apply -f "$MANIFEST"
log "✓ Manifest applied: $MANIFEST"

# ── Step 5: Wait for pods ──────────────────────────
log "Waiting for DCGM Exporter pods (timeout: 120s)..."
if kubectl wait --for=condition=ready pod \
    -l app=dcgm-exporter \
    -n "$NAMESPACE" \
    --timeout=120s 2>/dev/null; then
    log "✓ DCGM Exporter pods are ready"
else
    warn "Timeout waiting for pods — may not be scheduled yet"
    kubectl get pods -l app=dcgm-exporter -n "$NAMESPACE" -o wide
fi

# ── Step 6: Verify metrics in Prometheus ────────────
log "Checking DCGM metrics in Prometheus..."
sleep 10  # give scrape interval time to collect

METRICS_CHECK=(
    "DCGM_FI_DEV_GPU_UTIL"
    "DCGM_FI_DEV_FB_USED"
    "DCGM_FI_DEV_FB_FREE"
    "DCGM_FI_DEV_MEM_CLOCK"
    "DCGM_FI_DEV_POWER_USAGE"
)

ALL_OK=true
for metric in "${METRICS_CHECK[@]}"; do
    COUNT=$(curl -s --data-urlencode "query=count(${metric})" \
        "$PROMETHEUS_URL/api/v1/query" 2>/dev/null | \
        python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('data',{}).get('result',[])))" 2>/dev/null || echo "0")
    
    if [ "$COUNT" -gt 0 ] 2>/dev/null; then
        log "  ✓ $metric: $COUNT series"
    else
        warn "  ✗ $metric: 0 series (Prometheus may not have scraped yet)"
        ALL_OK=false
    fi
done

# ── Summary ─────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════╗"
if $ALL_OK; then
    echo "║  ✅ DCGM Deployed & Verified             ║"
else
    echo "║  ⚠️  DCGM Deployed — waiting for metrics ║"
fi
echo "╚══════════════════════════════════════════╝"
echo ""
echo "Next steps:"
echo "  kubectl -n $NAMESPACE get pods -l app=dcgm-exporter"
echo "  kubectl -n $NAMESPACE logs -l app=dcgm-exporter"
echo "  # Import Grafana dashboard:"
echo "  cat deploy/monitoring/grafana/grafana-gpu-dashboard.json | curl -X POST ..."

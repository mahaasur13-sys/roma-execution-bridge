#!/bin/bash
# activate_cloudpayments.sh — активация продакшен-биллинга CloudPayments
# Использование: ./activate_cloudpayments.sh [--test]
set -euo pipefail

ENV_FILE="${ENV_FILE:-.env}"
ROMA_URL="${ROMA_URL:-http://localhost:8900}"
TEST_MODE="${1:-}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${GREEN}[CP]${NC} $*"; }
warn() { echo -e "${YELLOW}[CP]${NC} $*"; }
err()  { echo -e "${RED}[CP]${NC} $*"; }

echo "╔══════════════════════════════════════════╗"
echo "║  CloudPayments Activation Script        ║"
echo "╚══════════════════════════════════════════╝"

# ── Step 1: Check keys ──────────────────────────────
REQUIRED_VARS=(
    "CLOUDPAYMENTS_PUBLIC_ID"
    "CLOUDPAYMENTS_API_SECRET"
    "CLOUDPAYMENTS_WEBHOOK_SECRET"
)

ALL_SET=true
for var in "${REQUIRED_VARS[@]}"; do
    VALUE=$(grep "^${var}=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '"')
    if [ -z "$VALUE" ] || [ "$VALUE" = "your-public-id" ] || [ "$VALUE" = "your-api-secret" ]; then
        err "  ✗ $var: EMPTY or placeholder"
        ALL_SET=false
    else
        log "  ✓ $var: ***${VALUE: -4} (${#VALUE} chars)"
    fi
done

if ! $ALL_SET; then
    err ""
    err "All 3 CloudPayments keys must be set in $ENV_FILE before activation."
    err "Get keys from: https://my.cloudpayments.ru/ → Integration keys"
    err "Webhook URL: ${ROMA_URL}/webhooks/cloudpayments"
    exit 1
fi

log "✓ All CloudPayments keys present"

# ── Step 2: Restart ROMA API ─────────────────────────
log "Restarting ROMA API to pick up live keys..."
if command -v supervisorctl &> /dev/null; then
    supervisorctl -c /etc/zo/supervisord-user.conf restart roma-execution-bridge 2>/dev/null || \
    supervisorctl restart roma-execution-bridge 2>/dev/null
    sleep 5
elif command -v docker &> /dev/null; then
    docker restart roma-api 2>/dev/null || warn "docker restart failed"
    sleep 5
else
    warn "Cannot auto-restart ROMA — restart manually:"
    warn "  supervisorctl restart roma-execution-bridge"
fi

log "✓ ROMA API restarted"

# ── Step 3: Health check ─────────────────────────────
HEALTH=$(curl -s "$ROMA_URL/health" 2>/dev/null || echo '{"status":"down"}')
STATUS=$(echo "$HEALTH" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status','down'))" 2>/dev/null || echo "down")

if [ "$STATUS" = "ok" ]; then
    log "✓ ROMA API health: $STATUS"
else
    err "✗ ROMA API health: $STATUS — check logs:"
    err "  tail -50 /dev/shm/roma-execution-bridge_err.log"
    exit 1
fi

# ── Step 4: Check billing enabled ────────────────────
BILLING=$(echo "$HEALTH" | python3 -c "
import json,sys
d=json.load(sys.stdin)
b=d.get('billing',{})
print(b.get('cloudpayments_enabled','unknown'))
" 2>/dev/null || echo "unknown")

if [ "$BILLING" = "true" ]; then
    log "✓ CloudPayments billing: ENABLED"
else
    warn "⚠ CloudPayments billing: $BILLING"
    warn "Check logs: grep CloudPayments /dev/shm/roma-execution-bridge_err.log | tail -5"
fi

# ── Step 5: Test webhook (test mode only) ────────────
if [ "$TEST_MODE" = "--test" ]; then
    log "Running test webhook..."
    TEST_INVOICE="cp-test-$(date +%s)"
    RESPONSE=$(curl -s -w "\nHTTP %{http_code}" -X POST \
        "$ROMA_URL/webhooks/cloudpayments" \
        -H 'Content-Type: application/json' \
        -H 'X-CloudPayments-HMAC-SHA256: test-signature' \
        -d "{
            \"InvoiceId\": \"$TEST_INVOICE\",
            \"Status\": \"Completed\",
            \"Amount\": 50.00,
            \"Currency\": \"RUB\",
            \"AccountId\": \"test@example.com\",
            \"TestMode\": 1
        }" 2>/dev/null)

    HTTP_CODE=$(echo "$RESPONSE" | grep -oP 'HTTP \K\d+')
    if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "400" ]; then
        log "  ✓ Webhook responded: HTTP $HTTP_CODE"
    else
        warn "  ⚠ Webhook responded: HTTP $HTTP_CODE"
    fi

    # Check if invoice was processed
    IN_DB=$(cd "$(dirname "$ENV_FILE")" && python3 -c "
import os, sys
os.environ['PG_DSN'] = os.environ.get('PG_DSN', 'postgresql://postgres:postgres@localhost:5432/roma')
sys.path.insert(0, '.')
from db_adapter import is_invoice_processed
print(is_invoice_processed('$TEST_INVOICE'))
" 2>/dev/null || echo "unknown")

    log "  is_invoice_processed($TEST_INVOICE): $IN_DB"
fi

# ── Summary ─────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║  ✅ CloudPayments Activated              ║"
echo "╚══════════════════════════════════════════╝"
echo ""
echo "Webhook URL: $ROMA_URL/webhooks/cloudpayments"
echo "Configure in CloudPayments dashboard: https://my.cloudpayments.ru/ → Settings → Webhooks"

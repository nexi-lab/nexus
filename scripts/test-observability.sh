#!/usr/bin/env bash
# Smoke test for the Nexus observability stack (Issue #761).
#
# Starts the Docker Compose observability services, waits for health,
# verifies key endpoints, and tears down.
#
# Usage:
#   ./scripts/test-observability.sh
#
# Prerequisites:
#   - Docker and Docker Compose v2 installed
#   - nexus-network Docker network exists (or run main compose first)

set -euo pipefail

COMPOSE_FILE="docker-compose.observability.yml"
PROFILE="--profile observability"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

passed=0
failed=0

log_info()  { echo -e "${YELLOW}[INFO]${NC}  $*"; }
log_pass()  { echo -e "${GREEN}[PASS]${NC}  $*"; ((passed++)); }
log_fail()  { echo -e "${RED}[FAIL]${NC}  $*"; ((failed++)); }

# ---------------------------------------------------------------------------
# Ensure the shared network exists
# ---------------------------------------------------------------------------
if ! docker network inspect nexus-network >/dev/null 2>&1; then
    log_info "Creating nexus-network..."
    docker network create nexus-network
fi

# ---------------------------------------------------------------------------
# Start the observability stack
# ---------------------------------------------------------------------------
log_info "Starting observability stack..."
docker compose -f "$COMPOSE_FILE" $PROFILE up -d

cleanup() {
    log_info "Tearing down observability stack..."
    docker compose -f "$COMPOSE_FILE" $PROFILE down -v --remove-orphans 2>/dev/null || true
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Wait for services to be ready
# ---------------------------------------------------------------------------
wait_for() {
    local name="$1" url="$2" max_wait="${3:-30}"
    log_info "Waiting for $name at $url ..."
    for i in $(seq 1 "$max_wait"); do
        if curl -sf "$url" >/dev/null 2>&1; then
            log_pass "$name is ready (${i}s)"
            return 0
        fi
        sleep 1
    done
    log_fail "$name did not become ready within ${max_wait}s"
    return 1
}

wait_for "Grafana"    "http://localhost:3000/api/health"
wait_for "Prometheus" "http://localhost:9090/-/ready"
wait_for "Loki"       "http://localhost:3100/ready"
wait_for "Tempo"      "http://localhost:3200/ready"

# ---------------------------------------------------------------------------
# Verify Prometheus has the nexus scrape target configured
# ---------------------------------------------------------------------------
log_info "Checking Prometheus targets..."
targets=$(curl -sf "http://localhost:9090/api/v1/targets" 2>/dev/null || echo "{}")
if echo "$targets" | grep -q "nexus"; then
    log_pass "Prometheus has nexus target configured"
else
    log_fail "Prometheus nexus target not found"
fi

# ---------------------------------------------------------------------------
# Verify Grafana datasources are provisioned
# ---------------------------------------------------------------------------
log_info "Checking Grafana datasources..."
datasources=$(curl -sf "http://localhost:3000/api/datasources" 2>/dev/null || echo "[]")

for ds in Prometheus Loki Tempo; do
    if echo "$datasources" | grep -q "$ds"; then
        log_pass "Grafana datasource '$ds' provisioned"
    else
        log_fail "Grafana datasource '$ds' missing"
    fi
done

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "======================================"
echo -e "  Results: ${GREEN}${passed} passed${NC}, ${RED}${failed} failed${NC}"
echo "======================================"

if [ "$failed" -gt 0 ]; then
    exit 1
fi

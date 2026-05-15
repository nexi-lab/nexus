#!/usr/bin/env bash
# E2E test for workspace/memory registry REST API running inside Docker.
# Usage: bash tests/e2e/server/test_registry_docker_e2e.sh [IMAGE_TAG]
set -euo pipefail

IMAGE="${1:-nexus-server:registry-e2e}"
CONTAINER="nexus-registry-e2e-$$"
PORT=$((RANDOM + 20000))
BASE_URL="http://127.0.0.1:${PORT}"
PASS=0
FAIL=0

cleanup() {
    echo "--- Cleaning up container ${CONTAINER}..."
    docker rm -f "${CONTAINER}" 2>/dev/null || true
}
trap cleanup EXIT

echo "=== Starting Nexus server from ${IMAGE} ==="
docker run -d --name "${CONTAINER}" \
    -p "${PORT}:2026" \
    --user root \
    -e NEXUS_HOST=0.0.0.0 \
    -e NEXUS_PORT=2026 \
    -e NEXUS_DATA_DIR=/app/data \
    -e NEXUS_SEARCH_DAEMON=false \
    -e NEXUS_RATE_LIMIT_ENABLED=false \
    -e NEXUS_BACKEND_ROOT=/app/data/backend \
    -e NEXUS_DATABASE_URL=sqlite:///app/data/nexus.db \
    --entrypoint "" \
    "${IMAGE}" \
    nexusd --host 0.0.0.0 --port 2026

echo "--- Waiting for health..."
for i in $(seq 1 60); do
    if curl -sf "${BASE_URL}/health" >/dev/null 2>&1; then
        echo "--- Server healthy after ${i}s"
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "FATAL: Server did not start in 60s"
        docker logs "${CONTAINER}" 2>&1 | tail -30
        exit 1
    fi
    sleep 1
done

assert_status() {
    local desc="$1" expected="$2" actual="$3" body="$4"
    if [ "$actual" -eq "$expected" ]; then
        echo "  PASS: ${desc} (${actual})"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: ${desc} — expected ${expected}, got ${actual}"
        echo "        body: ${body}"
        FAIL=$((FAIL + 1))
    fi
}

echo ""
echo "=== Workspace CRUD ==="

# 1. List (empty)
RESP=$(curl -sf -w "\n%{http_code}" "${BASE_URL}/api/v2/registry/workspaces")
CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')
assert_status "GET /workspaces (empty)" 200 "$CODE" "$BODY"

# 2. Register
RESP=$(curl -sf -w "\n%{http_code}" -X POST "${BASE_URL}/api/v2/registry/workspaces" \
    -H "Content-Type: application/json" \
    -d '{"path":"/docker-ws","name":"DockerTest","description":"Docker e2e"}')
CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')
assert_status "POST /workspaces (register)" 201 "$CODE" "$BODY"

# 3. Get
RESP=$(curl -sf -w "\n%{http_code}" "${BASE_URL}/api/v2/registry/workspaces/docker-ws")
CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')
assert_status "GET /workspaces/docker-ws" 200 "$CODE" "$BODY"

# 4. Update
RESP=$(curl -sf -w "\n%{http_code}" -X PATCH "${BASE_URL}/api/v2/registry/workspaces/docker-ws" \
    -H "Content-Type: application/json" \
    -d '{"name":"Updated"}')
CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')
assert_status "PATCH /workspaces/docker-ws" 200 "$CODE" "$BODY"

# 5. Duplicate → 409
RESP=$(curl -s -w "\n%{http_code}" -X POST "${BASE_URL}/api/v2/registry/workspaces" \
    -H "Content-Type: application/json" \
    -d '{"path":"/docker-ws"}')
CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')
assert_status "POST /workspaces (duplicate)" 409 "$CODE" "$BODY"

# 6. Delete
RESP=$(curl -sf -w "\n%{http_code}" -X DELETE "${BASE_URL}/api/v2/registry/workspaces/docker-ws")
CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')
assert_status "DELETE /workspaces/docker-ws" 200 "$CODE" "$BODY"

# 7. Get after delete → 404
RESP=$(curl -s -w "\n%{http_code}" "${BASE_URL}/api/v2/registry/workspaces/docker-ws")
CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')
assert_status "GET /workspaces/docker-ws (after delete)" 404 "$CODE" "$BODY"

echo ""
echo "=== Memory CRUD ==="

# 8. List
RESP=$(curl -sf -w "\n%{http_code}" "${BASE_URL}/api/v2/registry/memories")
CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')
assert_status "GET /memories" 200 "$CODE" "$BODY"

# 9. Register
RESP=$(curl -sf -w "\n%{http_code}" -X POST "${BASE_URL}/api/v2/registry/memories" \
    -H "Content-Type: application/json" \
    -d '{"path":"/docker-mem","name":"DockerMem"}')
CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')
assert_status "POST /memories (register)" 201 "$CODE" "$BODY"

# 10. Get
RESP=$(curl -sf -w "\n%{http_code}" "${BASE_URL}/api/v2/registry/memories/docker-mem")
CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')
assert_status "GET /memories/docker-mem" 200 "$CODE" "$BODY"

# 11. Update
RESP=$(curl -sf -w "\n%{http_code}" -X PATCH "${BASE_URL}/api/v2/registry/memories/docker-mem" \
    -H "Content-Type: application/json" \
    -d '{"name":"UpdatedMem","description":"Updated via Docker"}')
CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')
assert_status "PATCH /memories/docker-mem" 200 "$CODE" "$BODY"

# 12. Duplicate → 409
RESP=$(curl -s -w "\n%{http_code}" -X POST "${BASE_URL}/api/v2/registry/memories" \
    -H "Content-Type: application/json" \
    -d '{"path":"/docker-mem"}')
CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')
assert_status "POST /memories (duplicate)" 409 "$CODE" "$BODY"

# 13. Delete
RESP=$(curl -sf -w "\n%{http_code}" -X DELETE "${BASE_URL}/api/v2/registry/memories/docker-mem")
CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')
assert_status "DELETE /memories/docker-mem" 200 "$CODE" "$BODY"

# 14. Get after delete → 404
RESP=$(curl -s -w "\n%{http_code}" "${BASE_URL}/api/v2/registry/memories/docker-mem")
CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | sed '$d')
assert_status "GET /memories/docker-mem (after delete)" 404 "$CODE" "$BODY"

echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed ==="
if [ "$FAIL" -gt 0 ]; then
    echo "DOCKER E2E FAILED"
    exit 1
fi
echo "DOCKER E2E PASSED"

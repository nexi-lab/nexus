#!/usr/bin/env bash
# Run E2E tests: start Python test server, run TS tests, cleanup.
#
# Usage:
#   cd packages/nexus-pay-ts
#   bash tests/run-e2e.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PKG_DIR="$(dirname "$SCRIPT_DIR")"
REPO_ROOT="$(dirname "$(dirname "$PKG_DIR")")"

PORT=4219
SERVER_PID=""

cleanup() {
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "Stopping E2E test server (PID $SERVER_PID)..."
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}

trap cleanup EXIT

echo "=== NexusPay TypeScript SDK E2E Tests ==="
echo ""

# 1. Start the Python E2E test server
echo "Starting E2E test server on port $PORT..."
cd "$REPO_ROOT"
.venv/bin/python "$SCRIPT_DIR/e2e-server.py" &
SERVER_PID=$!

# 2. Wait for server to be ready
echo "Waiting for server to be ready..."
for i in $(seq 1 30); do
  if curl -s "http://localhost:$PORT/health" > /dev/null 2>&1; then
    echo "Server ready!"
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "ERROR: Server process died. Check Python dependencies."
    exit 1
  fi
  sleep 0.5
done

# Verify server is actually responding
if ! curl -s "http://localhost:$PORT/health" > /dev/null 2>&1; then
  echo "ERROR: Server failed to start within 15 seconds."
  exit 1
fi

# 3. Run TypeScript E2E tests
echo ""
echo "Running E2E tests..."
cd "$PKG_DIR"
E2E_BASE_URL="http://localhost:$PORT" E2E_API_KEY="sk-e2e-test-key" \
  npx vitest run tests/e2e.test.ts

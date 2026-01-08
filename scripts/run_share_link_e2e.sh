#!/bin/bash
# E2E tests for Share Link feature (Issue #227)
#
# This script:
# 1. Ensures PostgreSQL is running
# 2. Creates the nexus database if needed
# 3. Runs the E2E tests
#
# Usage:
#   ./scripts/run_share_link_e2e.sh
#   DATABASE_URL=postgresql://user:pass@host:5432/db ./scripts/run_share_link_e2e.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Default database URL
DATABASE_URL="${DATABASE_URL:-postgresql://postgres:nexus@localhost:5432/nexus}"

echo "============================================================"
echo "Share Link E2E Tests"
echo "============================================================"
echo "Database: $DATABASE_URL"
echo ""

# Check if PostgreSQL is available
echo "Checking PostgreSQL connection..."
if ! pg_isready -h localhost -p 5432 -q 2>/dev/null; then
    echo "PostgreSQL not running. Starting via docker-compose..."
    docker-compose -f "$PROJECT_ROOT/docker-compose.demo.yml" up -d postgres

    # Wait for PostgreSQL to be ready
    echo "Waiting for PostgreSQL to be ready..."
    for i in {1..30}; do
        if pg_isready -h localhost -p 5432 -q 2>/dev/null; then
            break
        fi
        sleep 1
    done

    if ! pg_isready -h localhost -p 5432 -q 2>/dev/null; then
        echo "ERROR: PostgreSQL failed to start"
        exit 1
    fi
fi
echo "PostgreSQL is ready"

# Create database if it doesn't exist
echo "Ensuring 'nexus' database exists..."
psql -h localhost -U postgres -tc "SELECT 1 FROM pg_database WHERE datname = 'nexus'" | grep -q 1 || \
    psql -h localhost -U postgres -c "CREATE DATABASE nexus;" 2>/dev/null || true

# Kill any process on test port
echo "Cleaning up test port 19227..."
lsof -i :19227 -t 2>/dev/null | xargs kill -9 2>/dev/null || true
sleep 1

# Run E2E tests
echo ""
echo "Running E2E tests..."
cd "$PROJECT_ROOT"
PYTHONPATH=src DATABASE_URL="$DATABASE_URL" python3 tests/test_share_link_e2e.py

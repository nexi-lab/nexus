#!/bin/bash
set -e

# Performance Benchmark Orchestration Script
# This script runs comprehensive performance tests in an isolated environment

# Note: Docker context should be set correctly in your environment
# If docker commands fail, ensure Docker Desktop is running

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NEXUS_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BENCHMARK_DB="postgresql://postgres:nexus@localhost:5432/perf_benchmark"
RESULT_DIR="$SCRIPT_DIR/result_$(date +%Y%m%d_%H%M%S)"

echo "================================================================================"
echo "NEXUS PERFORMANCE BENCHMARK SUITE"
echo "================================================================================"
echo "Nexus Root: $NEXUS_ROOT"
echo "Benchmark DB: $BENCHMARK_DB"
echo "Results Dir: $RESULT_DIR"
echo ""

# Step 1: Stop all existing Nexus servers
echo "[1/5] Stopping all existing Nexus servers..."
cd "$NEXUS_ROOT"
# ./local-demo.sh --stop 2>&1 | grep -v "No such file" || true
# echo "  ✓ All servers stopped"
# echo ""

# Step 2: Create/reset benchmark database
echo "[2/5] Setting up benchmark database in Docker PostgreSQL..."

# Check if postgres container is running
if ! docker ps | grep -q nexus-postgres; then
    echo "  ✗ PostgreSQL container (nexus-postgres) is not running"
    echo "  Start it with: docker-compose up -d postgres"
    exit 1
fi

# Drop and recreate database in Docker PostgreSQL container
echo "  Terminating connections to perf_benchmark database..."
docker exec nexus-postgres psql -U postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'perf_benchmark';" 2>/dev/null || {
    echo "  ⚠️  No active connections to terminate"
}

echo "  Dropping existing perf_benchmark database..."
docker exec nexus-postgres psql -U postgres -c "DROP DATABASE IF EXISTS perf_benchmark;" || {
    echo "  ⚠️  Could not drop database (may not exist)"
}

echo "  Creating perf_benchmark database..."
docker exec nexus-postgres psql -U postgres -c "CREATE DATABASE perf_benchmark;" || {
    echo "  ✗ Failed to create database in Docker container"
    exit 1
}
echo "  ✓ Database created: perf_benchmark (in Docker)"
echo ""

# Step 3: Initialize Nexus with benchmark database using local-demo.sh
echo "[3/5] Initializing Nexus server with benchmark database..."
cd "$NEXUS_ROOT"

# Set environment variables
export NEXUS_URL="http://localhost:2026"
export NEXUS_API_KEY="sk-default_admin_dddddddd_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
export NEXUS_RATE_LIMIT_DISABLED="true"  # Disable rate limiting for benchmarks

# Use local-demo.sh --init with custom postgres URL
# This will: stop services, clear data dir, create schema, start server
echo "  Starting server with: ./local-demo.sh --init --postgres-url \"$BENCHMARK_DB\""
AUTO_INIT=true ./local-demo.sh --init --postgres-url "$BENCHMARK_DB" --no-ui --no-langgraph > /tmp/nexus-benchmark-init.log 2>&1 &
INIT_PID=$!

# Wait for initialization to complete
echo "  Waiting for server to initialize..."
for i in {1..60}; do
    if curl -s http://localhost:2026/health > /dev/null 2>&1; then
        echo "  ✓ Server is ready!"
        break
    fi
    if [ $i -eq 60 ]; then
        echo "  ✗ Server failed to start within 60 seconds"
        echo "  Check logs: /tmp/nexus-benchmark-init.log"
        tail -50 /tmp/nexus-benchmark-init.log
        exit 1
    fi
    sleep 1
done
echo ""

# Step 4: Check and generate test data if needed
echo "[4/5] Checking test data..."
DATA_DIR="/tmp/nexus_perf_data"

if [ ! -d "$DATA_DIR" ] || [ -z "$(ls -A $DATA_DIR 2>/dev/null)" ]; then
    echo "  ⚠️  Test data not found at $DATA_DIR"
    echo "  Generating performance test data..."
    cd "$SCRIPT_DIR"
    python3 generate_perf_data.py
    echo "  ✓ Test data generated"
else
    echo "  ✓ Test data exists at $DATA_DIR"
fi
echo ""

# Step 5: Run performance tests
echo "[5/5] Running performance tests..."
mkdir -p "$RESULT_DIR"
cd "$SCRIPT_DIR"

echo ""
echo "  [Test 1/3] Flat directory comparison (1K and 10K files)..."
python3 test_flat_comparison.py > "$RESULT_DIR/flat_test.log" 2>&1
if [ -f "flat_comparison_results.csv" ]; then
    mv flat_comparison_results.csv "$RESULT_DIR/"
    echo "    ✓ Flat test complete"
else
    echo "    ⚠️ Flat test may have failed (check $RESULT_DIR/flat_test.log)"
fi

echo ""
echo "  [Test 2/3] Grep comparison (1K and 10K files)..."
python3 test_grep_comparison.py > "$RESULT_DIR/grep_test.log" 2>&1
if [ -f "grep_comparison_results.csv" ]; then
    mv grep_comparison_results.csv "$RESULT_DIR/"
    echo "    ✓ Grep test complete"
else
    echo "    ⚠️ Grep test may have failed (check $RESULT_DIR/grep_test.log)"
fi

echo ""
echo "  [Test 3/3] Nested directory comparison (1K and 10K files)..."
python3 test_nested_comparison.py > "$RESULT_DIR/nested_test.log" 2>&1
if [ -f "nested_comparison_results.csv" ]; then
    mv nested_comparison_results.csv "$RESULT_DIR/"
    echo "    ✓ Nested test complete"
else
    echo "    ⚠️ Nested test may have failed (check $RESULT_DIR/nested_test.log)"
fi

echo ""
echo "  ✓ All tests complete!"
echo ""

# Generate HTML summary
echo "Generating HTML summary..."
cd "$SCRIPT_DIR"
python3 generate_html_report.py "$RESULT_DIR"
echo ""

# Cleanup: Stop server
echo "Stopping benchmark server..."
cd "$NEXUS_ROOT"
./local-demo.sh --stop 2>&1 | grep -v "No such file" || true
echo "  ✓ Server stopped"
echo ""

# Print summary
echo "================================================================================"
echo "BENCHMARK COMPLETE!"
echo "================================================================================"
echo ""
echo "Results directory: $RESULT_DIR"
echo ""
echo "Files generated:"
echo "  - flat_comparison_results.csv"
echo "  - grep_comparison_results.csv"
echo "  - nested_comparison_results.csv"
echo "  - performance_summary.html"
echo "  - *.log (test output logs)"
echo ""
echo "Quick stats:"
if [ -f "$RESULT_DIR/flat_comparison_results.csv" ]; then
    echo ""
    echo "Flat Directory Results (row count):"
    wc -l "$RESULT_DIR/flat_comparison_results.csv" | awk '{print "  " $1 " rows"}'
fi
if [ -f "$RESULT_DIR/grep_comparison_results.csv" ]; then
    echo ""
    echo "Grep Results (row count):"
    wc -l "$RESULT_DIR/grep_comparison_results.csv" | awk '{print "  " $1 " rows"}'
fi
if [ -f "$RESULT_DIR/nested_comparison_results.csv" ]; then
    echo ""
    echo "Nested Directory Results (row count):"
    wc -l "$RESULT_DIR/nested_comparison_results.csv" | awk '{print "  " $1 " rows"}'
fi
echo ""
echo "View results:"
echo "  open $RESULT_DIR/performance_summary.html"
echo ""
echo "================================================================================"

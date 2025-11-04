#!/usr/bin/env bash
#
# Docker Sandbox Provider Demo (Issue #389)
#
# Demonstrates Docker-based local sandboxes for code execution with Nexus mounting.
#
# Prerequisites:
#   1. Docker installed and running: docker --version
#   2. Nexus server running locally (this script will start one)
#   3. Nexus runtime image (this script will build it automatically)
#
# Usage:
#   ./examples/cli/docker_sandbox_demo.sh
#
# Environment Variables:
#   KEEP=1                    Keep server and sandbox running after demo
#   SKIP_BUILD=1              Skip image build, use python:3.11-slim instead
#   NEXUS_DATA_DIR            Directory for Nexus data (default: /tmp/nexus-sandbox-demo)
#   DOCKER_IMAGE              Docker image to use (default: nexus/runtime:dev - built from local source)

set -e  # Exit on error

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo "=== Nexus Docker Sandbox Demo ==="
echo

# Check if Docker is running
if ! docker info > /dev/null 2>&1; then
    echo -e "${RED}❌ Docker is not running${NC}"
    echo "   Please start Docker and try again"
    exit 1
fi

echo -e "${GREEN}✓${NC} Docker is running"
echo

# Set up data directory
export NEXUS_DATA_DIR="${NEXUS_DATA_DIR:-/tmp/nexus-sandbox-demo}"
rm -rf "$NEXUS_DATA_DIR"
mkdir -p "$NEXUS_DATA_DIR"

echo "Using data directory: $NEXUS_DATA_DIR"
echo

# Build or use Nexus runtime image
# Default: Use dev image built from local source for testing latest changes
if [ -z "$SKIP_BUILD" ]; then
    echo "=== Building Nexus Runtime Image (Dev) ==="

    # Check if dev image already exists
    if docker images nexus/runtime:dev | grep -q nexus/runtime; then
        echo "Image nexus/runtime:dev already exists"
        echo "To rebuild, run: docker rmi nexus/runtime:dev"
        echo -e "${GREEN}✓${NC} Using existing dev image"
    else
        echo "Building nexus/runtime:dev from local source..."
        docker build -f docker/nexus-runtime-dev.Dockerfile -t nexus/runtime:dev .
        echo -e "${GREEN}✓${NC} Dev image built successfully"
    fi
    echo
    export DOCKER_IMAGE="nexus/runtime:dev"
else
    # Use default Python image (nexus CLI will be installed on-demand)
    export DOCKER_IMAGE="${DOCKER_IMAGE:-python:3.11-slim}"
    echo "Using Docker image: $DOCKER_IMAGE (nexus CLI will be installed on-demand)"
    echo
fi

# Start Nexus server in background with authentication
echo "=== Starting Nexus Server ==="
echo "Starting server with database authentication..."

# Kill any existing server on port 8080
lsof -ti:8080 | xargs kill -9 2>/dev/null || true

# Use existing PostgreSQL container
echo "Checking for existing PostgreSQL container..."
if docker ps | grep -q "nexus-postgres"; then
    echo -e "${GREEN}✓${NC} Using existing nexus-postgres container"
    # Configure database URL for existing container
    export NEXUS_DATABASE_URL="postgresql://postgres:nexus@localhost:5432/nexus"
else
    echo "Starting new PostgreSQL container..."
    docker run -d \
        --name nexus-postgres-demo \
        -e POSTGRES_PASSWORD=nexus \
        -e POSTGRES_USER=postgres \
        -e POSTGRES_DB=nexus \
        -p 5432:5432 \
        postgres:16-alpine > /dev/null 2>&1

    # Wait for PostgreSQL to be ready
    echo "Waiting for PostgreSQL to be ready..."
    for i in {1..30}; do
        if docker exec nexus-postgres-demo pg_isready -U postgres > /dev/null 2>&1; then
            echo -e "${GREEN}✓${NC} PostgreSQL is ready"
            break
        fi
        if [ $i -eq 30 ]; then
            echo -e "${RED}❌ PostgreSQL failed to start${NC}"
            docker logs nexus-postgres-demo
            exit 1
        fi
        sleep 1
    done

    # Configure database URL
    export NEXUS_DATABASE_URL="postgresql://postgres:nexus@localhost:5432/nexus"
fi

# Start server with database auth and initialization
echo "Starting server with database authentication..."
nexus serve \
    --auth-type database \
    --init \
    --data-dir "$NEXUS_DATA_DIR" \
    --port 8080 \
    > "$NEXUS_DATA_DIR/server.log" 2>&1 &

SERVER_PID=$!
echo "Server PID: $SERVER_PID"

# Wait for server to be ready
echo "Waiting for server to start..."
for i in {1..30}; do
    if curl -s http://localhost:8080/health > /dev/null 2>&1; then
        echo -e "${GREEN}✓${NC} Server is ready"
        break
    fi
    if [ $i -eq 30 ]; then
        echo -e "${RED}❌ Server failed to start${NC}"
        cat "$NEXUS_DATA_DIR/server.log"
        exit 1
    fi
    sleep 1
done

# Extract API key from server logs
echo "Extracting admin API key..."
NEXUS_API_KEY=$(grep 'Admin API Key:' "$NEXUS_DATA_DIR/server.log" | head -1 | awk '{print $NF}')

if [ -z "$NEXUS_API_KEY" ]; then
    echo -e "${RED}❌ Failed to extract API key${NC}"
    cat "$NEXUS_DATA_DIR/server.log"
    exit 1
fi

export NEXUS_API_KEY
export NEXUS_URL="http://localhost:8080"

echo -e "${GREEN}✓${NC} Server initialized with authentication"
echo -e "${GREEN}✓${NC} API key: ${NEXUS_API_KEY:0:20}..."
echo

# Demo 1: Create test files in Nexus
echo "=== Demo 1: Create Test Files in Nexus ==="
echo "Creating test files..."

nexus mkdir /sandbox-demo --parents
nexus write /sandbox-demo/data.txt "Hello from Nexus!"
nexus write /sandbox-demo/numbers.txt "1,2,3,4,5"
nexus mkdir /sandbox-demo/code --parents
nexus write /sandbox-demo/code/example.py "print('Hello from mounted file!')"

echo -e "${GREEN}✓${NC} Test files created"
nexus ls /sandbox-demo
echo

# Demo 2: Create a Docker sandbox
echo "=== Demo 2: Create Docker Sandbox ==="
echo "Creating sandbox with Docker provider..."

sandbox_name="docker-demo-$(date +%s)"
sandbox_output=$(nexus sandbox create "$sandbox_name" \
    --provider docker \
    --template "$DOCKER_IMAGE" \
    --ttl 30 \
    --json)

sandbox_id=$(echo "$sandbox_output" | jq -r '.sandbox_id')
echo -e "${GREEN}✓${NC} Sandbox created: $sandbox_id"
echo "  Provider: docker"
echo "  Image: $DOCKER_IMAGE"
echo "  TTL: 30 minutes"
echo

# Demo 3: Run Python code
echo "=== Demo 3: Run Python Code ==="
echo "Running Python code in sandbox..."
echo

nexus sandbox run "$sandbox_id" --language python --code "
import sys
import platform

print('=== Environment Info ===')
print(f'Python version: {sys.version}')
print(f'Platform: {platform.platform()}')
print(f'Machine: {platform.machine()}')
print()
print('✓ Python execution successful!')
"
echo

# Demo 4: Mount Nexus filesystem
echo "=== Demo 4: Mount Nexus Filesystem ==="
echo "Mounting Nexus at /mnt/nexus in sandbox..."
echo

# Note: Docker provider automatically transforms localhost to host.docker.internal
nexus sandbox connect "$sandbox_id" \
    --provider docker \
    --mount-path /mnt/nexus

echo

# Test: Simple list operation right after mount
echo "=== Testing List Operation Performance ==="
echo "Listing root directory via FUSE (this tests issue #391)..."
echo

nexus sandbox run "$sandbox_id" --language python --code "
import os
import time

mount_path = '/mnt/nexus'

print('Test 1: List root directory')
start = time.time()
try:
    files = os.listdir(mount_path)
    elapsed = time.time() - start
    print(f'✓ Listed {len(files)} items in {elapsed:.2f}s')
    print(f'  Files: {files}')
except Exception as e:
    elapsed = time.time() - start
    print(f'✗ Failed after {elapsed:.2f}s: {e}')

print()
print('Test 2: List /sandbox-demo')
start = time.time()
try:
    files = os.listdir(f'{mount_path}/sandbox-demo')
    elapsed = time.time() - start
    print(f'✓ Listed {len(files)} items in {elapsed:.2f}s')
    print(f'  Files: {files}')
except Exception as e:
    elapsed = time.time() - start
    print(f'✗ Failed after {elapsed:.2f}s: {e}')
"
echo

# Demo 5: Access mounted files
echo "=== Demo 5: Access Mounted Files ==="
echo "Reading files from mounted Nexus filesystem..."
echo

nexus sandbox run "$sandbox_id" --language python --code "
import os

mount_path = '/mnt/nexus'

print('=== Listing mounted files ===')
try:
    for root, dirs, files in os.walk(f'{mount_path}/sandbox-demo'):
        level = root.replace(f'{mount_path}/sandbox-demo', '').count(os.sep)
        indent = ' ' * 2 * level
        print(f'{indent}{os.path.basename(root)}/')
        subindent = ' ' * 2 * (level + 1)
        for file in files:
            print(f'{subindent}{file}')
except Exception as e:
    print(f'Error listing files: {e}')

print()
print('=== Reading data.txt ===')
try:
    with open(f'{mount_path}/sandbox-demo/data.txt', 'r') as f:
        content = f.read()
        print(f'Content: {content}')
    print()
    print('✓ Successfully read file from mounted Nexus!')
except Exception as e:
    print(f'Error reading file: {e}')

print()
print('=== Reading numbers.txt ===')
try:
    with open(f'{mount_path}/sandbox-demo/numbers.txt', 'r') as f:
        numbers = [int(x) for x in f.read().strip().split(',')]
        print(f'Numbers: {numbers}')
        print(f'Sum: {sum(numbers)}')
        print(f'Average: {sum(numbers)/len(numbers):.1f}')
    print()
    print('✓ Data processing successful!')
except Exception as e:
    print(f'Error processing numbers: {e}')
"
echo

# Demo 6: Execute code from mounted file
echo "=== Demo 6: Execute Code from Mounted File ==="
echo "Running Python code from mounted Nexus file..."
echo

nexus sandbox run "$sandbox_id" --language bash --code "
cd /mnt/nexus/sandbox-demo/code
python example.py
"
echo

# Demo 7: Write results back to Nexus
echo "=== Demo 7: Write Results to Nexus ==="
echo "Creating output file from sandbox..."
echo

nexus sandbox run "$sandbox_id" --language python --code "
import json
from datetime import datetime

mount_path = '/mnt/nexus'

# Generate some results
results = {
    'timestamp': datetime.now().isoformat(),
    'sandbox_id': '$sandbox_id',
    'provider': 'docker',
    'computation': {
        'input': [1, 2, 3, 4, 5],
        'sum': 15,
        'average': 3.0
    }
}

# Write to mounted filesystem
output_path = f'{mount_path}/sandbox-demo/results.json'
with open(output_path, 'w') as f:
    json.dump(results, f, indent=2)

print(f'✓ Results written to {output_path}')
"
echo

echo "Verifying file was created in Nexus..."
nexus cat /sandbox-demo/results.json
echo

# Demo 8: Run JavaScript
echo "=== Demo 8: Run JavaScript Code ==="
echo "Running Node.js code..."
echo

nexus sandbox run "$sandbox_id" --language javascript --code "
const data = [10, 20, 30, 40, 50];
const sum = data.reduce((a, b) => a + b, 0);
const avg = sum / data.length;

console.log('Data:', data);
console.log('Sum:', sum);
console.log('Average:', avg);
console.log('✓ JavaScript execution successful!');
"
echo

# Demo 9: Run Bash commands
echo "=== Demo 9: Run Bash Commands ==="
echo "Running system commands..."
echo

nexus sandbox run "$sandbox_id" --language bash --code "
echo '=== System Info ==='
uname -a
echo
echo '=== Disk Usage ==='
df -h | head -5
echo
echo '=== Mounted Filesystems ==='
mount | grep nexus || echo 'FUSE mount not visible in container'
echo
echo '✓ Bash execution successful!'
"
echo

# Demo 10: Test pause/resume
echo "=== Demo 10: Pause and Resume ==="
echo "Pausing sandbox..."

nexus sandbox pause "$sandbox_id"
echo -e "${GREEN}✓${NC} Sandbox paused"
echo

echo "Resuming sandbox..."
nexus sandbox resume "$sandbox_id"
echo -e "${GREEN}✓${NC} Sandbox resumed"
echo

# Demo 11: Get sandbox status
echo "=== Demo 11: Sandbox Status ==="
echo "Getting detailed status..."
echo

nexus sandbox status "$sandbox_id"
echo

# Demo 12: List all sandboxes
echo "=== Demo 12: List Sandboxes ==="
echo "Listing all sandboxes..."
echo

nexus sandbox list
echo

# Cleanup
echo "=== Cleanup ==="

if [ -n "$KEEP" ]; then
    echo -e "${YELLOW}⚠️  KEEP=1 set - leaving sandbox, server, and database running${NC}"
    echo
    echo "To stop manually:"
    echo "  nexus sandbox stop $sandbox_id"
    echo "  kill $SERVER_PID"
    echo "  docker stop nexus-postgres-demo && docker rm nexus-postgres-demo"
    echo "  rm -rf $NEXUS_DATA_DIR"
    echo
else
    echo "Stopping sandbox..."
    nexus sandbox stop "$sandbox_id"
    echo -e "${GREEN}✓${NC} Sandbox stopped"
    echo

    echo "Stopping server..."
    kill $SERVER_PID
    wait $SERVER_PID 2>/dev/null || true
    echo -e "${GREEN}✓${NC} Server stopped"
    echo

    echo "Stopping PostgreSQL container (if created by demo)..."
    if docker ps -a | grep -q "nexus-postgres-demo"; then
        docker stop nexus-postgres-demo > /dev/null 2>&1 || true
        docker rm nexus-postgres-demo > /dev/null 2>&1 || true
        echo -e "${GREEN}✓${NC} PostgreSQL demo container stopped"
    else
        echo -e "${GREEN}✓${NC} Using existing postgres, leaving it running"
    fi
    echo

    echo "Cleaning up data directory..."
    rm -rf "$NEXUS_DATA_DIR"
    echo -e "${GREEN}✓${NC} Cleanup complete"
    echo
fi

echo "=== Demo Complete! ==="
echo
echo "Summary:"
echo "  ✓ Created test files in Nexus"
echo "  ✓ Created Docker sandbox (local container)"
echo "  ✓ Ran Python code"
echo "  ✓ Mounted Nexus filesystem via FUSE"
echo "  ✓ Accessed mounted files from sandbox"
echo "  ✓ Executed code from mounted file"
echo "  ✓ Wrote results back to Nexus"
echo "  ✓ Ran JavaScript code"
echo "  ✓ Ran Bash commands"
echo "  ✓ Paused and resumed sandbox"
echo "  ✓ Retrieved sandbox status"
echo "  ✓ Listed all sandboxes"
echo
echo "Advantages of Docker provider:"
echo "  • No cloud API key required"
echo "  • Direct localhost access (no ngrok)"
echo "  • Fast FUSE mounting (no network latency)"
echo "  • Easy debugging (docker logs, docker exec)"
echo "  • Free for local development"
echo
echo "Next steps:"
echo "  - Run again (uses cached image): ./examples/cli/docker_sandbox_demo.sh"
echo "  - Skip build (slower): SKIP_BUILD=1 ./examples/cli/docker_sandbox_demo.sh"
echo "  - Rebuild image: docker rmi nexus/runtime:dev && ./examples/cli/docker_sandbox_demo.sh"
echo "  - Use production image: DOCKER_IMAGE=nexus/runtime:latest ./examples/cli/docker_sandbox_demo.sh"
echo "  - Read docs: docs/design/docker-sandbox-provider.md"

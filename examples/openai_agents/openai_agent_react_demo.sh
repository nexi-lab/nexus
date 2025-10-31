#!/bin/bash
# openai_agent_react_demo.sh - Complete OpenAI Agents SDK + Nexus demo
#
# This script:
# 1. Starts Nexus server with authentication (or uses existing server)
# 2. Creates sample Python files with async patterns
# 3. Runs the OpenAI Agent to analyze them
#
# Usage:
#   ./openai_agent_react_demo.sh
#
# Requirements:
#   - OpenAI API key (set OPENAI_API_KEY)
#   - PostgreSQL running (or will use local filesystem)

set -e  # Exit on error

# ============================================
# Configuration
# ============================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
NEXUS_PORT="${NEXUS_PORT:-8080}"
NEXUS_URL="http://localhost:$NEXUS_PORT"

# ============================================
# Banner
# ============================================

cat << 'EOF'
╔═══════════════════════════════════════════════════════════╗
║   OpenAI Agents SDK + Nexus ReAct Demo                   ║
╚═══════════════════════════════════════════════════════════╝
EOF

echo ""
echo "This demo will:"
echo "  1. Start Nexus server with authentication"
echo "  2. Create sample Python files with async patterns"
echo "  3. Run OpenAI Agent to analyze and generate report"
echo ""

# ============================================
# Check Prerequisites
# ============================================

echo "🔍 Checking prerequisites..."
echo ""

# Check OpenAI API key
if [ -z "$OPENAI_API_KEY" ]; then
    echo "❌ Error: OPENAI_API_KEY not set"
    echo ""
    echo "Please set your OpenAI API key:"
    echo "  export OPENAI_API_KEY='sk-...'"
    echo ""
    exit 1
fi
echo "✓ OpenAI API key set"

# Check if nexus is installed
if ! command -v nexus &> /dev/null; then
    echo "❌ Error: 'nexus' command not found"
    echo "   Install with: pip install nexus-ai-fs"
    exit 1
fi
echo "✓ Nexus CLI installed"

# Check if Python dependencies are installed
if ! python3 -c "import agents" 2>/dev/null; then
    echo "❌ Error: openai-agents not installed"
    echo ""
    echo "Install with:"
    echo "  cd $SCRIPT_DIR"
    echo "  pip install -r requirements.txt"
    echo ""
    exit 1
fi
echo "✓ OpenAI Agents SDK installed"

echo ""

# ============================================
# Start or Check Nexus Server
# ============================================

echo "🚀 Setting up Nexus server..."
echo ""

# Check if server is already running
if curl -s "$NEXUS_URL/health" >/dev/null 2>&1; then
    echo "✓ Nexus server already running at $NEXUS_URL"

    # Try to source existing admin env
    if [ -f "$PROJECT_ROOT/.nexus-admin-env" ]; then
        source "$PROJECT_ROOT/.nexus-admin-env"
        echo "✓ Loaded admin credentials from .nexus-admin-env"
    else
        echo "⚠️  Warning: .nexus-admin-env not found"
        echo "   Using existing server without authentication"
        unset NEXUS_API_KEY
    fi
else
    echo "Starting new Nexus server..."
    echo ""

    # Start server in background
    cd "$PROJECT_ROOT"

    # Check if PostgreSQL is available
    if command -v psql &> /dev/null && psql -lqt 2>/dev/null | cut -d \| -f 1 | grep -qw nexus; then
        echo "Using PostgreSQL database..."
        # Use the init script to start server with auth
        ./scripts/init-nexus-with-auth.sh &
        SERVER_PID=$!

        # Wait for server to start and credentials to be created
        echo "Waiting for server to initialize..."
        sleep 5

        # Wait for .nexus-admin-env to be created
        for i in {1..30}; do
            if [ -f ".nexus-admin-env" ]; then
                source .nexus-admin-env
                echo "✓ Server started with authentication"
                break
            fi
            sleep 1
        done

        if [ -z "$NEXUS_API_KEY" ]; then
            echo "⚠️  Warning: Could not load admin API key"
            echo "   Check .nexus-admin-env file"
        fi
    else
        echo "PostgreSQL not available, using local filesystem..."

        # Start simple server without auth in background
        NEXUS_DATA_DIR="./nexus-demo-data" nexus serve --port $NEXUS_PORT &
        SERVER_PID=$!

        echo "Waiting for server to start..."
        sleep 3

        # No authentication needed
        unset NEXUS_API_KEY
        export NEXUS_URL="$NEXUS_URL"

        echo "✓ Server started (no authentication)"
    fi

    # Wait for server to be healthy
    for i in {1..30}; do
        if curl -s "$NEXUS_URL/health" >/dev/null 2>&1; then
            echo "✓ Server is healthy"
            break
        fi
        sleep 1
    done

    if ! curl -s "$NEXUS_URL/health" >/dev/null 2>&1; then
        echo "❌ Error: Server failed to start"
        exit 1
    fi

    # Save PID for cleanup
    echo $SERVER_PID > /tmp/nexus-demo-server.pid
fi

echo ""

# ============================================
# Create Sample Files
# ============================================

echo "📝 Creating sample Python files with async patterns..."
echo ""

# Create sample async Python files
cat > /tmp/async_api.py << 'PYTHON'
"""Sample async API client."""
import asyncio
import aiohttp

async def fetch_data(url: str) -> dict:
    """Fetch data from API endpoint."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            return await response.json()

async def batch_fetch(urls: list[str]) -> list[dict]:
    """Fetch multiple URLs concurrently."""
    tasks = [fetch_data(url) for url in urls]
    return await asyncio.gather(*tasks)

async def main():
    urls = ["https://api.example.com/data1", "https://api.example.com/data2"]
    results = await batch_fetch(urls)
    print(f"Fetched {len(results)} results")

if __name__ == "__main__":
    asyncio.run(main())
PYTHON

cat > /tmp/async_worker.py << 'PYTHON'
"""Sample async task worker."""
import asyncio
from typing import Any

async def process_task(task_id: int, data: Any) -> Any:
    """Process a single task asynchronously."""
    await asyncio.sleep(0.1)  # Simulate async work
    return {"task_id": task_id, "result": f"Processed {data}"}

async def worker(queue: asyncio.Queue, results: list):
    """Worker that processes tasks from queue."""
    while True:
        task = await queue.get()
        if task is None:  # Sentinel to stop worker
            break
        result = await process_task(task["id"], task["data"])
        results.append(result)
        queue.task_done()

async def run_workers(tasks: list, num_workers: int = 3):
    """Run multiple workers to process tasks."""
    queue = asyncio.Queue()
    results = []

    # Start workers
    workers = [asyncio.create_task(worker(queue, results)) for _ in range(num_workers)]

    # Add tasks to queue
    for task in tasks:
        await queue.put(task)

    # Wait for all tasks to be processed
    await queue.join()

    # Stop workers
    for _ in range(num_workers):
        await queue.put(None)
    await asyncio.gather(*workers)

    return results
PYTHON

cat > /tmp/regular_utils.py << 'PYTHON'
"""Regular synchronous utilities (no async)."""

def format_data(data: dict) -> str:
    """Format data as string."""
    return ", ".join(f"{k}={v}" for k, v in data.items())

def validate_input(value: str) -> bool:
    """Validate input string."""
    return len(value) > 0 and value.isalnum()

class DataProcessor:
    """Process data synchronously."""

    def __init__(self):
        self.cache = {}

    def process(self, key: str, value: Any) -> Any:
        """Process and cache data."""
        if key in self.cache:
            return self.cache[key]

        result = f"Processed: {value}"
        self.cache[key] = result
        return result
PYTHON

# Upload files to Nexus
echo "Uploading files to /workspace..."

# Set environment for nexus CLI
export NEXUS_URL="$NEXUS_URL"

# Create workspace directory
nexus mkdir /workspace 2>/dev/null || true

# Upload the files
nexus write /workspace/async_api.py --input /tmp/async_api.py
nexus write /workspace/async_worker.py --input /tmp/async_worker.py
nexus write /workspace/regular_utils.py --input /tmp/regular_utils.py

echo "✓ Created 3 sample Python files in /workspace"
echo "  - async_api.py (async HTTP client)"
echo "  - async_worker.py (async task worker)"
echo "  - regular_utils.py (synchronous utilities)"
echo ""

# ============================================
# Run OpenAI Agent Demo
# ============================================

echo "╔═══════════════════════════════════════════════════════════╗"
echo "║   Running OpenAI Agent to Analyze Async Patterns         ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""

cd "$SCRIPT_DIR"

# Run the Python demo
python3 openai_agent_react_demo.py

# ============================================
# Show Results
# ============================================

echo ""
echo "╔═══════════════════════════════════════════════════════════╗"
echo "║   Checking Generated Report                              ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""

# Try to read the generated report
if nexus cat /reports/async-patterns.md 2>/dev/null; then
    echo ""
    echo "✓ Report successfully generated at /reports/async-patterns.md"
else
    echo "⚠️  Report not found (agent may have written to local filesystem)"

    # Check local filesystem
    if [ -f "/tmp/nexus-openai-agents-demo/dirs/reports/async-patterns.md" ]; then
        echo ""
        echo "Found report in local filesystem:"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        cat "/tmp/nexus-openai-agents-demo/dirs/reports/async-patterns.md"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    fi
fi

# ============================================
# Cleanup Instructions
# ============================================

echo ""
echo "╔═══════════════════════════════════════════════════════════╗"
echo "║   Demo Complete!                                          ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""
echo "What happened:"
echo "  1. ✓ Started Nexus server at $NEXUS_URL"
echo "  2. ✓ Created 3 sample Python files with async patterns"
echo "  3. ✓ OpenAI Agent analyzed files using ReAct loop"
echo "  4. ✓ Generated summary report with findings"
echo ""

if [ -f "/tmp/nexus-demo-server.pid" ]; then
    echo "To stop the server:"
    echo "  kill \$(cat /tmp/nexus-demo-server.pid)"
    echo ""
fi

echo "To explore the files:"
echo "  nexus ls /workspace"
echo "  nexus cat /workspace/async_api.py"
echo ""

echo "To run the agent again:"
echo "  export NEXUS_URL='$NEXUS_URL'"
if [ -n "$NEXUS_API_KEY" ]; then
    echo "  export NEXUS_API_KEY='$NEXUS_API_KEY'"
fi
echo "  python3 $SCRIPT_DIR/openai_agent_react_demo.py"
echo ""

# Cleanup temp files
rm -f /tmp/async_api.py /tmp/async_worker.py /tmp/regular_utils.py

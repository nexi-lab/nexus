#!/bin/bash
# Run LangGraph Multi-Agent Nexus Demo
#
# This script handles server initialization and runs the demo.
# It will start the Nexus server if needed and run the multi-agent workflow.

set -e

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

print_section() {
    echo ""
    echo -e "${BLUE}═══════════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}═══════════════════════════════════════════════════════${NC}"
    echo ""
}

print_info() { echo -e "${BLUE}ℹ${NC} $1"; }
print_success() { echo -e "${GREEN}✓${NC} $1"; }
print_warning() { echo -e "${YELLOW}⚠${NC} $1"; }
print_error() { echo -e "${RED}✗${NC} $1"; }

# Get repo root (script is in examples/langgraph_integration/)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

print_section "LangGraph + Nexus Multi-Agent Demo"

# Check if OPENAI_API_KEY is set
if [ -z "$OPENAI_API_KEY" ]; then
    print_error "OPENAI_API_KEY not set!"
    echo ""
    echo "Please set your OpenAI API key:"
    echo "  export OPENAI_API_KEY=\"your-key-here\""
    echo ""
    exit 1
fi
print_success "OPENAI_API_KEY is set"

# Try to load credentials if .nexus-admin-env exists
if [ -f "$REPO_ROOT/.nexus-admin-env" ]; then
    source "$REPO_ROOT/.nexus-admin-env"
    print_info "Loaded credentials from .nexus-admin-env"
fi

# Set default URL if not set
: ${NEXUS_URL:=http://localhost:8080}

# Check if Nexus server is running
print_info "Checking if Nexus server is running at $NEXUS_URL..."
SERVER_RUNNING=false
if curl -s "$NEXUS_URL/health" > /dev/null 2>&1; then
    print_success "Nexus server is already running"
    SERVER_RUNNING=true
fi

# If server not running, offer to start it
if [ "$SERVER_RUNNING" = false ]; then
    print_warning "Nexus server is not running"
    echo ""
    echo "This demo requires an auth-enabled Nexus server."
    echo ""

    # Check if we have credentials (server was initialized before)
    if [ -f "$REPO_ROOT/.nexus-admin-env" ]; then
        echo "Found existing credentials. Starting server in restart mode..."
        echo ""
        print_info "Starting Nexus server (press Ctrl+C to stop when done)..."
        echo ""

        # Start server in background (quiet mode, fully detached)
        cd "$REPO_ROOT"
        QUIET=1 nohup "$REPO_ROOT/scripts/init-nexus-with-auth.sh" > /dev/null 2>&1 &
        SERVER_PID=$!

        # Give server time to start
        echo ""
        print_info "Waiting for server to start..."
        sleep 3

        # Check if server is up
        MAX_RETRIES=10
        RETRY_COUNT=0
        while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
            if curl -s "$NEXUS_URL/health" > /dev/null 2>&1; then
                print_success "Server started successfully!"
                SERVER_RUNNING=true
                break
            fi
            echo -n "."
            sleep 1
            RETRY_COUNT=$((RETRY_COUNT + 1))
        done
        echo ""

        if [ "$SERVER_RUNNING" = false ]; then
            print_error "Server failed to start after $MAX_RETRIES seconds"
            kill $SERVER_PID 2>/dev/null || true
            exit 1
        fi

        # Load credentials
        source "$REPO_ROOT/.nexus-admin-env"

    else
        echo "No existing credentials found. You need to initialize first:"
        echo ""
        echo "Run this command in another terminal:"
        echo "  cd $REPO_ROOT"
        echo "  ./scripts/init-nexus-with-auth.sh --init"
        echo ""
        echo "Then run this demo script again."
        echo ""
        exit 1
    fi
fi

# Verify credentials are set
if [ -z "$NEXUS_API_KEY" ]; then
    print_error "NEXUS_API_KEY not set!"
    echo ""
    echo "Please ensure credentials are loaded:"
    echo "  source .nexus-admin-env"
    echo ""
    exit 1
fi

print_success "NEXUS_URL: $NEXUS_URL"
print_success "NEXUS_API_KEY is set"

# Verify server connectivity again
print_info "Verifying server connectivity..."
if ! curl -s "$NEXUS_URL/health" > /dev/null 2>&1; then
    print_error "Cannot connect to Nexus server at $NEXUS_URL"
    exit 1
fi
print_success "Connected to Nexus server"

# Change to example directory for running the demo
cd "$SCRIPT_DIR"

# Run the demo
print_section "Running Multi-Agent Workflow with Nexus"
echo ""
python3 multi_agent_nexus.py

echo ""
print_section "Demo Complete!"
print_info "Check the Nexus server for generated files:"
echo "  nexus ls /workspace/research/"
echo "  nexus ls /workspace/code/"
echo "  nexus ls /workspace/reviews/"
echo ""

# If we started the server, remind user to stop it
if [ -n "$SERVER_PID" ]; then
    print_warning "Server is still running in background (PID: $SERVER_PID)"
    echo ""
    echo "To stop the server:"
    echo "  kill $SERVER_PID"
    echo ""
fi

#!/bin/bash
# local-nexus.sh - Run Nexus server locally (outside Docker)
#
# This script allows running the Nexus server locally for faster development
# iteration while keeping other services (postgres, langgraph, frontend) in Docker.
#
# Usage:
#   ./local-nexus.sh --start    # Start the local server
#   ./local-nexus.sh --stop     # Stop the local server

set -e

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to get data directory path
get_data_path() {
    # Use local directory that's shared with Docker
    DATA_DIR="./nexus-data"

    # Create directory if it doesn't exist
    if [ ! -d "$DATA_DIR" ]; then
        echo -e "${GREEN}Creating data directory: $DATA_DIR${NC}" >&2
        mkdir -p "$DATA_DIR"
    fi

    echo "$DATA_DIR"
}

# Function to start the server
start_server() {
    echo ""
    echo "╔═══════════════════════════════════════════╗"
    echo "║     Starting Nexus Server Locally        ║"
    echo "╚═══════════════════════════════════════════╝"
    echo ""

    # Check Docker postgres is running
    if ! docker ps | grep -q nexus-postgres; then
        echo -e "${RED}ERROR: PostgreSQL container not running${NC}"
        echo ""
        echo "Please start Docker services first:"
        echo "  ./docker-start.sh"
        echo ""
        exit 1
    fi

    # Check if 'postgres' hostname resolves to localhost for connectors
    if ! grep -q "127.0.0.1.*postgres" /etc/hosts 2>/dev/null; then
        echo ""
        echo -e "${YELLOW}⚠  For connectors to work, add this to /etc/hosts:${NC}"
        echo ""
        echo "    127.0.0.1    postgres"
        echo ""
        echo "  Run: sudo bash -c 'echo \"127.0.0.1    postgres\" >> /etc/hosts'"
        echo ""
        echo "  (This allows 'postgres:5432' in connector configs to resolve to localhost)"
        echo ""
        read -p "Continue without it? (connectors will fail but core server works) [Y/n] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]] && [[ ! -z $REPLY ]]; then
            exit 1
        fi
    fi

    # Check if PostgreSQL is healthy
    if ! docker exec nexus-postgres pg_isready -U postgres > /dev/null 2>&1; then
        echo -e "${YELLOW}WARNING: PostgreSQL is not ready yet${NC}"
        echo "Waiting for PostgreSQL..."
        sleep 3
    fi

    # Check if port 8080 is already in use
    if lsof -ti :8080 >/dev/null 2>&1; then
        echo -e "${YELLOW}WARNING: Port 8080 is already in use${NC}"
        echo ""
        echo "This is likely the Docker nexus-server. Stop it first:"
        echo "  docker stop nexus-server"
        echo ""
        exit 1
    fi

    # Load environment variables from .env.local
    if [ -f .env.local ]; then
        set -a
        source .env.local
        set +a
        echo -e "${GREEN}✓${NC} Loaded environment from .env.local"
    else
        echo -e "${YELLOW}⚠${NC}  No .env.local file found (using defaults)"
    fi

    # Get data directory path (shared with Docker)
    DATA_PATH=$(get_data_path)

    # Override for local development
    # NEXUS_DB_PATH overrides the db_path setting in config.demo.yaml
    export NEXUS_DB_PATH="postgresql://postgres:nexus@localhost:5432/nexus"
    export NEXUS_DATA_DIR="$DATA_PATH"

    # Also set the database URL for components that use it directly
    export NEXUS_DATABASE_URL="postgresql://postgres:nexus@localhost:5432/nexus"

    # Override token_manager_db for connectors (GDrive, Gmail, etc.)
    # This ensures connectors use localhost instead of the Docker "postgres" hostname
    export TOKEN_MANAGER_DB="postgresql://postgres:nexus@localhost:5432/nexus"

    # Activate virtual environment
    if [ ! -d .venv ]; then
        echo -e "${RED}ERROR: Virtual environment not found${NC}"
        echo ""
        echo "Create it with:"
        echo "  python -m venv .venv"
        echo "  source .venv/bin/activate"
        echo "  pip install -e ."
        echo ""
        exit 1
    fi

    source .venv/bin/activate

    # Optional: Rebuild Rust extension for better performance
    # Only needed if you've modified the Rust code
    # Uncomment below to rebuild on each start:
    # if command -v maturin &> /dev/null || command -v ~/.local/bin/maturin &> /dev/null; then
    #     echo "Building Rust extension..."
    #     MATURIN_CMD=$(command -v maturin || echo ~/.local/bin/maturin)
    #     [ -d "rust/nexus_fast" ] && (cd rust/nexus_fast && $MATURIN_CMD develop --release --quiet && cd ../..)
    # fi

    # Display configuration
    echo ""
    echo -e "${BLUE}Configuration:${NC}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Config File:  ./configs/config.demo.yaml"
    echo "  Database:     $NEXUS_DB_PATH"
    echo "  Data Dir:     $DATA_PATH"
    echo "  Host:         ${NEXUS_HOST:-0.0.0.0}"
    echo "  Port:         ${NEXUS_PORT:-8080}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo -e "${GREEN}Starting server...${NC}"
    echo ""
    echo "Press Ctrl+C to stop"
    echo ""

    # Start the Nexus server
    nexus serve \
        --config ./configs/config.demo.yaml \
        --auth-type database \
        --async
}

# Function to stop the server
stop_server() {
    echo ""
    echo "Stopping local Nexus server..."

    # Get all PIDs using port 8080
    PIDS=$(lsof -ti :8080 2>/dev/null || true)

    if [ -n "$PIDS" ]; then
        echo "Found process(es) on port 8080"

        # Kill each PID
        for PID in $PIDS; do
            echo "Stopping PID: $PID"

            # Send SIGTERM for graceful shutdown
            if kill -0 $PID 2>/dev/null; then
                kill -TERM $PID 2>/dev/null || true
            fi
        done

        sleep 2

        # Force kill any remaining processes
        for PID in $PIDS; do
            if kill -0 $PID 2>/dev/null; then
                echo "Force killing PID: $PID"
                kill -9 $PID 2>/dev/null || true
            fi
        done

        echo -e "${GREEN}✓ Server stopped${NC}"
        echo ""
    else
        echo "No server running on port 8080"
        echo ""
    fi
}

# Main script logic
case "$1" in
    --start)
        start_server
        ;;
    --stop)
        stop_server
        ;;
    *)
        echo ""
        echo "Usage: $0 {--start|--stop}"
        echo ""
        echo "Commands:"
        echo "  --start    Start Nexus server locally (outside Docker)"
        echo "  --stop     Stop local Nexus server"
        echo ""
        echo "Workflow:"
        echo "  1. Start Docker services:       ./docker-start.sh"
        echo "  2. Stop Docker nexus-server:    docker stop nexus-server"
        echo "  3. Start local nexus:           ./local-nexus.sh --start"
        echo "  4. Make changes and restart:    Ctrl+C then ./local-nexus.sh --start"
        echo "  5. When done:                   docker start nexus-server"
        echo ""
        echo "Optional: Enable connectors (GDrive, Gmail) in local mode:"
        echo "  sudo bash -c 'echo \"127.0.0.1    postgres\" >> /etc/hosts'"
        echo "  This maps 'postgres' hostname to localhost for connector database access."
        echo ""
        exit 1
        ;;
esac

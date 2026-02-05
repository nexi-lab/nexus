#!/bin/bash
# local-demo.sh - Run Nexus server locally (outside Docker)
#
# This script allows running the Nexus server locally for faster development
# iteration while keeping other services (postgres, langgraph, frontend) in Docker.
#
# Usage:
#   ./scripts/local-demo.sh --start    # Start the local server
#   ./scripts/local-demo.sh --stop     # Stop the local server

set -e

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Load configuration from .env file (same as docker-demo.sh)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${PROJECT_ROOT}/.env"

if [ -f "$ENV_FILE" ]; then
    # Save any already-set variables before loading .env
    _SAVED_NEXUS_PORT="${NEXUS_PORT}"
    _SAVED_POSTGRES_PORT="${POSTGRES_PORT}"
    _SAVED_POSTGRES_CONTAINER="${POSTGRES_CONTAINER}"
    _SAVED_POSTGRES_DATA_DIR="${POSTGRES_DATA_DIR}"

    set -a  # Auto-export all variables
    source "$ENV_FILE"
    set +a
    echo -e "${GREEN}✓ Loaded configuration from ${ENV_FILE}${NC}"

    # Restore overridden variables (command-line takes precedence)
    [ -n "$_SAVED_NEXUS_PORT" ] && export NEXUS_PORT="$_SAVED_NEXUS_PORT"
    [ -n "$_SAVED_POSTGRES_PORT" ] && export POSTGRES_PORT="$_SAVED_POSTGRES_PORT"
    [ -n "$_SAVED_POSTGRES_CONTAINER" ] && export POSTGRES_CONTAINER="$_SAVED_POSTGRES_CONTAINER"
    [ -n "$_SAVED_POSTGRES_DATA_DIR" ] && export POSTGRES_DATA_DIR="$_SAVED_POSTGRES_DATA_DIR"

    # Construct database URL from primitives
    # Note: If using POSTGRES_HOST=postgres, ensure /etc/hosts has: 127.0.0.1 postgres
    POSTGRES_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
    TOKEN_MANAGER_DB="$POSTGRES_URL"
    ADMIN_API_KEY="${NEXUS_API_KEY}"

    # Set defaults for variables that might not be in .env
    NEXUS_DATA_DIR="${NEXUS_DATA_DIR:-./nexus-data-local}"
    POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-nexus-postgres}"
    elif [ -f "${PROJECT_ROOT}/.env.example" ]; then
    echo -e "${YELLOW}⚠️  No .env file found, using .env.example${NC}"
    echo "   💡 Tip: Create .env for your personal config"
    echo "   Run: cp .env.example .env"
    echo ""
    set -a
    source "${PROJECT_ROOT}/.env.example"
    set +a

    # Construct database URL from primitives
    # Note: If using POSTGRES_HOST=postgres, ensure /etc/hosts has: 127.0.0.1 postgres
    POSTGRES_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
    TOKEN_MANAGER_DB="$POSTGRES_URL"
    ADMIN_API_KEY="${NEXUS_API_KEY}"

    # Set defaults
    NEXUS_DATA_DIR="${NEXUS_DATA_DIR:-./nexus-data-local}"
    POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-nexus-postgres}"
else
    echo -e "${RED}✗ Configuration file not found: ${ENV_FILE}${NC}"
    echo "Using fallback default values..."
    # Fallback defaults
    NEXUS_DATA_DIR="./nexus-data-local"
    POSTGRES_USER="postgres"
    POSTGRES_PASSWORD="nexus"
    POSTGRES_DB="nexus"
    POSTGRES_PORT="5432"
    POSTGRES_HOST="localhost"
    POSTGRES_CONTAINER="nexus-postgres"
    POSTGRES_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
    TOKEN_MANAGER_DB="$POSTGRES_URL"
    ADMIN_API_KEY="sk-default_admin_dddddddd_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
fi

# Set sane defaults (explicit paths, no legacy overrides)
DEFAULT_DATA_DIR="${NEXUS_DATA_DIR:-${PROJECT_ROOT}/nexus-data-local}"
DEFAULT_POSTGRES_URL="${POSTGRES_URL:-postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}}"

# Function to get data directory path
get_data_path() {
    local data_dir="${1:-$DEFAULT_DATA_DIR}"

    # Create directory if it doesn't exist
    if [ ! -d "$data_dir" ]; then
        echo -e "${GREEN}Creating data directory: $data_dir${NC}" >&2
        mkdir -p "$data_dir"
    fi

    # Normalize to absolute path to avoid cwd-related sqlite issues
    if command -v realpath >/dev/null 2>&1; then
        data_dir="$(realpath "$data_dir")"
    else
        # POSIX fallback
        data_dir="$(cd "$data_dir" && pwd)"
    fi

    echo "$data_dir"
}

clean_sqlite_artifacts() {
    local db_path="$1"
    local wal="${db_path}-wal"
    local shm="${db_path}-shm"

    if [ -f "$wal" ]; then
        rm -f "$wal"
    fi
    if [ -f "$shm" ]; then
        rm -f "$shm"
    fi

    if [ -f "$db_path" ]; then
        chmod u+rw "$db_path" || true
    fi
}

# Function to parse command-line arguments
parse_args() {
    POSTGRES_URL="$DEFAULT_POSTGRES_URL"
    DATA_DIR="$DEFAULT_DATA_DIR"
    SQLITE=false         # default to PostgreSQL
    START_UI=true            # default to start UI
    START_LANGGRAPH=true     # default to start LangGraph
    DISABLE_PERMISSIONS=false
    DISABLE_ZONE_ISOLATION=false
    NO_AUTH=false

    while [[ $# -gt 0 ]]; do
        case $1 in
            --postgres-url)
                POSTGRES_URL="$2"
                SQLITE=false  # switch to Postgres if URL provided
                shift 2
                ;;
            --data-dir)
                DATA_DIR="$2"
                shift 2
                ;;
            --sqlite)
                SQLITE=true
                shift
                ;;
            --nosqlite)
                SQLITE=false
                shift
                ;;
            --ui)
                START_UI=true
                shift
                ;;
            --no-ui|--noui)
                START_UI=false
                shift
                ;;
            --langgraph)
                START_LANGGRAPH=true
                shift
                ;;
            --no-langgraph|--nolanggraph)
                START_LANGGRAPH=false
                shift
                ;;
            --no-permissions)
                DISABLE_PERMISSIONS=true
                shift
                ;;
            --no-zone-isolation)
                DISABLE_ZONE_ISOLATION=true
                shift
                ;;
            --no-auth)
                NO_AUTH=true
                shift
                ;;
            *)
                # Unknown option, pass through
                shift
                ;;
        esac
    done
}

# Ensure core Python virtual environment exists and nexus is installed
ensure_core_python_env() {
    local venv_path="${PROJECT_ROOT}/.venv"
    local python_bin="${PYTHON:-python3}"

    if [ ! -d "$venv_path" ]; then
        echo -e "${YELLOW}Creating Python virtual environment at ${venv_path}${NC}"
        $python_bin -m venv "$venv_path"
    fi

    source "$venv_path/bin/activate"

    # Install nexus in editable mode if missing
    if ! python -c "import nexus" >/dev/null 2>&1; then
        echo -e "${YELLOW}Installing nexus in editable mode (first-time setup)...${NC}"
        pip install --upgrade pip
        pip install -e "${PROJECT_ROOT}"
    fi
}

# Ensure frontend deps are installed once before running dev server
ensure_frontend_ready() {
    # Clone to same level as nexus directory (not inside it)
    local NEXUS_PARENT_DIR="$(cd "${PROJECT_ROOT}/.." && pwd)"
    local FRONTEND_DIR="${NEXUS_PARENT_DIR}/nexus-frontend"
    local FRONTEND_URL="${FRONTEND_REPO_URL:-https://github.com/nexi-lab/nexus-frontend.git}"

    if [ ! -d "$FRONTEND_DIR" ]; then
        echo -e "${YELLOW}Frontend directory not found: $FRONTEND_DIR${NC}"
        if ! command -v git >/dev/null 2>&1; then
            echo -e "${RED}ERROR: git is required to clone the frontend.${NC}"
            echo "Install git or clone manually:"
            echo "  git clone $FRONTEND_URL \"$FRONTEND_DIR\""
            return 1
        fi
        echo -e "${YELLOW}Cloning frontend from ${FRONTEND_URL}...${NC}"
        mkdir -p "$(dirname "$FRONTEND_DIR")"
        if ! git clone "$FRONTEND_URL" "$FRONTEND_DIR"; then
            echo -e "${RED}ERROR: Failed to clone frontend repo.${NC}"
            echo "Try manually:"
            echo "  git clone $FRONTEND_URL \"$FRONTEND_DIR\""
            return 1
        fi
        echo -e "${GREEN}✓ Frontend cloned to ${FRONTEND_DIR}${NC}"
    fi

    if ! command -v pnpm >/dev/null 2>&1; then
        echo -e "${RED}ERROR: pnpm is required. Install with 'corepack enable && npm install -g pnpm'${NC}"
        return 1
    fi

    if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
        echo -e "${YELLOW}Installing frontend dependencies (pnpm install)...${NC}"
        (cd "$FRONTEND_DIR" && pnpm install)
    fi
}

# Ensure LangGraph repo is cloned and dependencies are installed
ensure_langgraph_env() {
    # Clone to same level as nexus directory (not inside it)
    local NEXUS_PARENT_DIR="$(cd "${PROJECT_ROOT}/.." && pwd)"
    local LANGGRAPH_DIR="${NEXUS_PARENT_DIR}/nexus-langgraph"
    local LANGGRAPH_URL="${LANGGRAPH_REPO_URL:-https://github.com/nexi-lab/nexus-langgraph.git}"
    local python_bin="${PYTHON:-python3}"

    # Clone repo if it doesn't exist
    if [ ! -d "$LANGGRAPH_DIR" ]; then
        echo -e "${YELLOW}LangGraph directory not found: $LANGGRAPH_DIR${NC}"
        if ! command -v git >/dev/null 2>&1; then
            echo -e "${RED}ERROR: git is required to clone nexus-langgraph.${NC}"
            echo "Install git or clone manually:"
            echo "  git clone $LANGGRAPH_URL \"$LANGGRAPH_DIR\""
            return 1
        fi
        echo -e "${YELLOW}Cloning nexus-langgraph from ${LANGGRAPH_URL}...${NC}"
        mkdir -p "$(dirname "$LANGGRAPH_DIR")"
        if ! git clone "$LANGGRAPH_URL" "$LANGGRAPH_DIR"; then
            echo -e "${RED}ERROR: Failed to clone nexus-langgraph repo.${NC}"
            echo "Try manually:"
            echo "  git clone $LANGGRAPH_URL \"$LANGGRAPH_DIR\""
            return 1
        fi
        echo -e "${GREEN}✓ nexus-langgraph cloned to ${LANGGRAPH_DIR}${NC}"
    else
        # Pull latest if repo exists
        echo -e "${YELLOW}Pulling latest changes for nexus-langgraph...${NC}"
        (cd "$LANGGRAPH_DIR" && git pull origin main 2>/dev/null || echo "  (Already up to date or no git repo)")
    fi

    # Create virtual environment if needed
    if [ ! -d "$LANGGRAPH_DIR/.venv" ]; then
        echo -e "${YELLOW}Creating LangGraph virtual environment at ${LANGGRAPH_DIR}/.venv${NC}"
        (cd "$LANGGRAPH_DIR" && $python_bin -m venv .venv)
    fi

    # Install dependencies
    (
        cd "$LANGGRAPH_DIR"
        source .venv/bin/activate
        pip install --upgrade pip >/dev/null 2>&1 || true

        # Install langgraph deps (this will handle nexus-fs-python or nexus-ai-fs dependencies)
        if [ -f "pyproject.toml" ]; then
            echo -e "${YELLOW}Installing LangGraph dependencies...${NC}"
            pip install -e .
        elif [ -f "requirements.txt" ]; then
            pip install -r requirements.txt
        fi
    )
}

# Ensure Docker is available and running for langgraph workflows
ensure_docker_for_langgraph() {
    # Check binary
    if ! command -v docker >/dev/null 2>&1; then
        echo -e "${YELLOW}Docker is required for langgraph demos.${NC}"
        echo "Install Docker Desktop: https://www.docker.com/products/docker-desktop"
        read -p "Continue without Docker? [y/N] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
        return 0
    fi

    # Check daemon
    if ! docker info >/dev/null 2>&1; then
        echo -e "${YELLOW}Docker is not running. Attempting to start...${NC}"
        if command -v open >/dev/null 2>&1; then
            open --background -a Docker || true
        elif command -v systemctl >/dev/null 2>&1; then
            sudo systemctl start docker || true
        fi

        # Wait for Docker to become ready
        for i in {1..30}; do
            if docker info >/dev/null 2>&1; then
                echo -e "${GREEN}✓ Docker is running${NC}"
                return 0
            fi
            sleep 1
        done

        echo -e "${YELLOW}Docker is still not responding.${NC}"
        read -p "Continue without Docker? [y/N] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi
}

ensure_docker_sandbox_image() {
    local image="${NEXUS_SANDBOX_DOCKER_IMAGE:-nexus-sandbox:latest}"

    # If Docker isn't installed, just warn; sandbox calls will fail later with a clear error
    if ! command -v docker >/dev/null 2>&1; then
        echo -e "${YELLOW}Docker not installed. Sandbox image check skipped.${NC}"
        echo "  Install Docker Desktop: https://www.docker.com/products/docker-desktop"
        return 0
    fi

    # Ensure daemon is up
    if ! docker info >/dev/null 2>&1; then
        echo -e "${YELLOW}Docker is not running. Attempting to start...${NC}"
        if command -v open >/dev/null 2>&1; then
            open --background -a Docker || true
        elif command -v systemctl >/dev/null 2>&1; then
            sudo systemctl start docker || true
        fi
        for i in {1..30}; do
            if docker info >/dev/null 2>&1; then
                echo -e "${GREEN}✓ Docker is running${NC}"
                break
            fi
            sleep 1
        done
    fi

    # Fast check for image in current Docker context
    if docker image inspect "${image}" >/dev/null 2>&1; then
        echo -e "${GREEN}✓ Docker image found: ${image}${NC}"
        return 0
    fi

    echo -e "${YELLOW}Docker image '${image}' not found.${NC}"
    # Prefer dockerfiles/build.sh if present to build runtime image (matches docker-demo.sh)
    if [ -x "${PROJECT_ROOT}/dockerfiles/build.sh" ]; then
        echo -e "${YELLOW}Building sandbox runtime via dockerfiles/build.sh ...${NC}"
        if "${PROJECT_ROOT}/dockerfiles/build.sh"; then
            echo -e "${GREEN}✓ Built sandbox runtime image via dockerfiles/build.sh${NC}"
            docker image inspect "${image}" >/dev/null 2>&1 && return 0
            echo -e "${YELLOW}Build succeeded but image '${image}' not visible in this Docker context.${NC}"
        else
            echo -e "${YELLOW}dockerfiles/build.sh failed, falling back to direct docker build...${NC}"
        fi
    fi

    echo -e "${YELLOW}Building '${image}' from ${PROJECT_ROOT}/Dockerfile...${NC}"
    if docker build -t "${image}" -f "${PROJECT_ROOT}/Dockerfile" "${PROJECT_ROOT}"; then
        echo -e "${GREEN}✓ Built Docker image '${image}'${NC}"
    else
        echo -e "${RED}✗ Failed to build Docker image '${image}'${NC}"
        echo "  You can build manually with:"
        echo "    docker build -t ${image} -f ${PROJECT_ROOT}/Dockerfile ${PROJECT_ROOT}"
        return 1
    fi

    # Final verify
    if docker image inspect "${image}" >/dev/null 2>&1; then
        echo -e "${GREEN}✓ Docker image ready: ${image}${NC}"
    else
        echo -e "${RED}✗ Image '${image}' still not found after build. Check Docker context (docker context show) and DOCKER_HOST.${NC}"
        return 1
    fi
}

# Function to check if port 2026 is available
check_port_2026_available() {
    # Only check for LISTEN state, not stale connections
    local PORT=${NEXUS_PORT:-2026}
    if lsof -ti :${PORT} -sTCP:LISTEN >/dev/null 2>&1; then
        echo -e "${YELLOW}ERROR: Port ${PORT} is already in use${NC}"
        echo ""
        PIDS=$(lsof -ti :${PORT} -sTCP:LISTEN 2>/dev/null || true)
        if [ -n "$PIDS" ]; then
            echo "Process(es) running on port ${PORT}:"
            lsof -i :${PORT} -sTCP:LISTEN 2>/dev/null | grep -v "^COMMAND" | while read line; do
                echo "  $line"
            done
            echo ""
            echo "Stop the server first using one of these commands:"
            echo "   ./scripts/local-demo.sh --stop              # Stop local Nexus server"
            echo "   ./scripts/docker-demo.sh --stop             # Stop Docker-based server"
            echo ""
            echo "Or manually kill the process(es):"
            for PID in $PIDS; do
                echo "   kill $PID          # Graceful shutdown"
                echo "   kill -9 $PID       # Force kill (if needed)"
            done
            echo ""
        fi
        return 1
    fi
    return 0
}

# Function to check Docker is accessible (simple version)
check_docker_ready() {
    # Quick check - if Docker works, return immediately
    if docker info >/dev/null 2>&1; then
        return 0
    fi

    # Docker not ready - wait a bit for Docker Desktop to start
    echo -e "${YELLOW}Docker daemon not ready, waiting...${NC}"

    # Try to start Docker Desktop on macOS if not running
    if [[ "$OSTYPE" == "darwin"* ]]; then
        if ! pgrep -f "Docker.app" >/dev/null 2>&1; then
            echo "Starting Docker Desktop..."
            open --background -a Docker 2>/dev/null || true
        fi
    fi

    # Wait up to 30 seconds for Docker to become ready
    for i in {1..30}; do
        if docker info >/dev/null 2>&1; then
            echo -e "${GREEN}✓ Docker is ready${NC}"
            return 0
        fi
        sleep 1
    done

    # Still not ready
    echo -e "${RED}ERROR: Cannot connect to Docker daemon${NC}"
    echo ""
    echo "Please ensure Docker is running and try again."
    return 1
}

# Function to ensure PostgreSQL is running
ensure_postgres_running() {
    # Check Docker is ready before proceeding
    if ! check_docker_ready; then
        exit 1
    fi

    # Use configuration from config file
    CONTAINER_NAME="${POSTGRES_CONTAINER}"
    DB_NAME="${POSTGRES_DB}"
    DB_USER="${POSTGRES_USER}"
    DB_PASSWORD="${POSTGRES_PASSWORD}"
    # Extract port from POSTGRES_URL if it contains @host:port pattern, otherwise use POSTGRES_PORT
    DB_PORT=$(python3 -c "import re, sys; m=re.search(r'@[^:]+:(\d+)', \"${POSTGRES_URL}\"); sys.stdout.write(m.group(1) if m else '${POSTGRES_PORT}')" 2>/dev/null || echo "${POSTGRES_PORT}")
    POSTGRES_DATA_DIR="${POSTGRES_DATA_DIR:-/tmp/nexus-postgres}"

    if ! docker ps | grep -q "nexus.*postgres"; then
        echo -e "${YELLOW}PostgreSQL container not running, starting...${NC}"

        # Check if port is already in use
        if lsof -i :${DB_PORT} > /dev/null 2>&1 || netstat -an 2>/dev/null | grep -q ":${DB_PORT}.*LISTEN"; then
            echo -e "${YELLOW}Port ${DB_PORT} is already in use.${NC}"

            # Check if it's another Docker container using the port
            CONFLICTING_CONTAINER=$(docker ps --format '{{.Names}}' --filter "publish=${DB_PORT}" | head -1)
            if [ -n "$CONFLICTING_CONTAINER" ]; then
                echo -e "${YELLOW}Port ${DB_PORT} is used by container: ${CONFLICTING_CONTAINER}${NC}"
                if [ "$CONFLICTING_CONTAINER" = "${CONTAINER_NAME}" ]; then
                    echo "Container exists but may be in a bad state. Attempting to remove and recreate..."
                    docker rm -f ${CONTAINER_NAME} 2>/dev/null || true
                else
                    echo -e "${RED}ERROR: Port ${DB_PORT} is already allocated by container '${CONFLICTING_CONTAINER}'${NC}"
                    echo ""
                    echo "Please either:"
                    echo "  1. Stop the conflicting container: docker stop ${CONFLICTING_CONTAINER}"
                    echo "  2. Use a different port: POSTGRES_PORT=5433 $0 --start"
                    return 1
                fi
            else
                echo -e "${RED}ERROR: Port ${DB_PORT} is already allocated by a non-Docker process${NC}"
                echo ""
                echo "Please either:"
                echo "  1. Stop the process using port ${DB_PORT}"
                echo "  2. Use a different port: POSTGRES_PORT=5433 $0 --start"
                return 1
            fi
        fi

        # Check if container exists but is stopped
        if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
            echo "Starting existing container: ${CONTAINER_NAME}"
            if ! docker start ${CONTAINER_NAME} 2>/dev/null; then
                echo -e "${YELLOW}Failed to start existing container. Removing and recreating...${NC}"
                docker rm -f ${CONTAINER_NAME} 2>/dev/null || true
                # Fall through to create new container
            else
                # Successfully started, wait for it to be ready
                sleep 2
                if docker exec ${CONTAINER_NAME} pg_isready -U ${DB_USER} > /dev/null 2>&1; then
                    echo -e "${GREEN}✓ PostgreSQL container started successfully${NC}"
                    return 0
                fi
            fi
        else
            # Create new container
            echo "Creating new PostgreSQL container: ${CONTAINER_NAME}"

            # Create data directory if it doesn't exist
            mkdir -p ${POSTGRES_DATA_DIR}

            # Start PostgreSQL 18 container with performance optimizations
            docker run -d \
                --name ${CONTAINER_NAME} \
                -e POSTGRES_DB=${DB_NAME} \
                -e POSTGRES_USER=${DB_USER} \
                -e POSTGRES_PASSWORD=${DB_PASSWORD} \
                -p ${DB_PORT}:5432 \
                -v ${POSTGRES_DATA_DIR}:/var/lib/postgresql/data \
                postgres:18-alpine \
                postgres \
                -c io_method=worker \
                -c effective_io_concurrency=16 \
                -c maintenance_io_concurrency=16

            # Wait a moment for container to initialize
            sleep 2
        fi

        # Wait for PostgreSQL to be ready
        echo "Waiting for PostgreSQL to be ready..."
        for i in {1..30}; do
            if docker exec ${CONTAINER_NAME} pg_isready -U ${DB_USER} > /dev/null 2>&1; then
                echo -e "${GREEN}✓ PostgreSQL is ready!${NC}"

                # Ensure the database exists (create if it doesn't)
                if ! docker exec ${CONTAINER_NAME} psql -U ${DB_USER} -lqt | cut -d \| -f 1 | grep -qw ${DB_NAME}; then
                    echo "Creating database: ${DB_NAME}"
                    docker exec ${CONTAINER_NAME} psql -U ${DB_USER} -c "CREATE DATABASE ${DB_NAME};" > /dev/null 2>&1
                fi
                break
            fi
            if [ $i -eq 30 ]; then
                echo -e "${RED}ERROR: PostgreSQL failed to start after 30 seconds${NC}"
                exit 1
            fi
            sleep 1
        done
    fi
}

# Function to start the frontend
start_frontend() {
    # Use same level as nexus directory (not inside it)
    local NEXUS_PARENT_DIR="$(cd "${PROJECT_ROOT}/.." && pwd)"
    local FRONTEND_DIR="${NEXUS_PARENT_DIR}/nexus-frontend"

    ensure_frontend_ready || return 1

    echo -e "${GREEN}Starting frontend (pnpm run dev)...${NC}"

    # Start frontend in background with VITE_NEXUS_SERVER_URL set to localhost
    cd "$FRONTEND_DIR"
    VITE_NEXUS_SERVER_URL=http://localhost:2026 pnpm run dev > /tmp/nexus-frontend.log 2>&1 &
    local FRONTEND_PID=$!
    echo $FRONTEND_PID > /tmp/nexus-frontend.pid

    echo -e "${GREEN}✓ Frontend started (PID: $FRONTEND_PID)${NC}"
    echo "  Logs: /tmp/nexus-frontend.log"
    echo "  URL: http://localhost:5173"
    echo "  Nexus Backend: http://localhost:2026"

    cd - > /dev/null
}

# Function to start langgraph
start_langgraph() {
    # Use same level as nexus directory (not inside it)
    local NEXUS_PARENT_DIR="$(cd "${PROJECT_ROOT}/.." && pwd)"
    local LANGGRAPH_DIR="${NEXUS_PARENT_DIR}/nexus-langgraph"

    ensure_docker_for_langgraph
    ensure_langgraph_env || return 1

    echo -e "${GREEN}Starting langgraph (langgraph dev)...${NC}"

    # Start langgraph in background with virtual environment activated
    (
        cd "$LANGGRAPH_DIR"
        source .venv/bin/activate
        langgraph dev > /tmp/nexus-langgraph.log 2>&1
    ) &
    local LANGGRAPH_PID=$!
    echo $LANGGRAPH_PID > /tmp/nexus-langgraph.pid

    echo -e "${GREEN}✓ Langgraph started (PID: $LANGGRAPH_PID)${NC}"
    echo "  Logs: /tmp/nexus-langgraph.log"
    echo "  URL: http://localhost:2024 (default LangGraph port)"
}

# Function to stop the frontend
stop_frontend() {
    if [ -f /tmp/nexus-frontend.pid ]; then
        local PID=$(cat /tmp/nexus-frontend.pid)
        if kill -0 $PID 2>/dev/null; then
            echo "Stopping frontend (PID: $PID)..."
            kill -TERM $PID 2>/dev/null || true
            sleep 1
            if kill -0 $PID 2>/dev/null; then
                kill -9 $PID 2>/dev/null || true
            fi
            echo -e "${GREEN}✓ Frontend stopped${NC}"
        fi
        rm -f /tmp/nexus-frontend.pid
    fi
}

# Function to stop langgraph
stop_langgraph() {
    if [ -f /tmp/nexus-langgraph.pid ]; then
        local PID=$(cat /tmp/nexus-langgraph.pid)
        if kill -0 $PID 2>/dev/null; then
            echo "Stopping langgraph (PID: $PID)..."
            kill -TERM $PID 2>/dev/null || true
            sleep 1
            if kill -0 $PID 2>/dev/null; then
                kill -9 $PID 2>/dev/null || true
            fi
            echo -e "${GREEN}✓ Langgraph stopped${NC}"
        fi
        rm -f /tmp/nexus-langgraph.pid
    fi
}

# Function to start the server
start_server() {
    # Parse arguments
    parse_args "$@"

    echo ""
    echo "╔═══════════════════════════════════════════╗"
    echo "║     Starting Nexus Server Locally        ║"
    echo "╚═══════════════════════════════════════════╝"
    echo ""

    # Check if port 2026 is available FIRST before any other setup work
    if ! check_port_2026_available; then
        exit 1
    fi

    # If data directory is empty, auto-init (no prompt)
    DATA_PATH=$(get_data_path "$DATA_DIR")
    if [ -z "$(ls -A "$DATA_PATH" 2>/dev/null)" ]; then
        echo -e "${YELLOW}Data directory is empty: ${DATA_PATH}${NC}"
        echo -e "${YELLOW}Running init flow automatically...${NC}"
        AUTO_INIT=true init_database "$@"
        return
    fi

    # Handle database setup based on mode
    if [ "$SQLITE" = true ]; then
        echo -e "${GREEN}Using SQLite database (embedded mode)${NC}"
        # Set SQLite database URL
        DATA_PATH=$(get_data_path "$DATA_DIR")
        POSTGRES_URL="sqlite:///${DATA_PATH}/nexus.db"
        clean_sqlite_artifacts "${DATA_PATH}/nexus.db"
    else
        # Check and start PostgreSQL container if needed
        ensure_postgres_running

        # Check if 'postgres' hostname resolves to localhost
        # This is needed for both core functionality and connectors
        if ! grep -q "127.0.0.1.*postgres" /etc/hosts 2>/dev/null; then
            echo ""
            echo -e "${YELLOW}⚠  For local-demo.sh to work with POSTGRES_HOST=postgres, add this to /etc/hosts:${NC}"
            echo ""
            echo "    127.0.0.1    postgres"
            echo ""
            echo "  Run: sudo bash -c 'echo \"127.0.0.1    postgres\" >> /etc/hosts'"
            echo ""
            echo "  This allows the same .env config to work for both local-demo.sh and docker-demo.sh"
            echo ""

            # Auto-fallback to localhost if postgres hostname not mapped
            if [ "$POSTGRES_HOST" = "postgres" ]; then
                echo -e "${YELLOW}  Using localhost fallback for now...${NC}"
                POSTGRES_HOST="localhost"
                POSTGRES_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
                TOKEN_MANAGER_DB="$POSTGRES_URL"
                echo ""
            fi
        fi

        # Verify PostgreSQL is healthy
        if docker ps | grep -q "${POSTGRES_CONTAINER}"; then
            if ! docker exec "${POSTGRES_CONTAINER}" pg_isready -U "${POSTGRES_USER}" > /dev/null 2>&1; then
                echo -e "${YELLOW}WARNING: PostgreSQL health check failed${NC}"
            fi
        elif docker ps | grep -q nexus-postgres; then
            if ! docker exec nexus-postgres pg_isready -U postgres > /dev/null 2>&1; then
                echo -e "${YELLOW}WARNING: PostgreSQL health check failed${NC}"
            fi
        fi
    fi

    # Environment variables already loaded at script start from .env

    # Get data directory path (create if missing)
    DATA_PATH=$(get_data_path "$DATA_DIR")

    # Use provided postgres URL or default
    export NEXUS_DB_PATH="$POSTGRES_URL"
    export NEXUS_DATABASE_URL="$POSTGRES_URL"
    export TOKEN_MANAGER_DB="$POSTGRES_URL"
    export NEXUS_DATA_DIR="$DATA_PATH"

    # Configure permissions and zone isolation
    if [ "$DISABLE_PERMISSIONS" = true ]; then
        export NEXUS_ENFORCE_PERMISSIONS=false
    else
        export NEXUS_ENFORCE_PERMISSIONS=true
    fi

    if [ "$DISABLE_ZONE_ISOLATION" = true ]; then
        export NEXUS_ENFORCE_ZONE_ISOLATION=false
    else
        export NEXUS_ENFORCE_ZONE_ISOLATION=true
    fi

    # Ensure Python environment is ready (first-time install support)
    ensure_core_python_env

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
    if [ "$SQLITE" = true ]; then
        echo "  Database:     SQLite (${DATA_PATH}/nexus.db)"
    else
        echo "  Database:     $NEXUS_DB_PATH"
    fi
    echo "  Data Dir:     $DATA_PATH"
    echo "  Host:         ${NEXUS_HOST:-0.0.0.0}"
    echo "  Port:         ${NEXUS_PORT:-2026}"
    if [ "$START_UI" = true ]; then
        echo "  Frontend:     Enabled"
    fi
    if [ "$START_LANGGRAPH" = true ]; then
        echo "  Langgraph:    Enabled"
    fi
    echo ""
    if [ "$NO_AUTH" = true ]; then
        echo -e "  Auth:         ${YELLOW}Disabled${NC}"
    else
        echo -e "  Auth:         ${GREEN}Enabled (database)${NC}"
    fi
    if [ "$DISABLE_PERMISSIONS" = true ]; then
        echo -e "  Permissions:  ${YELLOW}Disabled${NC}"
    else
        echo -e "  Permissions:  ${GREEN}Enabled${NC}"
    fi
    if [ "$DISABLE_ZONE_ISOLATION" = true ]; then
        echo -e "  Zone Isol:    ${YELLOW}Disabled${NC}"
    else
        echo -e "  Zone Isol:    ${GREEN}Enabled${NC}"
    fi
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # Ensure docker sandbox image exists (best-effort)
    ensure_docker_sandbox_image || true

    # Start frontend if requested
    if [ "$START_UI" = true ]; then
        echo ""
        start_frontend
        echo ""
    fi

    # Start langgraph if requested
    if [ "$START_LANGGRAPH" = true ]; then
        echo ""
        ensure_docker_for_langgraph
        start_langgraph
        echo ""
    fi

    echo -e "${GREEN}Starting Nexus server...${NC}"
    echo ""
    echo "Press Ctrl+C to stop"
    echo ""

    # Setup cleanup on exit
    cleanup() {
        local exit_code=$?
        echo ""
        echo "Shutting down services..."

        # Stop Nexus server first (gracefully)
        if [ -n "$NEXUS_PID" ] && kill -0 $NEXUS_PID 2>/dev/null; then
            echo "Stopping Nexus server (PID: $NEXUS_PID)..."
            kill $NEXUS_PID 2>/dev/null || true

            # Wait for graceful shutdown (up to 10 seconds)
            for i in {1..10}; do
                if ! kill -0 $NEXUS_PID 2>/dev/null; then
                    echo "✓ Nexus server stopped gracefully"
                    break
                fi
                sleep 1
            done

            # Force kill if still running
            if kill -0 $NEXUS_PID 2>/dev/null; then
                echo "Force stopping Nexus server..."
                kill -9 $NEXUS_PID 2>/dev/null || true
                sleep 1
            fi
        fi

        # Stop frontend
        if [ "$START_UI" = true ]; then
            stop_frontend
        fi

        # Stop langgraph
        if [ "$START_LANGGRAPH" = true ]; then
            stop_langgraph
        fi

        # Kill any remaining background jobs from this script
        local bg_jobs=$(jobs -p)
        if [ -n "$bg_jobs" ]; then
            echo "$bg_jobs" | xargs kill 2>/dev/null || true
        fi

        echo -e "${GREEN}✓ All services stopped${NC}"

        # Exit with 0 on graceful shutdown (Ctrl+C should not return error code)
        exit 0
    }
    trap cleanup EXIT INT TERM

    # Wait for server to be ready, then open browser (if frontend enabled)
    if [ "$START_UI" = true ]; then
        (
            # Wait for server to be ready (check health endpoint)
            echo "Waiting for server to be ready..."
            for i in {1..30}; do
                if curl -s http://localhost:${NEXUS_PORT:-2026}/health >/dev/null 2>&1; then
                    echo "Server is ready!"
                    sleep 1  # Give it one more second

                    # Open browser
                    echo "Opening browser to http://localhost:5173"
                    if [[ "$OSTYPE" == "darwin"* ]]; then
                        open "http://localhost:5173" >/dev/null 2>&1 || true
                    elif command -v xdg-open >/dev/null 2>&1; then
                        xdg-open "http://localhost:5173" >/dev/null 2>&1 || true
                    fi
                    break
                fi
                sleep 1
            done
        ) &
    fi

    # Start the Nexus server in background and capture PID
    if [ "$NO_AUTH" = true ]; then
        nexus serve \
            --config ./configs/config.demo.yaml \
            --port ${NEXUS_PORT:-2026} \
            --async &
    else
        nexus serve \
            --config ./configs/config.demo.yaml \
            --auth-type database \
            --port ${NEXUS_PORT:-2026} \
            --async &
    fi
    NEXUS_PID=$!

    echo "Nexus server started (PID: $NEXUS_PID)"
    echo ""

    # Wait for the Nexus server process
    wait $NEXUS_PID
}

# Function to stop the server
stop_server() {
    echo ""
    echo "Stopping local Nexus server and related services..."
    echo ""

    # Stop frontend
    stop_frontend

    # Stop langgraph
    stop_langgraph

    # Kill all processes on ports 2026, 5173, and 2024
    for PORT in 2026 5173 2024; do
        PIDS=$(lsof -ti :$PORT 2>/dev/null || true)

        if [ -n "$PIDS" ]; then
            echo "Killing process(es) on port $PORT..."
            for PID in $PIDS; do
                echo "  Killing PID $PID"
                kill -9 $PID 2>/dev/null || true
            done
            echo -e "${GREEN}✓ Port $PORT cleared${NC}"
        else
            echo "No process running on port $PORT"
        fi
    done

    echo ""
    echo -e "${GREEN}✓ All services stopped${NC}"
    echo ""
}

# Function to initialize database
init_database() {
    # Parse arguments
    parse_args "$@"

    echo ""
    echo "╔═══════════════════════════════════════════╗"
    echo "║     Initializing Nexus Database          ║"
    echo "╚═══════════════════════════════════════════╝"
    echo ""

    # Stop all running servers first to prevent database lock issues
    echo "🛑 Stopping all running services..."
    stop_server
    echo ""

    # Get data directory (absolute) and ALWAYS clear it for init
    DATA_PATH=$(get_data_path "$DATA_DIR")
    echo "  Removing data directory: $DATA_PATH"
    rm -rf "$DATA_PATH"
    mkdir -p "$DATA_PATH"
    echo -e "  ${GREEN}✓ Data directory reset${NC}"

    # Handle database setup based on mode
    if [ "$SQLITE" = true ]; then
        echo -e "${GREEN}Using SQLite database (embedded mode)${NC}"
        # Set SQLite database URL
        POSTGRES_URL="sqlite:///${DATA_PATH}/nexus.db"
        clean_sqlite_artifacts "${DATA_PATH}/nexus.db"
    else
        # Ensure PostgreSQL is running
        ensure_postgres_running
    fi

    # Ensure docker sandbox image exists (strict for init)
    ensure_docker_sandbox_image || exit 1

    echo -e "${YELLOW}⚠️  This will DELETE ALL DATA in: ${DATA_PATH}${NC}"
    echo ""
    echo -e "${BLUE}Configuration:${NC}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    if [ "$SQLITE" = true ]; then
        echo "  Database:     SQLite (${DATA_PATH}/nexus.db)"
    else
        echo "  Database:     PostgreSQL ($POSTGRES_URL)"
    fi
    echo "  Data Dir:     $DATA_PATH"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    if [ "${AUTO_INIT:-false}" != "true" ]; then
        read -p "Are you sure you want to continue? This will DELETE all data in ${DATA_PATH}! [y/N] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo "Aborted."
            exit 0
        fi
    else
        echo "AUTO_INIT=true, skipping confirmation."
    fi

    echo ""
    echo "🧹 Clearing existing data..."

    if [ "$SQLITE" = true ]; then
        echo "  SQLite database will be recreated at: ${DATA_PATH}/nexus.db"
    else
        # Use configuration from config file
        DB_NAME="${POSTGRES_DB}"
        DB_USER="${POSTGRES_USER}"
        CONTAINER_NAME="${POSTGRES_CONTAINER}"

        # Clear database - drop and recreate
        # Note: Must connect to 'postgres' database to drop other databases
        # Also need to terminate active connections before dropping
        if docker ps | grep -q "^.*${CONTAINER_NAME}"; then
            echo "  Terminating connections to database: ${DB_NAME}"
            docker exec ${CONTAINER_NAME} psql -U ${DB_USER} -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${DB_NAME}' AND pid <> pg_backend_pid();" > /dev/null 2>&1 || true
            sleep 1
            echo "  Dropping database: ${DB_NAME}"
            docker exec ${CONTAINER_NAME} psql -U ${DB_USER} -d postgres -c "DROP DATABASE IF EXISTS ${DB_NAME};" > /dev/null 2>&1 || true
            sleep 1
            echo "  Creating database: ${DB_NAME}"
            if docker exec ${CONTAINER_NAME} psql -U ${DB_USER} -d postgres -c "CREATE DATABASE ${DB_NAME};" > /dev/null 2>&1; then
                echo -e "  ${GREEN}✓ Database cleared${NC}"
            else
                echo -e "  ${RED}✗ Failed to create database${NC}"
                echo "  Trying to see what went wrong..."
                docker exec ${CONTAINER_NAME} psql -U ${DB_USER} -d postgres -c "CREATE DATABASE ${DB_NAME};" 2>&1 | head -5
                exit 1
            fi
        else
            echo -e "  ${YELLOW}⚠ PostgreSQL container not found, skipping database clear${NC}"
        fi
    fi

    # Data directory already reset above
    echo ""

    # Set up environment variables
    export NEXUS_DB_PATH="$POSTGRES_URL"
    export NEXUS_DATABASE_URL="$POSTGRES_URL"
    export TOKEN_MANAGER_DB="$POSTGRES_URL"
    export NEXUS_DATA_DIR="$DATA_PATH"

    # Ensure Python environment is ready (first-time install support)
    ensure_core_python_env

    # Run database initialization script
    echo "📊 Running database initialization..."

    if [ ! -f "${SCRIPT_DIR}/init_database.py" ]; then
        echo -e "${RED}ERROR: ${SCRIPT_DIR}/init_database.py not found${NC}"
        exit 1
    fi

    python3 "${SCRIPT_DIR}/init_database.py"

    if [ $? -ne 0 ]; then
        echo ""
        echo -e "${RED}✗ Database initialization failed${NC}"
        echo ""
        exit 1
    fi

    echo ""
    echo "👤 Creating admin user and API key..."

    # Use API key from config file (loaded at script start)
    # This matches docker-integration.yml for consistency

    # Create admin user and API key using the extracted Python script
    # Always use "system" zone for admin API keys created via --init
    ZONE_ID="system"
    python3 "${SCRIPT_DIR}/setup_admin_api_key.py" "$NEXUS_DATABASE_URL" "$ADMIN_API_KEY" "$ZONE_ID"

    if [ $? -ne 0 ]; then
        echo -e "${RED}✗ Failed to create admin API key${NC}"
        exit 1
    fi

    echo -e "${GREEN}✓ Admin API key configured: ${ADMIN_API_KEY:0:30}...${NC}"

    # Export API key for provisioning
    export NEXUS_API_KEY="$ADMIN_API_KEY"

    NEXUS_URL_VALUE="http://localhost:${NEXUS_PORT:-2026}"

    # Save to .nexus-admin-env for easy sourcing (standard format)
    cat > .nexus-admin-env << EOF
# Nexus Admin Environment
# Created: $(date)
# User: admin
export NEXUS_API_KEY='$ADMIN_API_KEY'
export NEXUS_URL='$NEXUS_URL_VALUE'
export NEXUS_DATABASE_URL='$POSTGRES_URL'
EOF

    echo -e "${GREEN}✓ Saved credentials to .nexus-admin-env${NC}"
    echo ""
    echo "  To use in your shell:"
    echo "    source .nexus-admin-env"
    echo ""

    # Also save to .env for future use
    if [ ! -f .env ]; then
        touch .env
    fi

    # Update or add NEXUS_API_KEY to .env
    if grep -q "^NEXUS_API_KEY=" .env 2>/dev/null; then
        sed -i.bak "s|^NEXUS_API_KEY=.*|NEXUS_API_KEY=$ADMIN_API_KEY|" .env
        rm -f .env.bak
    else
        echo "NEXUS_API_KEY=$ADMIN_API_KEY" >> .env
    fi

    echo ""
    echo -e "${GREEN}✓ Database initialized successfully!${NC}"
    echo ""
    echo "Admin API Key: $ADMIN_API_KEY"
    echo ""
    echo "🚀 Starting Nexus server..."
    echo ""

    # Ensure PostgreSQL is running (skip if using SQLite)
    if [ "$SQLITE" != true ]; then
        ensure_postgres_running
    fi

    # Get data directory path (create if missing)
    DATA_PATH=$(get_data_path "$DATA_DIR")

    # Set up environment variables for server
    export NEXUS_DB_PATH="$POSTGRES_URL"
    export NEXUS_DATABASE_URL="$POSTGRES_URL"
    export TOKEN_MANAGER_DB="$POSTGRES_URL"
    export NEXUS_DATA_DIR="$DATA_PATH"
    export NEXUS_API_KEY="$ADMIN_API_KEY"

    # Environment variables already loaded at script start from .env

    # Check if port 2026 is available before starting server
    if ! check_port_2026_available; then
        exit 1
    fi

    # Ensure Python environment is ready (first-time install support)
    ensure_core_python_env

    # Display configuration
    echo ""
    echo -e "${BLUE}Configuration:${NC}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Config File:  ./configs/config.demo.yaml"
    if [ "$SQLITE" = true ]; then
        echo "  Database:     SQLite (${DATA_PATH}/nexus.db)"
    else
        echo "  Database:     PostgreSQL ($POSTGRES_URL)"
    fi
    echo "  Data Dir:     $DATA_PATH"
    echo "  Host:         0.0.0.0"
    echo "  Port:         ${NEXUS_PORT:-2026}"
    if [ "$START_UI" = true ]; then
        echo "  Frontend:     Enabled"
    fi
    if [ "$START_LANGGRAPH" = true ]; then
        echo "  Langgraph:    Enabled"
    fi
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # Start frontend if requested
    if [ "$START_UI" = true ]; then
        echo ""
        start_frontend
        echo ""
    fi

    # Start langgraph if requested
    if [ "$START_LANGGRAPH" = true ]; then
        echo ""
        ensure_docker_for_langgraph
        start_langgraph
        echo ""
    fi

    echo -e "${GREEN}Starting Nexus server...${NC}"
    echo ""
    echo "Press Ctrl+C to stop"
    echo ""

    # Setup cleanup on exit
    cleanup() {
        local exit_code=$?
        echo ""
        echo "Shutting down services..."

        # Stop Nexus server first (gracefully)
        if [ -n "$NEXUS_PID" ] && kill -0 $NEXUS_PID 2>/dev/null; then
            echo "Stopping Nexus server (PID: $NEXUS_PID)..."
            kill $NEXUS_PID 2>/dev/null || true

            # Wait for graceful shutdown (up to 10 seconds)
            for i in {1..10}; do
                if ! kill -0 $NEXUS_PID 2>/dev/null; then
                    echo "✓ Nexus server stopped gracefully"
                    break
                fi
                sleep 1
            done

            # Force kill if still running
            if kill -0 $NEXUS_PID 2>/dev/null; then
                echo "Force stopping Nexus server..."
                kill -9 $NEXUS_PID 2>/dev/null || true
                sleep 1
            fi
        fi

        # Stop frontend
        if [ "$START_UI" = true ]; then
            stop_frontend
        fi

        # Stop langgraph
        if [ "$START_LANGGRAPH" = true ]; then
            stop_langgraph
        fi

        # Kill any remaining background jobs from this script
        local bg_jobs=$(jobs -p)
        if [ -n "$bg_jobs" ]; then
            echo "$bg_jobs" | xargs kill 2>/dev/null || true
        fi

        echo -e "${GREEN}✓ All services stopped${NC}"

        # Exit with 0 on graceful shutdown (Ctrl+C should not return error code)
        exit 0
    }
    trap cleanup EXIT INT TERM

    # Wait for server to be ready, then open browser (if frontend enabled)
    if [ "$START_UI" = true ]; then
        (
            # Wait for server to be ready (check health endpoint)
            echo "Waiting for server to be ready..."
            for i in {1..30}; do
                if curl -s http://localhost:${NEXUS_PORT:-2026}/health >/dev/null 2>&1; then
                    echo "Server is ready!"
                    sleep 1  # Give it one more second

                    # Open browser
                    echo "Opening browser to http://localhost:5173"
                    if [[ "$OSTYPE" == "darwin"* ]]; then
                        open "http://localhost:5173" >/dev/null 2>&1 || true
                    elif command -v xdg-open >/dev/null 2>&1; then
                        xdg-open "http://localhost:5173" >/dev/null 2>&1 || true
                    fi
                    break
                fi
                sleep 1
            done
        ) &
    fi

    # Start the Nexus server in background and capture PID
    if [ "$NO_AUTH" = true ]; then
        nexus serve \
            --config ./configs/config.demo.yaml \
            --port ${NEXUS_PORT:-2026} \
            --async &
    else
        nexus serve \
            --config ./configs/config.demo.yaml \
            --auth-type database \
            --port ${NEXUS_PORT:-2026} \
            --async &
    fi
    NEXUS_PID=$!

    echo "Nexus server started (PID: $NEXUS_PID)"
    echo ""

    # Wait for the Nexus server process
    wait $NEXUS_PID
}

# Main script logic
case "$1" in
    --start)
        shift  # Remove --start from arguments
        start_server "$@"
        ;;
    --stop)
        stop_server
        ;;
    --init)
        shift  # Remove --init from arguments
        init_database "$@"
        ;;
    *)
        echo ""
        echo "Usage: $0 {--start|--stop|--init} [OPTIONS]"
        echo ""
        echo "Commands:"
        echo "  --start    Start Nexus server locally (outside Docker)"
        echo "  --stop     Stop local Nexus server"
        echo "  --init     Initialize database (clear all data, create admin key, start server)"
        echo ""
        echo "Options for --start, --init, and --stop:"
        echo "  --sqlite   Use SQLite instead of PostgreSQL"
        echo "  --nosqlite     Use PostgreSQL instead of SQLite (default)"
        echo "  --postgres-url URL    PostgreSQL connection URL"
        echo "                       (default: $DEFAULT_POSTGRES_URL)"
        echo "  --data-dir PATH       Data directory path"
        echo "                       (default: $DEFAULT_DATA_DIR)"
        echo "  --ui                  Start the frontend (pnpm run dev in nexus-frontend) (default)"
        echo "  --no-ui, --noui      Don't start the frontend"
        echo "  --langgraph          Start langgraph dev server (default)"
        echo "  --no-langgraph, --nolanggraph   Don't start langgraph"
        echo ""
        echo "Examples:"
        echo "  # Initialize database (clean state) with PostgreSQL:"
        echo "  $0 --init"
        echo ""
        echo "  # Initialize with SQLite:"
        echo "  $0 --init --sqlite"
        echo ""
        echo "  # Using PostgreSQL (default):"
        echo "  $0 --start"
        echo ""
        echo "  # Using SQLite:"
        echo "  $0 --start --sqlite"
        echo ""
        echo "  # Using custom PostgreSQL URL:"
        echo "  $0 --start --postgres-url 'postgresql://user:pass@localhost:5432/db'"
        echo ""
        echo "  # Start with frontend and langgraph (PostgreSQL default):"
        echo "  $0 --start --ui --langgraph"
        echo ""
        echo "  # Start with SQLite:"
        echo "  $0 --start --sqlite --ui --langgraph"
        echo ""
        echo "  # Start without UI or langgraph:"
        echo "  $0 --start --noui --nolanggraph"
        echo ""
        echo "  # Custom data directory (PostgreSQL):"
        echo "  $0 --start --data-dir '/custom/path'"
        echo ""
        echo "  # Custom data directory (SQLite):"
        echo "  $0 --start --sqlite --data-dir '/custom/path'"
        echo ""
        echo "Optional: Enable connectors (GDrive, Gmail) in PostgreSQL mode:"
        echo "  sudo bash -c 'echo \"127.0.0.1    postgres\" >> /etc/hosts'"
        echo "  This maps 'postgres' hostname to localhost for connector database access."
        echo ""
        exit 1
        ;;
esac

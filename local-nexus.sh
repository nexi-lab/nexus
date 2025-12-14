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

# Load configuration from config file
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/configs/local-dev.env"

if [ -f "$CONFIG_FILE" ]; then
    source "$CONFIG_FILE"
    echo -e "${GREEN}✓ Loaded configuration from ${CONFIG_FILE}${NC}"
else
    echo -e "${RED}✗ Configuration file not found: ${CONFIG_FILE}${NC}"
    echo "Using fallback default values..."
    # Fallback defaults
    NEXUS_DATA_DIR="/tmp/nexus-data"
    POSTGRES_USER="nexus_test"
    POSTGRES_PASSWORD="nexus_test_password"
    POSTGRES_DB="tmp_nexus_test"
    POSTGRES_PORT="5433"
    POSTGRES_HOST="localhost"
    POSTGRES_CONTAINER="nexus-test-postgres"
    POSTGRES_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
    ADMIN_API_KEY="sk-default_admin_dddddddd_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
fi

# Set defaults for backward compatibility
DEFAULT_DATA_DIR="${NEXUS_DATA_DIR}"
DEFAULT_POSTGRES_URL="${POSTGRES_URL}"
export TOKEN_MANAGER_DB="$DEFAULT_POSTGRES_URL"

# Function to get data directory path
get_data_path() {
    local data_dir="${1:-$DEFAULT_DATA_DIR}"

    # Create directory if it doesn't exist
    if [ ! -d "$data_dir" ]; then
        echo -e "${GREEN}Creating data directory: $data_dir${NC}" >&2
        mkdir -p "$data_dir"
    fi

    echo "$data_dir"
}

# Function to parse command-line arguments
parse_args() {
    POSTGRES_URL="$DEFAULT_POSTGRES_URL"
    DATA_DIR="$DEFAULT_DATA_DIR"

    while [[ $# -gt 0 ]]; do
        case $1 in
            --postgres-url)
                POSTGRES_URL="$2"
                shift 2
                ;;
            --data-dir)
                DATA_DIR="$2"
                shift 2
                ;;
            *)
                # Unknown option, pass through
                shift
                ;;
        esac
    done
}

# Function to ensure PostgreSQL is running
ensure_postgres_running() {
    # Use configuration from config file
    CONTAINER_NAME="${POSTGRES_CONTAINER}"
    DB_NAME="${POSTGRES_DB}"
    DB_USER="${POSTGRES_USER}"
    DB_PASSWORD="${POSTGRES_PASSWORD}"
    DB_PORT="${POSTGRES_PORT}"
    POSTGRES_DATA_DIR="/tmp/nexus-postgres"

    if ! docker ps | grep -q "nexus.*postgres"; then
        echo -e "${YELLOW}PostgreSQL container not running, starting...${NC}"

        # Check if container exists but is stopped
        if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
            echo "Starting existing container: ${CONTAINER_NAME}"
            docker start ${CONTAINER_NAME}
        else
            # Create new container
            echo "Creating new PostgreSQL container: ${CONTAINER_NAME}"

            # Create data directory if it doesn't exist
            mkdir -p ${POSTGRES_DATA_DIR}

            # Start PostgreSQL container
            docker run -d \
                --name ${CONTAINER_NAME} \
                -e POSTGRES_DB=${DB_NAME} \
                -e POSTGRES_USER=${DB_USER} \
                -e POSTGRES_PASSWORD=${DB_PASSWORD} \
                -p ${DB_PORT}:5432 \
                -v ${POSTGRES_DATA_DIR}:/var/lib/postgresql/data \
                postgres:15-alpine

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

# Function to start the server
start_server() {
    # Parse arguments
    parse_args "$@"

    echo ""
    echo "╔═══════════════════════════════════════════╗"
    echo "║     Starting Nexus Server Locally        ║"
    echo "╚═══════════════════════════════════════════╝"
    echo ""

    # Check and start PostgreSQL container if needed
    ensure_postgres_running

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

    # Get data directory path (create if missing)
    DATA_PATH=$(get_data_path "$DATA_DIR")

    # Use provided postgres URL or default
    export NEXUS_DB_PATH="$POSTGRES_URL"
    export NEXUS_DATABASE_URL="$POSTGRES_URL"
    export TOKEN_MANAGER_DB="$POSTGRES_URL"
    export NEXUS_DATA_DIR="$DATA_PATH"

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

# Function to initialize database
init_database() {
    # Parse arguments
    parse_args "$@"

    echo ""
    echo "╔═══════════════════════════════════════════╗"
    echo "║     Initializing Nexus Database          ║"
    echo "╚═══════════════════════════════════════════╝"
    echo ""

    # Ensure PostgreSQL is running
    ensure_postgres_running

    # Extract database name from POSTGRES_URL
    # Format: postgresql://user:pass@host:port/dbname
    DB_NAME=$(echo "$POSTGRES_URL" | sed -n 's/.*\/\([^?]*\).*/\1/p')
    DB_USER=$(echo "$POSTGRES_URL" | sed -n 's/.*:\/\/\([^:]*\):.*/\1/p')
    DB_HOST=$(echo "$POSTGRES_URL" | sed -n 's/.*@\([^:]*\):.*/\1/p')
    DB_PORT=$(echo "$POSTGRES_URL" | sed -n 's/.*:\([0-9]*\)\/.*/\1/p')

    # Get data directory
    DATA_PATH=$(get_data_path "$DATA_DIR")

    echo -e "${YELLOW}⚠️  This will CLEAR all existing data!${NC}"
    echo ""
    echo -e "${BLUE}Configuration:${NC}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Database:     $POSTGRES_URL"
    echo "  Data Dir:     $DATA_PATH"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    read -p "Are you sure you want to continue? This will DELETE all data! [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi

    echo ""
    echo "🧹 Clearing existing data..."

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

    # Clear data directory
    if [ -d "$DATA_PATH" ]; then
        echo "  Removing data directory: $DATA_PATH"
        rm -rf "$DATA_PATH"/*
        echo -e "  ${GREEN}✓ Data directory cleared${NC}"
    else
        echo "  Data directory doesn't exist, will be created"
    fi

    echo ""

    # Set up environment variables
    export NEXUS_DB_PATH="$POSTGRES_URL"
    export NEXUS_DATABASE_URL="$POSTGRES_URL"
    export TOKEN_MANAGER_DB="$POSTGRES_URL"
    export NEXUS_DATA_DIR="$DATA_PATH"

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

    # Run database initialization script
    echo "📊 Running database initialization..."
    cd "$(dirname "$0")"

    if [ ! -f "scripts/init_database.py" ]; then
        echo -e "${RED}ERROR: scripts/init_database.py not found${NC}"
        exit 1
    fi

    python3 scripts/init_database.py

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
    python3 "${SCRIPT_DIR}/scripts/setup_admin_api_key.py" "$NEXUS_DATABASE_URL" "$ADMIN_API_KEY"

    if [ $? -ne 0 ]; then
        echo -e "${RED}✗ Failed to create admin API key${NC}"
        exit 1
    fi

    echo -e "${GREEN}✓ Admin API key configured: ${ADMIN_API_KEY:0:30}...${NC}"

    # Export API key for provisioning
    export NEXUS_API_KEY="$ADMIN_API_KEY"

    # Save to .env.local for future use
    if [ ! -f .env.local ]; then
        touch .env.local
    fi

    # Update or add NEXUS_API_KEY to .env.local
    if grep -q "^NEXUS_API_KEY=" .env.local 2>/dev/null; then
        sed -i.bak "s|^NEXUS_API_KEY=.*|NEXUS_API_KEY=$ADMIN_API_KEY|" .env.local
        rm -f .env.local.bak
    else
        echo "NEXUS_API_KEY=$ADMIN_API_KEY" >> .env.local
    fi

    echo ""
    echo -e "${GREEN}✓ Database initialized successfully!${NC}"
    echo ""
    echo "Admin API Key: $ADMIN_API_KEY"
    echo ""
    echo "🚀 Starting Nexus server..."
    echo ""

    # Ensure PostgreSQL is running
    ensure_postgres_running

    # Get data directory path (create if missing)
    DATA_PATH=$(get_data_path "$DATA_DIR")

    # Set up environment variables for server
    export NEXUS_DB_PATH="$POSTGRES_URL"
    export NEXUS_DATABASE_URL="$POSTGRES_URL"
    export TOKEN_MANAGER_DB="$POSTGRES_URL"
    export NEXUS_DATA_DIR="$DATA_PATH"
    export NEXUS_API_KEY="$ADMIN_API_KEY"

    # Load environment variables from .env.local if it exists
    if [ -f .env.local ]; then
        set -a
        source .env.local
        set +a
    fi

    # Check if port 8080 is already in use and stop it
    if lsof -ti :8080 >/dev/null 2>&1; then
        echo -e "${YELLOW}Port 8080 is already in use, stopping existing server...${NC}"
        echo ""

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

            echo -e "${GREEN}✓ Existing server stopped${NC}"
            echo ""
            sleep 1
        fi
    fi

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

    # Display configuration
    echo ""
    echo -e "${BLUE}Configuration:${NC}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Config File:  ./configs/config.demo.yaml"
    echo "  Database:     $POSTGRES_URL"
    echo "  Data Dir:     $DATA_PATH"
    echo "  Host:         0.0.0.0"
    echo "  Port:         8080"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo -e "${GREEN}Starting server...${NC}"
    echo ""
    echo "Press Ctrl+C to stop"
    echo ""

    # Wait a moment for server to start, then run provisioning in background
    (
        sleep 5
        echo ""
        echo "📦 Running provisioning..."
        if [ -f "scripts/provision_namespace.py" ]; then
            # Activate venv in the background process
            source .venv/bin/activate
            # Set environment variables for embedded mode provisioning
            # Note: Provisioning uses embedded mode (not server mode) because
            # the provisioning script uses context parameters not supported by RemoteNexusFS
            export NEXUS_DATABASE_URL="$POSTGRES_URL"
            export NEXUS_DATA_DIR="$DATA_PATH"
            export NEXUS_API_KEY="$ADMIN_API_KEY"
            # Don't set NEXUS_URL - this forces embedded mode
            unset NEXUS_URL
            # Load .env.local if it exists
            if [ -f .env.local ]; then
                set -a
                source .env.local
                set +a
            fi
            python3 scripts/provision_namespace.py --tenant default --env-file .env.local 2>&1
            if [ $? -eq 0 ]; then
                echo -e "${GREEN}✓ Provisioning completed successfully${NC}"
            else
                echo -e "${YELLOW}⚠ Provisioning encountered errors${NC}"
            fi
        fi
    ) &

    # Start the Nexus server (this blocks)
    nexus serve \
        --config ./configs/config.demo.yaml \
        --auth-type database \
        --async
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
        echo "  --init     Initialize database schema"
        echo ""
        echo "Options for --start and --init:"
        echo "  --postgres-url URL    PostgreSQL connection URL"
        echo "                       (default: $DEFAULT_POSTGRES_URL)"
        echo "  --data-dir PATH       Data directory path"
        echo "                       (default: $DEFAULT_DATA_DIR)"
        echo ""
        echo "Examples:"
        echo "  $0 --start"
        echo "  $0 --init"
        echo "  $0 --start --postgres-url 'postgresql://user:pass@localhost:5432/db'"
        echo "  $0 --init --postgres-url 'postgresql://user:pass@localhost:5432/db'"
        echo "  $0 --start --data-dir '/custom/path'"
        echo "  $0 --start --postgres-url 'postgresql://...' --data-dir '/custom/path'"
        echo ""
        echo "Workflow:"
        echo "  1. Initialize database:         ./local-nexus.sh --init"
        echo "  2. Start Docker services:       ./docker-start.sh"
        echo "  3. Stop Docker nexus-server:    docker stop nexus-server"
        echo "  4. Start local nexus:           ./local-nexus.sh --start"
        echo "  5. Make changes and restart:    Ctrl+C then ./local-nexus.sh --start"
        echo "  6. When done:                   docker start nexus-server"
        echo ""
        echo "Optional: Enable connectors (GDrive, Gmail) in local mode:"
        echo "  sudo bash -c 'echo \"127.0.0.1    postgres\" >> /etc/hosts'"
        echo "  This maps 'postgres' hostname to localhost for connector database access."
        echo ""
        exit 1
        ;;
esac

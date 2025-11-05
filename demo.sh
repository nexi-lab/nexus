#!/bin/bash
# demo.sh - Start all Nexus services locally for development
#
# Usage:
#   ./demo.sh                                        # Start Nexus backend + frontend
#   ./demo.sh --init                                 # Initialize (clean data, preserve credentials)
#   ./demo.sh --init --clean-credentials             # Initialize (clean data AND credentials)
#   ./demo.sh --start_agent                          # Start with LangGraph server
#   ./demo.sh --start_sandbox                        # Start with Docker sandbox (local code execution)
#   ./demo.sh --start_agent --start_sandbox --init   # All services + init

set -e  # Exit on error

# ============================================
# Configuration
# ============================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NEXUS_DIR="$SCRIPT_DIR"
FRONTEND_DIR="$SCRIPT_DIR/../nexus-frontend"

# Nexus server configuration
export NEXUS_DATABASE_URL="${NEXUS_DATABASE_URL:-postgresql://postgres:nexus@localhost/nexus}"
export NEXUS_DATA_DIR="$SCRIPT_DIR/../nexus-data"
export NEXUS_PORT=8080
export NEXUS_HOST=0.0.0.0

# Frontend configuration
FRONTEND_PORT=5173

# LangGraph configuration
LANGGRAPH_PORT=2024
LANGGRAPH_DIR="$SCRIPT_DIR/examples/langgraph"

# Feature flags
START_AGENT=false        # LangGraph server
START_SANDBOX=false      # Docker sandbox
START_NGROK=true         # Always start ngrok for backend
NGROK_URL=""

# Parse arguments
INIT_MODE=""
CLEAN_CREDENTIALS=""
for arg in "$@"; do
    if [ "$arg" == "--init" ]; then
        INIT_MODE="--init"
    elif [ "$arg" == "--clean-credentials" ]; then
        CLEAN_CREDENTIALS="1"
    elif [ "$arg" == "--start_agent" ]; then
        START_AGENT=true
    elif [ "$arg" == "--start_sandbox" ]; then
        START_SANDBOX=true
    fi
done

# Export for init script
if [ -n "$CLEAN_CREDENTIALS" ]; then
    export CLEAN_CREDENTIALS
fi

# ============================================
# Banner
# ============================================

cat << 'EOF'
╔═══════════════════════════════════════════╗
║      Nexus Local Development Demo        ║
╚═══════════════════════════════════════════╝
EOF
echo ""

# ============================================
# Initialization Confirmation (if --init)
# ============================================

if [ -n "$INIT_MODE" ]; then
    echo "⚠️  INITIALIZATION MODE"
    echo ""

    if [ "$CLEAN_CREDENTIALS" = "1" ]; then
        echo "This will DELETE ALL existing data AND credentials:"
        echo "  • All users and API keys (CREDENTIALS)"
        echo "  • All files and metadata"
        echo "  • All permissions and relationships"
        echo "  • All workspaces and configurations"
        echo "  • All operation logs and caches"
    else
        echo "This will DELETE data but PRESERVE credentials:"
        echo "  • All files and metadata"
        echo "  • All permissions and relationships"
        echo "  • All workspaces and configurations"
        echo "  • All operation logs and caches"
        echo ""
        echo "The following will be PRESERVED:"
        echo "  ✓ All users and API keys (existing credentials still work)"
    fi
    echo ""

    read -p "Are you sure you want to continue? (yes/no): " CONFIRM
    if [ "$CONFIRM" != "yes" ]; then
        echo ""
        echo "❌ Initialization cancelled"
        exit 0
    fi
    echo ""
    echo "✓ Confirmed - proceeding with initialization..."
    echo ""

    # Auto-confirm for the init script (already confirmed here)
    export AUTO_CONFIRM=1
fi

# ============================================
# Pre-flight Checks
# ============================================

echo "🔍 Pre-flight checks..."
echo ""

# Check/clone frontend repository
echo "  Checking nexus-frontend repository..."
if [ ! -d "$FRONTEND_DIR" ]; then
    echo "  📥 Frontend not found, cloning repository..."
    git clone https://github.com/nexi-lab/nexus-frontend.git "$FRONTEND_DIR"
    echo "  ✅ Frontend repository cloned"
else
    echo "  ✅ Frontend repository exists"
fi

# Detect OS
OS_TYPE="unknown"
if [[ "$OSTYPE" == "darwin"* ]]; then
    OS_TYPE="macos"
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    OS_TYPE="linux"
fi

# Check PostgreSQL
echo "  Checking PostgreSQL..."

# Check if PostgreSQL is installed
if ! command -v psql &> /dev/null; then
    echo "  ⚠️  PostgreSQL not found. Installing..."
    if [ "$OS_TYPE" == "macos" ]; then
        if command -v brew &> /dev/null; then
            brew install postgresql@15
            echo "  ✅ PostgreSQL installed via Homebrew"
        else
            echo "  ❌ Homebrew not found. Please install PostgreSQL manually:"
            echo "    https://www.postgresql.org/download/macosx/"
            exit 1
        fi
    elif [ "$OS_TYPE" == "linux" ]; then
        if command -v apt-get &> /dev/null; then
            sudo apt-get update
            sudo apt-get install -y postgresql postgresql-contrib
            echo "  ✅ PostgreSQL installed via apt"
        elif command -v yum &> /dev/null; then
            sudo yum install -y postgresql-server postgresql-contrib
            sudo postgresql-setup --initdb
            echo "  ✅ PostgreSQL installed via yum"
        else
            echo "  ❌ Package manager not found. Please install PostgreSQL manually:"
            echo "    https://www.postgresql.org/download/linux/"
            exit 1
        fi
    else
        echo "  ❌ Unsupported OS. Please install PostgreSQL manually."
        exit 1
    fi
fi

# Check if PostgreSQL is running
echo "  Checking if PostgreSQL is running..."
if ! pg_isready -h localhost -p 5432 &> /dev/null; then
    echo "  ⚠️  PostgreSQL not running. Starting..."
    if [ "$OS_TYPE" == "macos" ]; then
        brew services start postgresql@15 || brew services start postgresql
        sleep 3  # Give it time to start
        echo "  ✅ PostgreSQL started"
    elif [ "$OS_TYPE" == "linux" ]; then
        sudo systemctl start postgresql
        sudo systemctl enable postgresql
        sleep 3
        echo "  ✅ PostgreSQL started"
    fi
fi

# For Linux, we may need to create the postgres user or switch to it
POSTGRES_USER="postgres"
POSTGRES_CMD="psql"
if [ "$OS_TYPE" == "linux" ]; then
    # On Linux, we typically need to use sudo -u postgres
    POSTGRES_CMD="sudo -u postgres psql"
fi

# Check if database exists and is accessible
echo "  Checking database 'nexus'..."
DB_EXISTS=false
if [ "$OS_TYPE" == "macos" ]; then
    # On macOS, postgres user may not exist, use current user
    if psql -lqt 2>/dev/null | cut -d \| -f 1 | grep -qw nexus; then
        DB_EXISTS=true
    fi
elif [ "$OS_TYPE" == "linux" ]; then
    if sudo -u postgres psql -lqt 2>/dev/null | cut -d \| -f 1 | grep -qw nexus; then
        DB_EXISTS=true
    fi
fi

if [ "$DB_EXISTS" = false ]; then
    echo "  ⚠️  Database 'nexus' not found. Creating..."
    if [ "$OS_TYPE" == "macos" ]; then
        createdb nexus 2>/dev/null || true
        # Create postgres user if it doesn't exist (macOS)
        psql -d nexus -c "CREATE USER postgres WITH PASSWORD 'nexus';" 2>/dev/null || true
        psql -d nexus -c "ALTER USER postgres WITH PASSWORD 'nexus';" 2>/dev/null || true
        psql -d nexus -c "GRANT ALL PRIVILEGES ON DATABASE nexus TO postgres;" 2>/dev/null || true
    elif [ "$OS_TYPE" == "linux" ]; then
        sudo -u postgres createdb nexus
        sudo -u postgres psql -c "ALTER USER postgres PASSWORD 'nexus';"
    fi
    echo "  ✅ Database 'nexus' created"
else
    # Database exists, ensure password is set
    if [ "$OS_TYPE" == "macos" ]; then
        psql -d nexus -c "ALTER USER postgres WITH PASSWORD 'nexus';" 2>/dev/null || true
    elif [ "$OS_TYPE" == "linux" ]; then
        sudo -u postgres psql -c "ALTER USER postgres PASSWORD 'nexus';"
    fi
fi

# Final connectivity check
if ! PGPASSWORD=nexus psql -h localhost -U postgres -d nexus -c '\q' 2>/dev/null; then
    echo "  ⚠️  Database connection test failed"
    echo "  Trying alternative connection methods..."

    # For macOS, we might need to connect as current user
    if [ "$OS_TYPE" == "macos" ]; then
        if psql -d nexus -c '\q' 2>/dev/null; then
            echo "  ℹ️  Note: Connected as current user instead of 'postgres'"
            echo "  Update NEXUS_DATABASE_URL if needed"
        fi
    fi
fi

echo "  ✅ PostgreSQL ready"

# Check Node.js
echo "  Checking Node.js..."
if ! command -v node &> /dev/null; then
    echo "  ❌ Node.js not found. Please install Node.js."
    exit 1
fi
echo "  ✅ Node.js ready ($(node --version))"

# Check uv for LangGraph
echo "  Checking uv (for LangGraph)..."
if ! command -v uv &> /dev/null; then
    echo "  ⚠️  uv not found. Installing..."
    if [ "$OS_TYPE" == "macos" ] || [ "$OS_TYPE" == "linux" ]; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.cargo/bin:$PATH"
        echo "  ✅ uv installed"
    else
        echo "  ❌ Unsupported OS. Please install uv manually:"
        echo "    https://docs.astral.sh/uv/"
        exit 1
    fi
fi
echo "  ✅ uv ready ($(uv --version))"

# Check ngrok (always enabled)
echo "  Checking ngrok..."

# Check if ngrok is installed
if ! command -v ngrok &> /dev/null; then
    echo "  ⚠️  ngrok not found. Installing..."
    if [ "$OS_TYPE" == "macos" ]; then
        if command -v brew &> /dev/null; then
            brew install ngrok/ngrok/ngrok
            echo "  ✅ ngrok installed via Homebrew"
        else
            echo "  ❌ Homebrew not found. Please install ngrok manually:"
            echo "    https://ngrok.com/download"
            exit 1
        fi
    elif [ "$OS_TYPE" == "linux" ]; then
        echo "  📥 Downloading ngrok..."
        curl -s https://ngrok-agent.s3.amazonaws.com/ngrok.asc | \
            sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null && \
            echo "deb https://ngrok-agent.s3.amazonaws.com buster main" | \
            sudo tee /etc/apt/sources.list.d/ngrok.list && \
            sudo apt update && sudo apt install ngrok
        echo "  ✅ ngrok installed"
    else
        echo "  ❌ Unsupported OS. Please install ngrok manually:"
        echo "    https://ngrok.com/download"
        exit 1
    fi
fi

# Check if ngrok is authenticated
echo "  Checking ngrok authentication..."
if ! ngrok config check &> /dev/null; then
    echo "  ⚠️  ngrok not authenticated"
    echo ""
    echo "  Please authenticate ngrok:"
    echo "    1. Sign up at https://dashboard.ngrok.com/signup"
    echo "    2. Get your auth token from https://dashboard.ngrok.com/get-started/your-authtoken"
    echo "    3. Run: ngrok config add-authtoken <YOUR_TOKEN>"
    echo ""
    read -p "  Press Enter after authenticating ngrok..."

    # Check again
    if ! ngrok config check &> /dev/null; then
        echo "  ❌ ngrok authentication failed. Please try again."
        exit 1
    fi
fi

echo "  ✅ ngrok ready"

# Check Python virtual environment
echo "  Checking Python virtual environment..."
if [ ! -d "$NEXUS_DIR/.venv" ]; then
    echo "  ⚠️  Virtual environment not found at $NEXUS_DIR/.venv"
    echo "  Creating virtual environment..."
    cd "$NEXUS_DIR"
    python3.11 -m venv .venv
    .venv/bin/pip install -e .
    cd "$SCRIPT_DIR"
fi
echo "  ✅ Python virtual environment ready"

# Check Docker if sandbox mode
if [ "$START_SANDBOX" = true ]; then
    echo "  Checking Docker (sandbox mode)..."

    # Check if Docker is installed
    if ! command -v docker &> /dev/null; then
        echo "  ❌ Docker not found. Please install Docker:"
        echo "    https://docs.docker.com/get-docker/"
        exit 1
    fi
    echo "  ✅ Docker installed ($(docker --version))"

    # Check if Docker daemon is running
    if ! docker info > /dev/null 2>&1; then
        echo "  ❌ Docker is not running"
        echo "    Please start Docker Desktop or Docker daemon"
        exit 1
    fi
    echo "  ✅ Docker daemon is running"

    # Check if docker Python SDK is installed
    echo "  Checking Docker Python SDK..."
    if ! "$NEXUS_DIR/.venv/bin/python" -c "import docker" 2>/dev/null; then
        echo "  ⚠️  Docker Python SDK not found. Installing..."
        "$NEXUS_DIR/.venv/bin/pip" install docker
        echo "  ✅ Docker Python SDK installed"
    else
        echo "  ✅ Docker Python SDK ready"
    fi

    echo "  ✅ Docker sandbox ready"
fi

echo ""

# ============================================
# Stop existing services
# ============================================

echo "🛑 Stopping existing services..."
echo ""

# Stop Nexus backend
echo "  Stopping Nexus backend (port $NEXUS_PORT)..."
lsof -ti:$NEXUS_PORT | xargs kill -9 2>/dev/null || true
sleep 1
echo "  ✅ Nexus backend stopped"

# Stop frontend
echo "  Stopping frontend (port $FRONTEND_PORT)..."
lsof -ti:$FRONTEND_PORT | xargs kill -9 2>/dev/null || true
sleep 1
echo "  ✅ Frontend stopped"

# Stop LangGraph server (always try to stop in case it was running from before)
if [ "$START_AGENT" = true ]; then
    echo "  Stopping LangGraph server (port $LANGGRAPH_PORT)..."
    lsof -ti:$LANGGRAPH_PORT | xargs kill -9 2>/dev/null || true
    sleep 1
    echo "  ✅ LangGraph server stopped"
fi

echo ""

# ============================================
# Start Nexus Backend
# ============================================

echo "🚀 Starting Nexus backend..."
echo ""

cd "$NEXUS_DIR"

# Run initialization script
echo "  Running init-nexus-with-auth.sh $INIT_MODE..."
echo "  Database: $NEXUS_DATABASE_URL"
echo "  Data dir: $NEXUS_DATA_DIR"
echo ""

# Run in background and capture output
./scripts/init-nexus-with-auth.sh $INIT_MODE > /tmp/nexus-backend.log 2>&1 &
NEXUS_PID=$!

# Wait for server to start
echo "  Waiting for Nexus backend to start..."
MAX_WAIT=30
WAITED=0
while [ $WAITED -lt $MAX_WAIT ]; do
    if curl -s http://localhost:$NEXUS_PORT/health > /dev/null 2>&1; then
        echo "  ✅ Nexus backend started (PID: $NEXUS_PID)"
        break
    fi
    sleep 1
    WAITED=$((WAITED + 1))
    if [ $WAITED -eq $MAX_WAIT ]; then
        echo "  ❌ Nexus backend failed to start within ${MAX_WAIT}s"
        echo "  Check logs: tail -f /tmp/nexus-backend.log"
        exit 1
    fi
done

echo ""

# ============================================
# Start ngrok (always enabled for backend)
# ============================================

echo "🌐 Setting up ngrok tunnel..."
echo ""

# Check if ngrok is already running
NGROK_RUNNING=false
if pgrep -x "ngrok" > /dev/null 2>&1; then
    NGROK_RUNNING=true
    echo "  ℹ️  ngrok is already running"
fi

# Only kill and restart if initialization mode is active
if [ -n "$INIT_MODE" ] && [ "$NGROK_RUNNING" = true ]; then
    echo "  🔄 Restarting ngrok (initialization mode)..."
    pkill ngrok 2>/dev/null || true
    sleep 1
    NGROK_RUNNING=false
fi

# Start ngrok for backend if not running
if [ "$NGROK_RUNNING" = false ]; then
    echo "  Starting ngrok tunnel for backend..."
    ngrok http $NEXUS_PORT --domain=nexi.ngrok.io --log=/tmp/ngrok-backend.log > /dev/null 2>&1 &
    NGROK_PID=$!
    sleep 3
else
    # Get existing ngrok PID
    NGROK_PID=$(pgrep -x "ngrok")
    echo "  ♻️  Reusing existing ngrok tunnel (PID: $NGROK_PID)"
fi

# Use reserved domain (no need to query API)
NGROK_URL="https://nexi.ngrok.io"

# Wait for ngrok to be ready
echo "  Waiting for ngrok tunnel to be ready..."
MAX_WAIT=10
WAITED=0
while [ $WAITED -lt $MAX_WAIT ]; do
    if curl -s "$NGROK_URL/health" > /dev/null 2>&1; then
        echo "  ✅ ngrok tunnel ready"
        echo "  🌍 Backend URL:  $NGROK_URL"
        break
    fi

    sleep 1
    WAITED=$((WAITED + 1))

    if [ $WAITED -eq $MAX_WAIT ]; then
        echo "  ⚠️  Could not connect to ngrok tunnel"
        echo "  Check ngrok dashboard: http://localhost:4040"
    fi
done

echo ""

# ============================================
# Start Frontend
# ============================================

echo "🎨 Starting Nexus frontend..."
echo ""

cd "$FRONTEND_DIR"

# Install dependencies if needed
if [ ! -d "node_modules" ]; then
    echo "  Installing npm dependencies..."
    npm install
    echo "  ✅ Dependencies installed"
    echo ""
fi

# Start frontend dev server
echo "  Starting frontend dev server..."
npm run dev > /tmp/nexus-frontend.log 2>&1 &
FRONTEND_PID=$!

# Wait for frontend to start
echo "  Waiting for frontend to start..."
MAX_WAIT=15
WAITED=0
while [ $WAITED -lt $MAX_WAIT ]; do
    if curl -s http://localhost:$FRONTEND_PORT > /dev/null 2>&1; then
        echo "  ✅ Frontend started (PID: $FRONTEND_PID)"
        break
    fi
    sleep 1
    WAITED=$((WAITED + 1))
    if [ $WAITED -eq $MAX_WAIT ]; then
        echo "  ⚠️  Frontend may still be starting..."
        echo "  Check logs: tail -f /tmp/nexus-frontend.log"
        break
    fi
done

cd "$SCRIPT_DIR"

echo ""

# ============================================
# Start ngrok for Frontend (Optional)
# ============================================

echo "🌐 Setting up ngrok tunnel for frontend..."
echo ""

# Check if ngrok is available for frontend (need to start another instance)
# Note: This requires a second ngrok process or agent
echo "  Starting ngrok tunnel for frontend..."
ngrok http $FRONTEND_PORT --domain=nexi-hub.ngrok.io --log=/tmp/ngrok-frontend.log > /dev/null 2>&1 &
NGROK_FRONTEND_PID=$!
sleep 3

# Use reserved domain for frontend
NGROK_FRONTEND_URL="https://nexi-hub.ngrok.io"

# Wait for frontend ngrok to be ready
echo "  Waiting for frontend ngrok tunnel to be ready..."
MAX_WAIT=10
WAITED=0
while [ $WAITED -lt $MAX_WAIT ]; do
    if curl -s "$NGROK_FRONTEND_URL" > /dev/null 2>&1; then
        echo "  ✅ Frontend ngrok tunnel ready"
        echo "  🌍 Frontend Public URL:  $NGROK_FRONTEND_URL"
        break
    fi

    sleep 1
    WAITED=$((WAITED + 1))

    if [ $WAITED -eq $MAX_WAIT ]; then
        echo "  ⚠️  Could not connect to frontend ngrok tunnel"
        echo "  Check ngrok dashboard: http://localhost:4040"
    fi
done

echo ""

# ============================================
# Create Demo Environment File
# ============================================

echo "📝 Creating environment configuration..."

# Remove old .demo-env if it exists
rm -f "$NEXUS_DIR/.demo-env"

# Create fresh .demo-env with all URLs, keys, and PIDs
cat > "$NEXUS_DIR/.demo-env" << EOF
# Nexus Demo Environment
# Created: $(date)
# This file contains all URLs, keys, and PIDs for the demo environment

# Frontend
export NEXUS_FRONTEND_URL="http://localhost:$FRONTEND_PORT"
export NEXUS_FRONTEND_PUBLIC_URL="$NGROK_FRONTEND_URL"
export NEXUS_FRONTEND_PID=$FRONTEND_PID
export NEXUS_FRONTEND_NGROK_PID=$NGROK_FRONTEND_PID
EOF

cat >> "$NEXUS_DIR/.demo-env" << EOF

# Admin Credentials
EOF

# Admin credentials are NOT exported to .demo-env
# API keys should be passed by the frontend/client, not hardcoded in the environment

# Add backend info
# Always use public ngrok URL if available, otherwise localhost
if [ -n "$NGROK_URL" ]; then
    NEXUS_SERVER_URL="$NGROK_URL"
else
    NEXUS_SERVER_URL="http://localhost:$NEXUS_PORT"
fi

cat >> "$NEXUS_DIR/.demo-env" << EOF

# Backend
export NEXUS_URL="http://localhost:$NEXUS_PORT"
export NEXUS_SERVER_URL="$NEXUS_SERVER_URL"
export NEXUS_PUBLIC_URL="$NGROK_URL"
export NEXUS_HEALTH_URL="http://localhost:$NEXUS_PORT/health"
export NEXUS_BACKEND_PID=$NEXUS_PID
EOF

# Add ngrok info (always enabled)
if [ -n "$NGROK_URL" ]; then
    cat >> "$NEXUS_DIR/.demo-env" << EOF

# ngrok Tunnel
export NGROK_DASHBOARD_URL="http://localhost:4040"
export NGROK_PID=$NGROK_PID
EOF
fi

# Add Docker info if sandbox mode enabled
if [ "$START_SANDBOX" = true ]; then
    cat >> "$NEXUS_DIR/.demo-env" << EOF

# Docker Sandbox
export NEXUS_SANDBOX_PROVIDER="docker"
EOF
fi

# Add database info
cat >> "$NEXUS_DIR/.demo-env" << EOF

# PostgreSQL Database
export NEXUS_DATABASE_URL="$NEXUS_DATABASE_URL"
export NEXUS_DATA_DIR="$NEXUS_DATA_DIR"

# Logs
export NEXUS_BACKEND_LOG="/tmp/nexus-backend.log"
export NEXUS_FRONTEND_LOG="/tmp/nexus-frontend.log"
EOF

if [ "$START_SANDBOX" = true ]; then
    echo 'export NGROK_LOG="/tmp/ngrok.log"' >> "$NEXUS_DIR/.demo-env"
fi

echo "  ✅ Environment file created at $NEXUS_DIR/.demo-env"
echo ""

# ============================================
# Start LangGraph Server (if --start_agent)
# ============================================

if [ "$START_AGENT" = true ]; then
    echo "🔮 Starting LangGraph server..."
    echo ""

    cd "$LANGGRAPH_DIR"

    # Source .demo-env to make NEXUS_SERVER_URL available to LangGraph
    echo "  Loading environment variables from .demo-env..."
    source "$NEXUS_DIR/.demo-env"

    # Start LangGraph dev server
    echo "  Starting LangGraph dev server (NEXUS_SERVER_URL=$NEXUS_SERVER_URL)..."
    export LANGGRAPH_PORT=$LANGGRAPH_PORT
    uv run langgraph dev --allow-blocking --port $LANGGRAPH_PORT > /tmp/langgraph.log 2>&1 &
    LANGGRAPH_PID=$!

    # Add LangGraph info to .demo-env
    cat >> "$NEXUS_DIR/.demo-env" << EOF

# LangGraph Server
export LANGGRAPH_URL="http://localhost:$LANGGRAPH_PORT"
export LANGGRAPH_DOCS_URL="http://localhost:$LANGGRAPH_PORT/docs"
export LANGGRAPH_PID=$LANGGRAPH_PID
export LANGGRAPH_LOG="/tmp/langgraph.log"
EOF

    # Wait for LangGraph to start
    echo "  Waiting for LangGraph server to start..."
    MAX_WAIT=15
    WAITED=0
    while [ $WAITED -lt $MAX_WAIT ]; do
        if curl -s http://localhost:$LANGGRAPH_PORT > /dev/null 2>&1; then
            echo "  ✅ LangGraph started (PID: $LANGGRAPH_PID)"
            break
        fi
        sleep 1
        WAITED=$((WAITED + 1))
        if [ $WAITED -eq $MAX_WAIT ]; then
            echo "  ⚠️  LangGraph may still be starting..."
            echo "  Check logs: tail -f /tmp/langgraph.log"
            break
        fi
    done

    cd "$SCRIPT_DIR"

    echo ""
fi

# ============================================
# Services Overview
# ============================================

cat << 'EOF'
╔═══════════════════════════════════════════════════════════════════╗
║                     🎉 Services Running!                         ║
╚═══════════════════════════════════════════════════════════════════╝
EOF
echo ""

# 1. Frontend (Web UI)
cat << EOF
┌───────────────────────────────────────────────────────────────────┐
│ 🎨 NEXUS FRONTEND (Web UI)                                       │
├───────────────────────────────────────────────────────────────────┤
│ Description: React-based web interface for Nexus                 │
│ Local URL:   http://localhost:$FRONTEND_PORT                          │
│ Public URL:  $NGROK_FRONTEND_URL                      │
│ Process:     PID $FRONTEND_PID (frontend) / PID $NGROK_FRONTEND_PID (ngrok)  │
│ Logs:        /tmp/nexus-frontend.log                              │
│              /tmp/ngrok-frontend.log                              │
└───────────────────────────────────────────────────────────────────┘
EOF
echo ""

# 2. Admin Credentials (always show with actual key if available)
if [ -f "$NEXUS_DIR/.nexus-admin-env" ]; then
    source "$NEXUS_DIR/.nexus-admin-env"
    if [ -n "$NEXUS_API_KEY" ]; then
        if [ "$CLEAN_CREDENTIALS" = "1" ]; then
            CRED_LABEL="ADMIN CREDENTIALS (NEW)"
        else
            CRED_LABEL="ADMIN CREDENTIALS"
        fi
cat << EOF
┌───────────────────────────────────────────────────────────────────┐
│ 🔑 $CRED_LABEL                                       │
├───────────────────────────────────────────────────────────────────┤
│ User:        admin                                                │
│ API Key:     $NEXUS_API_KEY        │
│ Saved in:    $NEXUS_DIR/.nexus-admin-env          │
│                                                                   │
│ To use this key in your shell:                                   │
│   source $NEXUS_DIR/.nexus-admin-env               │
└───────────────────────────────────────────────────────────────────┘
EOF
echo ""
    fi
fi

# 3. Nexus Backend
cat << EOF
┌───────────────────────────────────────────────────────────────────┐
│ 🔧 NEXUS BACKEND (RPC Server)                                    │
├───────────────────────────────────────────────────────────────────┤
│ Description: Core Nexus RPC server with database-backed auth     │
│ Local:       http://localhost:$NEXUS_PORT                             │
│              http://localhost:$NEXUS_PORT/health                       │
EOF

if [ -n "$NGROK_URL" ]; then
cat << EOF
│ Public:      $NGROK_URL                           │
│              $NGROK_URL/health                    │
EOF
fi

cat << EOF
│ Process:     PID $NEXUS_PID                                           │
│ Logs:        /tmp/nexus-backend.log                               │
└───────────────────────────────────────────────────────────────────┘
EOF
echo ""

# 4. ngrok Tunnel (always enabled)
if [ -n "$NGROK_URL" ]; then
cat << EOF
┌───────────────────────────────────────────────────────────────────┐
│ 🌐 NGROK TUNNEL                                                  │
├───────────────────────────────────────────────────────────────────┤
│ Description: Public HTTPS tunnel for Nexus backend              │
│ Public URL:  $NGROK_URL                           │
│ Dashboard:   http://localhost:4040                              │
│ Process:     PID $NGROK_PID                                          │
│ Logs:        /tmp/ngrok.log                                     │
└───────────────────────────────────────────────────────────────────┘
EOF
echo ""
fi

# 5. Docker Sandbox (if enabled)
if [ "$START_SANDBOX" = true ]; then
cat << EOF
┌───────────────────────────────────────────────────────────────────┐
│ 🐳 DOCKER SANDBOX                                                │
├───────────────────────────────────────────────────────────────────┤
│ Description: Local Docker-based code execution sandboxes        │
│ Provider:    docker                                              │
│ Runtime:     nexus/runtime:dev                                   │
│ Features:    • Python 3.11 • Node.js 20 • FUSE mounting         │
└───────────────────────────────────────────────────────────────────┘
EOF
echo ""
fi

# 6. LangGraph Server (if enabled)
if [ "$START_AGENT" = true ]; then
    # Determine the server URL to display
    if [ -n "$NGROK_URL" ]; then
        LG_NEXUS_URL="$NGROK_URL (public)"
    else
        LG_NEXUS_URL="http://localhost:$NEXUS_PORT (local)"
    fi
cat << EOF
┌───────────────────────────────────────────────────────────────────┐
│ 🔮 LANGGRAPH SERVER (Agent Runtime)                              │
├───────────────────────────────────────────────────────────────────┤
│ Description: FastAPI server with header-based authentication     │
│ Endpoints:   http://localhost:$LANGGRAPH_PORT                         │
│              http://localhost:$LANGGRAPH_PORT/docs (OpenAPI)           │
│              http://localhost:$LANGGRAPH_PORT/chat (POST)              │
│ Auth:        Authorization: Bearer <api-key>                     │
│ Nexus URL:   $LG_NEXUS_URL      │
│ Process:     PID $LANGGRAPH_PID                                       │
│ Logs:        /tmp/langgraph.log                                   │
└───────────────────────────────────────────────────────────────────┘
EOF
echo ""
fi

# 7. PostgreSQL Database
cat << EOF
┌───────────────────────────────────────────────────────────────────┐
│ 🗄️  POSTGRESQL DATABASE                                          │
├───────────────────────────────────────────────────────────────────┤
│ Description: PostgreSQL database for metadata and auth           │
│ Connection:  $NEXUS_DATABASE_URL      │
│ Data Dir:    $NEXUS_DATA_DIR                     │
└───────────────────────────────────────────────────────────────────┘
EOF
echo ""

# Quick Start
cat << 'EOF'
╔═══════════════════════════════════════════════════════════════════╗
║                         📚 QUICK START                           ║
╚═══════════════════════════════════════════════════════════════════╝
EOF
echo ""
echo "  Open frontend:      open http://localhost:$FRONTEND_PORT"
echo "  Open public frontend: open $NGROK_FRONTEND_URL"
echo "  Test local API:     curl http://localhost:$NEXUS_PORT/health"
if [ -n "$NGROK_URL" ]; then
    echo "  Test public API:    curl $NGROK_URL/health"
    echo "  ngrok dashboard:    open http://localhost:4040"
fi
if [ "$START_AGENT" = true ]; then
    echo "  LangGraph docs:     open http://localhost:$LANGGRAPH_PORT/docs"
fi
if [ "$START_SANDBOX" = true ]; then
    echo "  Docker sandbox:     docker ps (view containers)"
fi
echo ""
echo "  Environment file:   source $NEXUS_DIR/.demo-env"
echo ""

# Stop commands
cat << 'EOF'
╔═══════════════════════════════════════════════════════════════════╗
║                      🛑 STOP SERVICES                            ║
╚═══════════════════════════════════════════════════════════════════╝
EOF
echo ""
# Build kill command based on what's running
KILL_ALL="kill $NEXUS_PID $FRONTEND_PID"
if [ "$START_AGENT" = true ]; then
    KILL_ALL="$KILL_ALL $LANGGRAPH_PID"
fi
if [ -n "$NGROK_PID" ]; then
    KILL_ALL="$KILL_ALL $NGROK_PID"
fi
if [ -n "$NGROK_FRONTEND_PID" ]; then
    KILL_ALL="$KILL_ALL $NGROK_FRONTEND_PID"
fi

echo "  Kill all services:  $KILL_ALL"
echo "  Kill backend:       kill $NEXUS_PID"
echo "  Kill frontend:      kill $FRONTEND_PID"
if [ "$START_AGENT" = true ]; then
    echo "  Kill LangGraph:     kill $LANGGRAPH_PID"
fi
if [ -n "$NGROK_PID" ]; then
    echo "  Kill backend ngrok: kill $NGROK_PID"
fi
if [ -n "$NGROK_FRONTEND_PID" ]; then
    echo "  Kill frontend ngrok: kill $NGROK_FRONTEND_PID"
fi
echo ""

# ============================================
# Keep running and handle Ctrl+C
# ============================================

# Trap SIGINT (Ctrl+C) and SIGTERM to clean up
cleanup() {
    echo ""
    echo ""
    echo "🛑 Stopping all services..."
    echo ""

    # Kill all services
    echo "  Stopping Nexus backend (PID $NEXUS_PID)..."
    kill $NEXUS_PID 2>/dev/null || true

    echo "  Stopping frontend (PID $FRONTEND_PID)..."
    kill $FRONTEND_PID 2>/dev/null || true

    if [ "$START_AGENT" = true ] && [ -n "$LANGGRAPH_PID" ]; then
        echo "  Stopping LangGraph (PID $LANGGRAPH_PID)..."
        kill $LANGGRAPH_PID 2>/dev/null || true
    fi

    if [ -n "$NGROK_PID" ]; then
        echo "  Stopping backend ngrok (PID $NGROK_PID)..."
        kill $NGROK_PID 2>/dev/null || true
    fi

    if [ -n "$NGROK_FRONTEND_PID" ]; then
        echo "  Stopping frontend ngrok (PID $NGROK_FRONTEND_PID)..."
        kill $NGROK_FRONTEND_PID 2>/dev/null || true
    fi

    # Also kill by port to ensure cleanup
    lsof -ti:$NEXUS_PORT | xargs kill -9 2>/dev/null || true
    lsof -ti:$FRONTEND_PORT | xargs kill -9 2>/dev/null || true

    if [ "$START_AGENT" = true ]; then
        lsof -ti:$LANGGRAPH_PORT | xargs kill -9 2>/dev/null || true
    fi

    # Always cleanup ngrok
    pkill ngrok 2>/dev/null || true

    echo ""
    echo "✅ All services stopped"
    exit 0
}

# Set up trap
trap cleanup SIGINT SIGTERM

echo "Press Ctrl+C to stop all services..."
echo ""

# Wait for user interrupt (blocking)
wait

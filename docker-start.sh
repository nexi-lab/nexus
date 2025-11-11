#!/bin/bash
# docker-start.sh - Start Nexus services using Docker Compose
#
# Usage:
#   ./docker-start.sh                    # Start all services (detached)
#   ./docker-start.sh --build            # Rebuild images and start
#   ./docker-start.sh --stop             # Stop all services
#   ./docker-start.sh --restart          # Restart all services
#   ./docker-start.sh --logs             # View logs (follow mode)
#   ./docker-start.sh --status           # Check service status
#   ./docker-start.sh --clean            # Stop and remove all data (volumes)
#   ./docker-start.sh --init             # Initialize (clean + build + start)
#   ./docker-start.sh --init --skip_permission  # Initialize with permissions disabled
#   ./docker-start.sh --env=production   # Use production environment files
#
# Services:
#   - postgres:    PostgreSQL database (port 5432)
#   - nexus:       Nexus RPC server (port 8080)
#   - langgraph:   LangGraph agent server (port 2024)
#   - frontend:    React web UI (port 5173)

set -e  # Exit on error

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

COMPOSE_FILE="docker-compose.demo.yml"
ENV_MODE="local"  # Default: local development
SKIP_PERMISSIONS=false  # Default: set up permissions

# ============================================
# Banner
# ============================================

print_banner() {
cat << 'EOF'
╔═══════════════════════════════════════════╗
║   Nexus Docker Development Environment   ║
╚═══════════════════════════════════════════╝
EOF
echo ""
}

# ============================================
# Helper Functions
# ============================================

check_docker() {
    if ! command -v docker &> /dev/null; then
        echo "❌ Docker not found. Please install Docker:"
        echo "   https://docs.docker.com/get-docker/"
        exit 1
    fi

    if ! docker info > /dev/null 2>&1; then
        echo "❌ Docker is not running"
        echo "   Please start Docker Desktop or Docker daemon"
        exit 1
    fi
}

check_env_file() {
    # Determine environment files based on ENV_MODE
    case "$ENV_MODE" in
        production)
            ENV_FILE=".env.production"
            ENV_SECRETS=".env.production.secrets"
            FRONTEND_ENV_FILE="../nexus-frontend/.env.production"
            ;;
        *)
            # Local development (default)
            # Try .env.local first, fallback to .env for backwards compatibility
            if [ -f ".env.local" ]; then
                ENV_FILE=".env.local"
            else
                ENV_FILE=".env"
            fi
            ENV_SECRETS=""
            FRONTEND_ENV_FILE="../nexus-frontend/.env.local"
            ;;
    esac

    echo "🎯 Environment mode: $ENV_MODE"
    echo "   Backend config: $ENV_FILE"
    if [ -n "$ENV_SECRETS" ]; then
        echo "   Backend secrets: $ENV_SECRETS"
    fi
    echo "   Frontend config: $FRONTEND_ENV_FILE"
    echo ""

    # Check main env file
    if [ ! -f "$ENV_FILE" ]; then
        echo "⚠️  Environment file not found: $ENV_FILE"
        echo ""

        if [ "$ENV_MODE" = "production" ]; then
            echo "❌ Production environment file missing!"
            echo "   Expected: $ENV_FILE"
            exit 1
        else
            echo "Creating $ENV_FILE from .env.example..."
            if [ -f ".env.example" ]; then
                cp .env.example "$ENV_FILE"
                echo "✅ Created $ENV_FILE"
                echo ""
                echo "⚠️  IMPORTANT: Edit $ENV_FILE and add your API keys:"
                echo "   - ANTHROPIC_API_KEY (required for LangGraph)"
                echo "   - OPENAI_API_KEY (required for LangGraph)"
                echo ""
                read -p "Press Enter to continue after editing $ENV_FILE..."
            else
                echo "❌ .env.example not found"
                exit 1
            fi
        fi
    fi

    # Load main env file
    set -a  # Auto-export all variables
    source "$ENV_FILE"
    set +a

    # Load secrets file if in production mode
    if [ "$ENV_MODE" = "production" ] && [ -n "$ENV_SECRETS" ]; then
        if [ -f "$ENV_SECRETS" ]; then
            echo "🔐 Loading production secrets from $ENV_SECRETS"
            set -a
            source "$ENV_SECRETS"
            set +a
        else
            echo "⚠️  Production secrets file not found: $ENV_SECRETS"
            echo "   This is OK for testing, but required for production deployment"
        fi
    fi

    # Check for frontend env file
    if [ -f "$FRONTEND_ENV_FILE" ]; then
        echo "📦 Loading frontend config from $FRONTEND_ENV_FILE"
        set -a
        source "$FRONTEND_ENV_FILE"
        set +a
    else
        echo "ℹ️  Frontend env file not found: $FRONTEND_ENV_FILE (using defaults)"
        if [ "$ENV_MODE" != "production" ]; then
            echo "   💡 Tip: Create $FRONTEND_ENV_FILE for custom frontend config"
        fi
    fi
    echo ""
}

show_services() {
    cat << EOF
📦 Services:
   • postgres    - PostgreSQL database (port 5432)
   • nexus       - Nexus RPC server (port 8080)
   • langgraph   - LangGraph agent (port 2024)
   • frontend    - React web UI (port 5173)
EOF
    echo ""
}

# ============================================
# Commands
# ============================================

cmd_start() {
    print_banner
    check_docker
    check_env_file

    echo "🧹 Cleaning up old sandbox containers..."
    docker ps -a --filter "ancestor=nexus/runtime:latest" -q | xargs -r docker rm -f 2>/dev/null || true
    echo ""

    echo "🚀 Starting Nexus services..."
    echo ""
    show_services

    # Start services in detached mode
    docker compose -f "$COMPOSE_FILE" up -d

    echo ""
    echo "✅ Services started!"
    echo ""
    cmd_status
    show_api_key
    cmd_urls
}

cmd_build() {
    print_banner
    check_docker
    check_env_file

    echo "🧹 Cleaning up old sandbox containers..."
    docker ps -a --filter "ancestor=nexus/runtime:latest" -q | xargs -r docker rm -f 2>/dev/null || true
    echo ""

    echo "🔨 Building Docker images..."
    echo ""

    # Build images
    docker compose -f "$COMPOSE_FILE" build

    echo ""
    echo "✅ Images built successfully!"
    echo ""
    echo "Starting services..."
    docker compose -f "$COMPOSE_FILE" up -d

    echo ""
    cmd_status
    show_api_key
    cmd_urls
}

cmd_stop() {
    print_banner
    echo "🛑 Stopping Nexus services..."
    echo ""

    docker compose -f "$COMPOSE_FILE" down

    echo ""
    echo "✅ Services stopped!"
}

cmd_restart() {
    print_banner
    echo "🔄 Restarting Nexus services..."
    echo ""

    docker compose -f "$COMPOSE_FILE" restart

    echo ""
    echo "✅ Services restarted!"
    echo ""
    cmd_status
    show_api_key
    cmd_urls
}

cmd_logs() {
    check_docker

    echo "📋 Following logs (Ctrl+C to exit)..."
    echo ""

    docker compose -f "$COMPOSE_FILE" logs -f
}

cmd_status() {
    check_docker

    echo "📊 Service Status:"
    echo ""
    docker compose -f "$COMPOSE_FILE" ps
}

cmd_clean() {
    print_banner
    echo "⚠️  CLEAN MODE"
    echo ""
    echo "This will DELETE ALL data:"
    echo "  • All Docker containers"
    echo "  • All Docker volumes (PostgreSQL data, Nexus data)"
    echo "  • All Docker images"
    echo ""
    read -p "Are you sure you want to continue? (yes/no): " CONFIRM

    if [ "$CONFIRM" != "yes" ]; then
        echo ""
        echo "❌ Clean cancelled"
        exit 0
    fi

    echo ""
    echo "🧹 Cleaning up..."

    # Stop and remove containers, volumes, and images
    docker compose -f "$COMPOSE_FILE" down -v --rmi all

    echo ""
    echo "✅ Cleanup complete!"
}

cmd_init() {
    print_banner
    check_docker
    check_env_file

    echo "🔧 INITIALIZATION MODE"
    echo ""
    echo "This will:"
    echo "  1. Clean up old sandbox containers"
    echo "  2. Clean all existing data and containers"
    echo "  3. Rebuild all Docker images"
    echo "  4. Start all services fresh"
    if [ "$SKIP_PERMISSIONS" = true ]; then
        echo "  5. Skip permission setup and disable runtime permission checks"
    fi
    echo ""
    read -p "Are you sure you want to continue? (yes/no): " CONFIRM

    if [ "$CONFIRM" != "yes" ]; then
        echo ""
        echo "❌ Initialization cancelled"
        exit 0
    fi

    echo ""
    echo "🧹 Step 1/4: Cleaning up old sandbox containers..."
    docker ps -a --filter "ancestor=nexus/runtime:latest" -q | xargs -r docker rm -f 2>/dev/null || true

    echo ""
    echo "🧹 Step 2/4: Cleaning Docker Compose resources..."
    docker compose -f "$COMPOSE_FILE" down -v

    echo ""
    echo "🔨 Step 3/4: Building images..."
    docker compose -f "$COMPOSE_FILE" build

    echo ""
    echo "🚀 Step 4/4: Starting services..."
    # Export SKIP_PERMISSIONS so Docker Compose can pass it to containers
    if [ "$SKIP_PERMISSIONS" = true ]; then
        export NEXUS_SKIP_PERMISSIONS=true
        export NEXUS_ENFORCE_PERMISSIONS=false
        echo "   (Skipping permission setup and disabling runtime permission checks)"
    fi
    docker compose -f "$COMPOSE_FILE" up -d

    echo ""
    echo "✅ Initialization complete!"
    echo ""
    cmd_status
    show_api_key
    cmd_urls
}

show_api_key() {
    echo ""
    echo "🔑 Retrieving admin API key..."
    echo ""

    # Wait a moment for container to fully initialize
    sleep 2

    # Try to get API key from container
    API_KEY=$(docker exec nexus-server cat /app/data/.admin-api-key 2>/dev/null || echo "")

    if [ -z "$API_KEY" ]; then
        # Fallback: try to extract from logs
        API_KEY=$(docker logs nexus-server 2>&1 | grep "API Key:" | tail -1 | awk '{print $3}')
    fi

    if [ -n "$API_KEY" ]; then
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "ADMIN API KEY"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo ""
        echo "  User:    admin"
        echo "  API Key: ${API_KEY}"
        echo ""
        echo "  To use this key:"
        echo "    export NEXUS_API_KEY='${API_KEY}'"
        echo "    export NEXUS_URL='http://localhost:8080'"
        echo ""
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo ""
    else
        echo "⚠️  Could not retrieve API key from container"
        echo "   Try: docker logs nexus-server | grep 'API Key:'"
        echo ""
    fi
}

cmd_urls() {
    cat << 'EOF'
╔═══════════════════════════════════════════════════════════════════╗
║                      🌐 Access URLs                              ║
╚═══════════════════════════════════════════════════════════════════╝

  🎨 Frontend:        http://localhost:5173
  🔧 Nexus API:       http://localhost:8080
  🔮 LangGraph:       http://localhost:2024
  🗄️  PostgreSQL:     localhost:5432

  📊 Health Checks:
     • Nexus:         curl http://localhost:8080/health
     • Frontend:      curl http://localhost:5173/health
     • LangGraph:     curl http://localhost:2024/health

╔═══════════════════════════════════════════════════════════════════╗
║                      📚 Useful Commands                          ║
╚═══════════════════════════════════════════════════════════════════╝

  View logs:         ./docker-start.sh --logs
  Check status:      ./docker-start.sh --status
  Restart:           ./docker-start.sh --restart
  Stop:              ./docker-start.sh --stop

  Docker commands:
    All logs:        docker compose -f docker-compose.demo.yml logs -f
    Nexus logs:      docker logs -f nexus-server
    Frontend logs:   docker logs -f nexus-frontend
    LangGraph logs:  docker logs -f nexus-langgraph

  Shell access:
    Nexus:           docker exec -it nexus-server sh
    PostgreSQL:      docker exec -it nexus-postgres psql -U postgres -d nexus

EOF
}

# ============================================
# Main
# ============================================

# Parse flags and filter out non-command arguments
COMMAND=""
while [ $# -gt 0 ]; do
    case $1 in
        --env=*)
            ENV_MODE="${1#*=}"
            shift
            ;;
        --skip_permission)
            SKIP_PERMISSIONS=true
            shift
            ;;
        --*)
            # This is a command argument
            if [ -z "$COMMAND" ]; then
                COMMAND="$1"
            fi
            shift
            ;;
        *)
            shift
            ;;
    esac
done

# Parse command arguments
if [ -z "$COMMAND" ]; then
    cmd_start
    exit 0
fi

case "$COMMAND" in
    --start)
        cmd_start
        ;;
    --build)
        cmd_build
        ;;
    --stop)
        cmd_stop
        ;;
    --restart)
        cmd_restart
        ;;
    --logs)
        cmd_logs
        ;;
    --status)
        print_banner
        cmd_status
        show_api_key
        cmd_urls
        ;;
    --clean)
        cmd_clean
        ;;
    --init)
        cmd_init
        ;;
    --help|-h)
        print_banner
        echo "Usage: $0 [OPTION] [--env=MODE] [--skip_permission]"
        echo ""
        echo "Options:"
        echo "  (none)          Start all services (detached)"
        echo "  --build         Rebuild images and start"
        echo "  --stop          Stop all services"
        echo "  --restart       Restart all services"
        echo "  --logs          View logs (follow mode)"
        echo "  --status        Check service status"
        echo "  --clean         Stop and remove all data (volumes)"
        echo "  --init          Initialize (clean + build + start)"
        echo "  --env=MODE      Set environment mode (local|production)"
        echo "  --skip_permission  Skip permission setup and disable runtime checks (use with --init)"
        echo "  --help, -h      Show this help message"
        echo ""
        echo "Environment Modes:"
        echo "  local           Use .env.local and .env (default)"
        echo "  production      Use .env.production and .env.production.secrets"
        echo ""
        echo "Examples:"
        echo "  ./docker-start.sh                    # Start with local env"
        echo "  ./docker-start.sh --env=production   # Start with production env"
        echo "  ./docker-start.sh --build --env=production  # Rebuild with production env"
        echo "  ./docker-start.sh --init --skip_permission  # Initialize with permissions disabled"
        echo ""
        show_services
        ;;
    *)
        echo "❌ Unknown option: $1"
        echo "Run '$0 --help' for usage information"
        exit 1
        ;;
esac

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
ENV_FILE=".env"

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
    if [ ! -f "$ENV_FILE" ]; then
        echo "⚠️  Environment file not found: $ENV_FILE"
        echo ""
        echo "Creating .env from .env.example..."
        if [ -f ".env.example" ]; then
            cp .env.example .env
            echo "✅ Created .env file"
            echo ""
            echo "⚠️  IMPORTANT: Edit .env and add your API keys:"
            echo "   - ANTHROPIC_API_KEY (required for LangGraph)"
            echo "   - OPENAI_API_KEY (required for LangGraph)"
            echo ""
            read -p "Press Enter to continue after editing .env..."
        else
            echo "❌ .env.example not found"
            exit 1
        fi
    fi
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
    echo ""
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
    echo ""
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
    echo "🔧 INITIALIZATION MODE"
    echo ""
    echo "This will:"
    echo "  1. Clean up old sandbox containers"
    echo "  2. Clean all existing data and containers"
    echo "  3. Rebuild all Docker images"
    echo "  4. Start all services fresh"
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
    docker compose -f "$COMPOSE_FILE" up -d

    echo ""
    echo "✅ Initialization complete!"
    echo ""
    cmd_status
    echo ""
    cmd_urls
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

# Parse arguments
if [ $# -eq 0 ]; then
    cmd_start
    exit 0
fi

case "$1" in
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
        echo ""
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
        echo "Usage: $0 [OPTION]"
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
        echo "  --help, -h      Show this help message"
        echo ""
        show_services
        ;;
    *)
        echo "❌ Unknown option: $1"
        echo "Run '$0 --help' for usage information"
        exit 1
        ;;
esac

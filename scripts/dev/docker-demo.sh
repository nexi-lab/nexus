#!/bin/bash
# docker-demo.sh - Start Nexus services using Docker Compose
#
# Usage:
#   ./docker-demo.sh                    # Start all services (detached)
#   ./docker-demo.sh --build            # Rebuild images and start
#   ./docker-demo.sh --stop             # Stop all services
#   ./docker-demo.sh --restart          # Restart all services
#   ./docker-demo.sh --logs             # View logs (follow mode)
#   ./docker-demo.sh --status           # Check service status
#   ./docker-demo.sh --clean            # Stop and remove all data (volumes)
#   ./docker-demo.sh --init             # Initialize (clean + build + start)
#   ./docker-demo.sh --init --skip_permission  # Initialize with permissions disabled
#   ./docker-demo.sh --init --yes       # Initialize without confirmation (CI)
#   ./docker-demo.sh --env=production   # Use production environment files
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
SKIP_CONFIRM=false  # Default: ask for confirmation on destructive operations

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
            ;;
        *)
            # Local development (default)
            # Try .env.local first, then .env.example
            if [ -f ".env.local" ]; then
                ENV_FILE=".env.local"
            elif [ -f ".env.example" ]; then
                ENV_FILE=".env.example"
                echo "⚠️  Using .env.example (no .env.local found)"
                echo "   💡 Tip: Create .env.local for your personal config"
                echo "   Run: cp .env.example .env.local"
                echo ""
            else
                ENV_FILE=".env.local"
            fi
            ENV_SECRETS=""
            ;;
    esac

    echo "🎯 Environment mode: $ENV_MODE"
    echo "   Config file: $ENV_FILE"
    if [ -n "$ENV_SECRETS" ]; then
        echo "   Secrets file: $ENV_SECRETS"
    fi
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
            echo "❌ No environment file found!"
            echo ""
            echo "To get started, create .env.local:"
            echo "   cp .env.example .env.local"
            echo ""
            echo "Then edit .env.local and add your API keys:"
            echo "   - ANTHROPIC_API_KEY (required for LangGraph)"
            echo "   - OPENAI_API_KEY (optional, for LangGraph)"
            echo ""
            echo "Or you can edit .env.example directly and the script will use it."
            exit 1
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
    echo ""
}

check_gcs_credentials() {
    echo "🔍 Checking for GCS credentials..."

    # Priority order for finding credentials:
    # 1. Existing ./gcs-credentials.json (check validity)
    # 2. GCS_CREDENTIALS_PATH environment variable (if set)
    # 3. ~/.config/gcloud/application_default_credentials.json (gcloud default)

    # Check if gcs-credentials.json exists (should be a service account key)
    if [ -f "./gcs-credentials.json" ]; then
        echo "✅ Found GCS credentials at: ./gcs-credentials.json"
        # Verify it's a service account key (not OAuth user credentials)
        if grep -q '"type": "service_account"' ./gcs-credentials.json 2>/dev/null; then
            echo "   ✓ Valid service account key detected"
        else
            echo "   ⚠️  Warning: Not a service account key (found OAuth user credentials)"
            echo "   Please replace with a service account key for better reliability"
        fi
    elif [ -n "$GCS_CREDENTIALS_PATH" ] && [ -f "$GCS_CREDENTIALS_PATH" ]; then
        echo "✅ Found GCS credentials at: $GCS_CREDENTIALS_PATH (from GCS_CREDENTIALS_PATH)"
        # Copy to local file for Docker mount
        cp "$GCS_CREDENTIALS_PATH" ./gcs-credentials.json
        echo "   Copied to ./gcs-credentials.json for Docker mount"
    elif [ -f "$HOME/.config/gcloud/application_default_credentials.json" ]; then
        echo "✅ Found GCS credentials at: ~/.config/gcloud/application_default_credentials.json"
        # Copy gcloud credentials to local file for Docker mount
        cp "$HOME/.config/gcloud/application_default_credentials.json" ./gcs-credentials.json
        echo "   Copied to ./gcs-credentials.json for Docker mount"
    else
        echo "⚠️  No GCS credentials found - GCS mounts will not work"
        echo "   Please create a service account key and save to ./gcs-credentials.json"
        echo "   Continuing without GCS support..."
        # Create empty placeholder to prevent mount errors
        touch ./gcs-credentials.json
    fi
    export GCS_CREDENTIALS_PATH="./gcs-credentials.json"
    echo ""
}

check_aws_credentials() {
    echo "🔍 Checking for AWS credentials..."

    # Priority order for finding credentials:
    # 1. AWS_CREDENTIALS_PATH environment variable (if set)
    # 2. ./aws-credentials (local file)
    # 3. ~/.aws/credentials (AWS default)

    if [ -n "$AWS_CREDENTIALS_PATH" ] && [ -f "$AWS_CREDENTIALS_PATH" ]; then
        echo "✅ Found AWS credentials at: $AWS_CREDENTIALS_PATH (from AWS_CREDENTIALS_PATH)"
    elif [ -f "./aws-credentials" ]; then
        echo "✅ Found AWS credentials at: ./aws-credentials"
        export AWS_CREDENTIALS_PATH="./aws-credentials"
    elif [ -f "$HOME/.aws/credentials" ]; then
        echo "✅ Found AWS credentials at: ~/.aws/credentials"
        export AWS_CREDENTIALS_PATH="$HOME/.aws/credentials"
    else
        echo "⚠️  No AWS credentials found - S3 mounts will not work"
        echo "   To set up: aws configure"
        echo "   Continuing without S3 support..."
        # Create empty placeholder to prevent mount errors
        touch ./aws-credentials
        export AWS_CREDENTIALS_PATH="./aws-credentials"
    fi

    # Also check for AWS config
    if [ -n "$AWS_CONFIG_PATH" ] && [ -f "$AWS_CONFIG_PATH" ]; then
        echo "✅ Found AWS config at: $AWS_CONFIG_PATH"
    elif [ -f "./aws-config" ]; then
        echo "✅ Found AWS config at: ./aws-config"
        export AWS_CONFIG_PATH="./aws-config"
    elif [ -f "$HOME/.aws/config" ]; then
        echo "✅ Found AWS config at: ~/.aws/config"
        export AWS_CONFIG_PATH="$HOME/.aws/config"
    else
        # Create empty placeholder to prevent mount errors
        touch ./aws-config
        export AWS_CONFIG_PATH="./aws-config"
    fi
    echo ""
}

check_frontend_repo() {
    echo "🔍 Checking for nexus-frontend repository..."

    FRONTEND_DIR="../nexus-frontend"

    if [ -d "$FRONTEND_DIR" ]; then
        echo "✅ Found nexus-frontend at: $FRONTEND_DIR"
    else
        echo "⚠️  nexus-frontend not found at: $FRONTEND_DIR"
        echo ""
        echo "Cloning nexus-frontend repository..."

        # Clone the repository
        git clone https://github.com/nexi-lab/nexus-frontend.git "$FRONTEND_DIR"

        if [ $? -eq 0 ]; then
            echo "✅ Successfully cloned nexus-frontend"
        else
            echo "❌ Failed to clone nexus-frontend"
            echo "   Please manually clone: git clone https://github.com/nexi-lab/nexus-frontend.git $FRONTEND_DIR"
            exit 1
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

ensure_skills_available() {
    echo "📦 Ensuring default skills are available..."

    # Create nexus-data directory if it doesn't exist
    mkdir -p "./nexus-data/skills"

    # Source skills directory (from nexus repo)
    SKILLS_SOURCE="./data/skills"
    SKILLS_TARGET="./nexus-data/skills"

    # Check if skills exist in source
    if [ -d "$SKILLS_SOURCE" ] && [ "$(ls -A $SKILLS_SOURCE/*.skill 2>/dev/null | wc -l)" -gt 0 ]; then
        # Copy skills to target if they don't exist there
        if [ "$(ls -A $SKILLS_TARGET/*.skill 2>/dev/null | wc -l)" -eq 0 ]; then
            echo "  Copying skills from $SKILLS_SOURCE to $SKILLS_TARGET..."
            cp -r "$SKILLS_SOURCE"/*.skill "$SKILLS_TARGET/" 2>/dev/null || true
            if [ $? -eq 0 ]; then
                echo "  ✓ Skills copied successfully"
            else
                echo "  ⚠️  Failed to copy some skills (this is OK if they'll be created later)"
            fi
        else
            echo "  ✓ Skills already exist in nexus-data/skills/"
        fi
    else
        echo "  ⚠️  Skills not found in $SKILLS_SOURCE"
        echo "  This is OK - skills will be created by zip_default_skills.py if needed"
    fi
    echo ""
}

run_provisioning() {
    echo "📦 Running provisioning inside nexus-server..."

    # Get the admin API key from the container (file first, then logs fallback)
    local API_KEY=""
    API_KEY=$(docker exec nexus-server cat /app/data/.admin-api-key 2>/dev/null || true)
    if [ -z "$API_KEY" ]; then
        API_KEY=$(docker logs nexus-server 2>&1 | grep "API Key:" | tail -1 | awk '{print $3}')
    fi

    if [ -z "$API_KEY" ]; then
        echo "⚠️  Could not retrieve admin API key; skipping provisioning"
        return
    fi

    # Run provisioning in embedded mode (no NEXUS_URL) so it talks directly to DB/files
    docker exec \
        -e NEXUS_API_KEY="$API_KEY" \
        -e NEXUS_DATABASE_URL="${NEXUS_DATABASE_URL:-postgresql://postgres:nexus@postgres:5432/nexus}" \
        -e NEXUS_DATA_DIR="/app/data" \
        nexus-server sh -c "unset NEXUS_URL && cd /app && python3 scripts/provision_namespace.py --tenant default" \
        && echo "✅ Provisioning completed" \
        || echo "⚠️  Provisioning encountered errors (see container logs)"
}

clean_all_data() {
    # Utility function to clean all Nexus-related Docker resources
    # Args:
    #   $1: Whether to remove images (default: false)
    #   $2: Step prefix for logging (e.g., "Step 1/7:" or "Step 1/5:")

    local REMOVE_IMAGES="${1:-false}"
    local STEP_PREFIX="${2:-}"

    if [ -n "$STEP_PREFIX" ]; then
        STEP_PREFIX="$STEP_PREFIX "
    fi

    # Step 1: Remove sandbox containers
    echo "${STEP_PREFIX}Removing sandbox containers..."
    docker ps -a --filter "ancestor=nexus-sandbox:latest" -q | xargs -r docker rm -f 2>/dev/null || true

    # Step 2: Stop and remove all nexus-related containers (with graceful shutdown)
    echo "${STEP_PREFIX}Removing all nexus-related containers..."
    docker ps -a --filter "name=nexus" -q | xargs -r docker stop --timeout 30 2>/dev/null || true
    docker ps -a --filter "name=nexus" -q | xargs -r docker rm 2>/dev/null || true

    # Step 3: Stop and remove containers via docker-compose (this also removes volumes)
    echo "${STEP_PREFIX}Stopping docker-compose services..."
    docker compose -f "$COMPOSE_FILE" down -v --timeout 30 2>/dev/null || true

    # Step 4: Remove all volumes explicitly (including those with project prefixes)
    echo "${STEP_PREFIX}Removing all volumes (including PostgreSQL data)..."
    # Remove volumes by exact name
    docker volume rm postgres-data 2>/dev/null || true
    docker volume rm nexus-data 2>/dev/null || true
    # Remove volumes with project prefix (docker-compose may prefix volumes with project name)
    # Common patterns: nexus_postgres-data, demo_postgres-data, etc.
    # This ensures we catch volumes even if docker-compose used a project prefix
    docker volume ls -q | grep -E "(postgres-data|nexus-data)" | xargs -r docker volume rm 2>/dev/null || true

    # Step 5: Remove all nexus-related images (if requested)
    if [ "$REMOVE_IMAGES" = "true" ]; then
        echo "${STEP_PREFIX}Removing nexus-related images..."
        docker images -q --filter "reference=nexus-server:*" | xargs -r docker rmi -f 2>/dev/null || true
        docker images -q --filter "reference=nexus-langgraph:*" | xargs -r docker rmi -f 2>/dev/null || true
        docker images -q --filter "reference=nexus-frontend:*" | xargs -r docker rmi -f 2>/dev/null || true
        docker images -q --filter "reference=nexus-sandbox:*" | xargs -r docker rmi -f 2>/dev/null || true
    fi
}

# ============================================
# Commands
# ============================================

cmd_start() {
    print_banner
    check_docker
    check_env_file
    check_gcs_credentials
    check_aws_credentials
    check_frontend_repo

    echo "🧹 Cleaning up old sandbox containers..."
    docker ps -a --filter "ancestor=nexus-sandbox:latest" -q | xargs -r docker rm -f 2>/dev/null || true
    echo ""

    echo "🧹 Stopping and removing all existing Nexus containers..."
    # Stop and remove all containers with 'nexus' in the name (including manually started ones)
    # Use --timeout 30 to allow graceful shutdown (especially for LangGraph checkpoint saving)
    docker ps -a --filter "name=nexus" -q | xargs -r docker stop --timeout 30 2>/dev/null || true
    docker ps -a --filter "name=nexus" -q | xargs -r docker rm 2>/dev/null || true

    # Also use docker-compose down to clean up networks and volumes
    docker compose -f "$COMPOSE_FILE" down --timeout 30
    echo ""

    echo "🚀 Starting Nexus services..."
    echo ""
    show_services

    # Ensure skills are available in nexus-data directory
    ensure_skills_available

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
    check_gcs_credentials
    check_aws_credentials
    check_frontend_repo

    echo "🔨 Building Docker images..."
    echo ""

    # Build base runtime image first
    echo "🔨 Building base runtime image for sandboxes..."
    ./docker/build.sh

    echo ""
    echo "🔨 Building template images from config..."
    # Use uv if available, otherwise skip template building
    if command -v uv &> /dev/null; then
        uv run python docker/build-templates.py
    else
        echo "⚠️  uv not found - skipping template image builds"
        echo "   Template images will be built on-demand when first used"
        echo "   To enable pre-building, install uv: curl -LsSf https://astral.sh/uv/install.sh | sh"
    fi

    echo ""
    echo "🔨 Building service images..."
    docker compose -f "$COMPOSE_FILE" build

    echo ""
    echo "✅ All images built successfully!"
    echo ""

    # Only stop containers after successful build
    echo "🧹 Cleaning up old sandbox containers..."
    docker ps -a --filter "ancestor=nexus-sandbox:latest" -q | xargs -r docker rm -f 2>/dev/null || true
    echo ""

    echo "🧹 Stopping and removing all existing Nexus containers..."
    # Stop and remove all containers with 'nexus' in the name (including manually started ones)
    # Use --timeout 30 to allow graceful shutdown (especially for LangGraph checkpoint saving)
    docker ps -a --filter "name=nexus" -q | xargs -r docker stop --timeout 30 2>/dev/null || true
    docker ps -a --filter "name=nexus" -q | xargs -r docker rm 2>/dev/null || true

    # Also use docker-compose down to clean up networks and volumes
    docker compose -f "$COMPOSE_FILE" down --timeout 30
    echo ""

    echo "🚀 Starting services with new images..."

    # Ensure skills are available in nexus-data directory
    ensure_skills_available

    docker compose -f "$COMPOSE_FILE" up -d

    echo ""
    cmd_status
    show_api_key
    cmd_urls
}

cmd_stop() {
    print_banner
    echo "🛑 Stopping Nexus services..."
    echo "   (Using 30s timeout for graceful shutdown - saves LangGraph checkpoints)"
    echo ""

    docker compose -f "$COMPOSE_FILE" down --timeout 30

    echo ""
    echo "✅ Services stopped gracefully!"
}

cmd_restart() {
    print_banner
    echo "🔄 Restarting Nexus services..."
    echo "   (Using 30s timeout for graceful shutdown - saves LangGraph checkpoints)"
    echo ""

    docker compose -f "$COMPOSE_FILE" restart --timeout 30

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
    echo "  • All Docker containers (including sandbox containers)"
    echo "  • All Docker volumes (PostgreSQL data, Nexus data)"
    echo "  • All Docker images (nexus-server, nexus-langgraph, nexus-frontend)"
    echo "  • All Docker networks"
    echo ""

    if [ "$SKIP_CONFIRM" = false ]; then
        read -p "Are you sure you want to continue? [y/N] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo ""
            echo "❌ Clean cancelled"
            exit 0
        fi
    else
        echo "⚡ Skipping confirmation (--yes flag provided)"
    fi

    echo ""
    echo "🧹 Cleaning up..."
    clean_all_data "true" "  Step"
    echo ""
    echo "✅ Cleanup complete!"
}

cmd_init() {
    print_banner
    check_docker
    check_env_file
    check_gcs_credentials
    check_frontend_repo

    echo "🔧 INITIALIZATION MODE"
    echo ""
    echo "This will:"
    echo "  1. Clean all data (containers, volumes, sandboxes)"
    echo "  2. Build base runtime image for sandboxes"
    echo "  3. Build all template images from config (ml-heavy, web-dev, etc.)"
    echo "  4. Rebuild all service Docker images"
    echo "  5. Start all services fresh"
    if [ "$SKIP_PERMISSIONS" = true ]; then
        echo "  (Skipping permission setup and disabling runtime permission checks)"
    fi
    echo ""

    if [ "$SKIP_CONFIRM" = false ]; then
        read -p "Are you sure you want to continue? [y/N] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo ""
            echo "❌ Initialization cancelled"
            exit 0
        fi
    else
        echo "⚡ Skipping confirmation (--yes flag provided)"
    fi

    echo ""
    echo "🧹 Step 1/5: Cleaning all data..."
    clean_all_data "false" ""
    echo ""

    echo ""
    echo "🔨 Step 2/5: Building base runtime image for sandboxes..."
    ./docker/build.sh

    echo ""
    echo "🔨 Step 3/5: Building template images from config..."
    # Use uv if available, otherwise skip template building
    if command -v uv &> /dev/null; then
        uv run python docker/build-templates.py
    else
        echo "⚠️  uv not found - skipping template image builds"
        echo "   Template images will be built on-demand when first used"
        echo "   To enable pre-building, install uv: curl -LsSf https://astral.sh/uv/install.sh | sh"
    fi

    echo ""
    echo "🔨 Step 4/5: Building service images..."
    docker compose -f "$COMPOSE_FILE" build

    echo ""
    echo "🚀 Step 5/5: Starting services..."

    # Ensure skills are available in nexus-data directory
    ensure_skills_available

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
    run_provisioning
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
     • LangGraph:     curl http://localhost:2024/ok

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
        --yes|-y)
            SKIP_CONFIRM=true
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
        echo "Usage: $0 [OPTION] [--env=MODE] [--skip_permission] [--yes]"
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
        echo "  --yes, -y       Skip confirmation prompts (for CI/automation)"
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
        echo "  ./docker-start.sh --init --yes       # Initialize without confirmation (CI)"
        echo ""
        show_services
        ;;
    *)
        echo "❌ Unknown option: $1"
        echo "Run '$0 --help' for usage information"
        exit 1
        ;;
esac

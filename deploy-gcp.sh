#!/usr/bin/env bash
#
# deploy-gcp.sh - Deploy Nexus RPC Server to Google Cloud Platform
#
# This script provisions a GCP VM instance and deploys the Nexus server.
#
# Prerequisites:
#   - gcloud CLI installed and authenticated
#   - GCP project created with billing enabled
#   - Compute Engine API enabled
#
# Usage:
#   ./deploy-gcp.sh [OPTIONS]
#
# Options:
#   --project-id PROJECT_ID      GCP project ID (required)
#   --instance-name NAME         VM instance name (default: nexus-server)
#   --zone ZONE                  GCP zone (default: us-west1-a)
#   --machine-type TYPE          Machine type (default: e2-medium)
#   --disk-size SIZE             Boot disk size in GB (default: 50)
#   --api-key KEY                Nexus API key for authentication (optional)
#   --gcs-bucket BUCKET          GCS bucket for backend storage (optional)
#   --data-dir DIR               Data directory on VM (default: /var/lib/nexus)
#   --port PORT                  Server port (default: 8080)
#   --deploy-only                Skip VM creation, only deploy code
#   --help                       Show this help message

set -euo pipefail

# Default values
INSTANCE_NAME="nexus-server"
ZONE="us-west1-a"
MACHINE_TYPE="e2-medium"
DISK_SIZE="50"
PORT="8080"
DATA_DIR="/var/lib/nexus"
PROJECT_ID=""
API_KEY=""
GCS_BUCKET=""
DEPLOY_ONLY=false

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Print colored output
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Show usage
show_help() {
    grep '^#' "$0" | grep -v '#!/usr/bin/env' | sed 's/^# //' | sed 's/^#//'
    exit 0
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --project-id)
            PROJECT_ID="$2"
            shift 2
            ;;
        --instance-name)
            INSTANCE_NAME="$2"
            shift 2
            ;;
        --zone)
            ZONE="$2"
            shift 2
            ;;
        --machine-type)
            MACHINE_TYPE="$2"
            shift 2
            ;;
        --disk-size)
            DISK_SIZE="$2"
            shift 2
            ;;
        --api-key)
            API_KEY="$2"
            shift 2
            ;;
        --gcs-bucket)
            GCS_BUCKET="$2"
            shift 2
            ;;
        --data-dir)
            DATA_DIR="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --deploy-only)
            DEPLOY_ONLY=true
            shift
            ;;
        --help|-h)
            show_help
            ;;
        *)
            log_error "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Validate required arguments
if [[ -z "$PROJECT_ID" ]]; then
    log_error "Project ID is required. Use --project-id PROJECT_ID"
    exit 1
fi

# Check gcloud is installed
if ! command -v gcloud &> /dev/null; then
    log_error "gcloud CLI is not installed. Install it from https://cloud.google.com/sdk/docs/install"
    exit 1
fi

# Set project
log_info "Setting GCP project to: $PROJECT_ID"
gcloud config set project "$PROJECT_ID"

# Create VM instance
if [[ "$DEPLOY_ONLY" == false ]]; then
    log_info "Creating VM instance: $INSTANCE_NAME"

    # Check if instance already exists
    if gcloud compute instances describe "$INSTANCE_NAME" --zone="$ZONE" &> /dev/null; then
        log_warn "Instance $INSTANCE_NAME already exists in zone $ZONE"
        read -p "Do you want to delete and recreate it? (y/N) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            log_info "Deleting existing instance..."
            gcloud compute instances delete "$INSTANCE_NAME" --zone="$ZONE" --quiet
        else
            log_info "Skipping instance creation. Will deploy to existing instance."
        fi
    fi

    # Create instance if it doesn't exist
    if ! gcloud compute instances describe "$INSTANCE_NAME" --zone="$ZONE" &> /dev/null; then
        log_info "Creating new instance with:"
        log_info "  - Machine type: $MACHINE_TYPE"
        log_info "  - Disk size: ${DISK_SIZE}GB"
        log_info "  - Zone: $ZONE"

        gcloud compute instances create "$INSTANCE_NAME" \
            --zone="$ZONE" \
            --machine-type="$MACHINE_TYPE" \
            --boot-disk-size="${DISK_SIZE}GB" \
            --boot-disk-type=pd-standard \
            --image-family=ubuntu-2204-lts \
            --image-project=ubuntu-os-cloud \
            --metadata=enable-oslogin=TRUE \
            --tags=nexus-server,http-server \
            --scopes=cloud-platform

        log_success "VM instance created successfully"

        # Wait for instance to be ready
        log_info "Waiting for instance to be ready..."
        sleep 10
    fi

    # Create firewall rule for the server port
    log_info "Setting up firewall rules for port $PORT..."
    if ! gcloud compute firewall-rules describe "allow-nexus-${PORT}" &> /dev/null; then
        gcloud compute firewall-rules create "allow-nexus-${PORT}" \
            --allow="tcp:${PORT}" \
            --target-tags=nexus-server \
            --description="Allow Nexus RPC server traffic on port ${PORT}"
        log_success "Firewall rule created"
    else
        log_info "Firewall rule already exists"
    fi
fi

# Get instance external IP
log_info "Getting instance external IP..."
EXTERNAL_IP=$(gcloud compute instances describe "$INSTANCE_NAME" \
    --zone="$ZONE" \
    --format='get(networkInterfaces[0].accessConfigs[0].natIP)')

if [[ -z "$EXTERNAL_IP" ]]; then
    log_error "Failed to get external IP for instance"
    exit 1
fi

log_success "Instance external IP: $EXTERNAL_IP"

# Create deployment script
log_info "Creating deployment script..."
DEPLOY_SCRIPT=$(cat <<'DEPLOY_SCRIPT_EOF'
#!/usr/bin/env bash
set -euo pipefail

echo "[INFO] Starting Nexus server deployment..."

# Update system
echo "[INFO] Updating system packages..."
sudo apt-get update -qq
sudo apt-get upgrade -y -qq

# Install dependencies
echo "[INFO] Installing dependencies..."
sudo apt-get install -y -qq \
    python3.11 \
    python3.11-venv \
    python3-pip \
    git \
    curl

# Create nexus user if not exists
if ! id -u nexus &> /dev/null; then
    echo "[INFO] Creating nexus user..."
    sudo useradd -r -s /bin/bash -d /opt/nexus -m nexus
fi

# Create data directory
echo "[INFO] Creating data directory: DATA_DIR_PLACEHOLDER"
sudo mkdir -p DATA_DIR_PLACEHOLDER
sudo chown nexus:nexus DATA_DIR_PLACEHOLDER

# Clone/update repository
if [ -d "/opt/nexus/repo" ]; then
    echo "[INFO] Updating existing repository..."
    cd /opt/nexus/repo
    sudo -u nexus git fetch origin
    sudo -u nexus git reset --hard origin/main
else
    echo "[INFO] Cloning repository..."
    sudo -u nexus git clone https://github.com/nexi-lab/nexus.git /opt/nexus/repo
    cd /opt/nexus/repo
fi

# Install uv for faster installs
if ! command -v uv &> /dev/null; then
    echo "[INFO] Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.cargo/bin:$PATH"
fi

# Create virtual environment
echo "[INFO] Setting up virtual environment..."
cd /opt/nexus/repo
sudo -u nexus python3.11 -m venv .venv
sudo -u nexus .venv/bin/pip install --upgrade pip

# Install Nexus
echo "[INFO] Installing Nexus..."
sudo -u nexus .venv/bin/pip install -e .

# Create systemd service
echo "[INFO] Creating systemd service..."
sudo tee /etc/systemd/system/nexus-server.service > /dev/null <<'SERVICE_EOF'
[Unit]
Description=Nexus RPC Server
After=network.target

[Service]
Type=simple
User=nexus
Group=nexus
WorkingDirectory=/opt/nexus/repo
Environment="PATH=/opt/nexus/repo/.venv/bin:/usr/local/bin:/usr/bin:/bin"
NEXUS_ENV_PLACEHOLDER
ExecStart=/opt/nexus/repo/.venv/bin/python -m nexus.cli serve \
    --host 0.0.0.0 \
    --port PORT_PLACEHOLDER \
    --data-dir DATA_DIR_PLACEHOLDER \
    NEXUS_ARGS_PLACEHOLDER
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=nexus-server

[Install]
WantedBy=multi-user.target
SERVICE_EOF

# Reload systemd and start service
echo "[INFO] Starting Nexus server..."
sudo systemctl daemon-reload
sudo systemctl enable nexus-server
sudo systemctl restart nexus-server

# Wait for server to start
echo "[INFO] Waiting for server to start..."
sleep 5

# Check service status
if sudo systemctl is-active --quiet nexus-server; then
    echo "[SUCCESS] Nexus server is running"
    sudo systemctl status nexus-server --no-pager
else
    echo "[ERROR] Nexus server failed to start"
    sudo journalctl -u nexus-server -n 50 --no-pager
    exit 1
fi

echo "[SUCCESS] Deployment complete!"
DEPLOY_SCRIPT_EOF
)

# Replace placeholders in deployment script
DEPLOY_SCRIPT="${DEPLOY_SCRIPT//DATA_DIR_PLACEHOLDER/$DATA_DIR}"
DEPLOY_SCRIPT="${DEPLOY_SCRIPT//PORT_PLACEHOLDER/$PORT}"

# Build Nexus arguments
NEXUS_ARGS=""
if [[ -n "$API_KEY" ]]; then
    NEXUS_ARGS="$NEXUS_ARGS--api-key \$NEXUS_API_KEY "
fi

DEPLOY_SCRIPT="${DEPLOY_SCRIPT//NEXUS_ARGS_PLACEHOLDER/$NEXUS_ARGS}"

# Build environment variables
NEXUS_ENV=""
if [[ -n "$API_KEY" ]]; then
    NEXUS_ENV="${NEXUS_ENV}Environment=\"NEXUS_API_KEY=$API_KEY\"\n"
fi
if [[ -n "$GCS_BUCKET" ]]; then
    NEXUS_ENV="${NEXUS_ENV}Environment=\"NEXUS_BACKEND=gcs\"\n"
    NEXUS_ENV="${NEXUS_ENV}Environment=\"NEXUS_GCS_BUCKET_NAME=$GCS_BUCKET\"\n"
    NEXUS_ENV="${NEXUS_ENV}Environment=\"NEXUS_GCS_PROJECT_ID=$PROJECT_ID\"\n"
fi

DEPLOY_SCRIPT="${DEPLOY_SCRIPT//NEXUS_ENV_PLACEHOLDER/$NEXUS_ENV}"

# Copy deployment script to VM and execute
log_info "Deploying to VM..."
echo "$DEPLOY_SCRIPT" | gcloud compute ssh "$INSTANCE_NAME" \
    --zone="$ZONE" \
    --command="cat > /tmp/deploy-nexus.sh && chmod +x /tmp/deploy-nexus.sh && /tmp/deploy-nexus.sh"

# Test server
log_info "Testing server health..."
sleep 3

if curl -sf "http://${EXTERNAL_IP}:${PORT}/health" > /dev/null; then
    log_success "Server is healthy!"
else
    log_warn "Health check failed. Server may still be starting..."
fi

# Print summary
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log_success "Nexus Server Deployment Complete!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Instance Details:"
echo "  - Name: $INSTANCE_NAME"
echo "  - Zone: $ZONE"
echo "  - External IP: $EXTERNAL_IP"
echo "  - Server URL: http://${EXTERNAL_IP}:${PORT}"
echo ""
echo "Testing:"
echo "  curl http://${EXTERNAL_IP}:${PORT}/health"
echo ""
echo "Management:"
echo "  # SSH into instance"
echo "  gcloud compute ssh $INSTANCE_NAME --zone=$ZONE"
echo ""
echo "  # View logs"
echo "  gcloud compute ssh $INSTANCE_NAME --zone=$ZONE --command='sudo journalctl -u nexus-server -f'"
echo ""
echo "  # Restart server"
echo "  gcloud compute ssh $INSTANCE_NAME --zone=$ZONE --command='sudo systemctl restart nexus-server'"
echo ""
echo "  # Stop instance (to save costs)"
echo "  gcloud compute instances stop $INSTANCE_NAME --zone=$ZONE"
echo ""
echo "  # Start instance"
echo "  gcloud compute instances start $INSTANCE_NAME --zone=$ZONE"
echo ""
echo "  # Delete instance"
echo "  gcloud compute instances delete $INSTANCE_NAME --zone=$ZONE"
echo ""
if [[ -n "$API_KEY" ]]; then
    echo "API Key: $API_KEY (set in environment)"
    echo ""
fi
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

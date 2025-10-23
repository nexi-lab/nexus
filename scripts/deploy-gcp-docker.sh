#!/usr/bin/env bash
#
# deploy-gcp-docker.sh - Deploy Nexus RPC Server to GCP using Docker
#
# This script provisions a GCP VM instance and deploys Nexus using Docker.
#
# Prerequisites:
#   - gcloud CLI installed and authenticated
#   - GCP project created with billing enabled
#   - Compute Engine API enabled
#   - Docker installed (for building locally, optional)
#
# Usage:
#   ./deploy-gcp-docker.sh [OPTIONS]
#
# Options:
#   --project-id PROJECT_ID      GCP project ID (required)
#   --instance-name NAME         VM instance name (default: nexus-docker)
#   --zone ZONE                  GCP zone (default: us-west1-a)
#   --machine-type TYPE          Machine type (default: e2-medium)
#   --disk-size SIZE             Boot disk size in GB (default: 50)
#   --api-key KEY                Nexus API key for authentication (optional)
#   --gcs-bucket BUCKET          GCS bucket for backend storage (optional)
#   --port PORT                  Server port (default: 8080)
#   --image-name NAME            Docker image name (default: nexus-server)
#   --registry REGISTRY          GCR/Artifact Registry path (optional, uses Docker Hub if not set)
#   --build-local                Build image locally and push to GCR
#   --help                       Show this help message

set -euo pipefail

# Default values
INSTANCE_NAME="nexus-docker"
ZONE="us-west1-a"
MACHINE_TYPE="e2-medium"
DISK_SIZE="50"
PORT="8080"
PROJECT_ID=""
API_KEY=""
GCS_BUCKET=""
IMAGE_NAME="nexus-server"
REGISTRY=""
BUILD_LOCAL=false

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
        --port)
            PORT="$2"
            shift 2
            ;;
        --image-name)
            IMAGE_NAME="$2"
            shift 2
            ;;
        --registry)
            REGISTRY="$2"
            shift 2
            ;;
        --build-local)
            BUILD_LOCAL=true
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

# Build and push image if --build-local is set
if [[ "$BUILD_LOCAL" == true ]]; then
    log_info "Building Docker image locally..."

    # Set registry if not provided
    if [[ -z "$REGISTRY" ]]; then
        REGISTRY="gcr.io/${PROJECT_ID}"
        log_info "Using GCR registry: $REGISTRY"
    fi

    FULL_IMAGE="${REGISTRY}/${IMAGE_NAME}:latest"

    # Build image
    log_info "Building: $FULL_IMAGE"
    docker build -t "$FULL_IMAGE" .

    # Configure Docker to use gcloud as credential helper
    gcloud auth configure-docker gcr.io --quiet

    # Push to GCR
    log_info "Pushing image to GCR..."
    docker push "$FULL_IMAGE"

    log_success "Image pushed successfully"
else
    # Use pre-built image
    if [[ -n "$REGISTRY" ]]; then
        FULL_IMAGE="${REGISTRY}/${IMAGE_NAME}:latest"
    else
        FULL_IMAGE="${IMAGE_NAME}:latest"
    fi
    log_info "Using image: $FULL_IMAGE"
fi

# Check if instance already exists
if gcloud compute instances describe "$INSTANCE_NAME" --zone="$ZONE" &> /dev/null; then
    log_warn "Instance $INSTANCE_NAME already exists in zone $ZONE"
    read -p "Do you want to delete and recreate it? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        log_info "Deleting existing instance..."
        gcloud compute instances delete "$INSTANCE_NAME" --zone="$ZONE" --quiet
    else
        log_info "Skipping instance creation. Will update existing instance."
    fi
fi

# Create instance if it doesn't exist
if ! gcloud compute instances describe "$INSTANCE_NAME" --zone="$ZONE" &> /dev/null; then
    log_info "Creating VM instance with Docker support..."

    gcloud compute instances create-with-container "$INSTANCE_NAME" \
        --zone="$ZONE" \
        --machine-type="$MACHINE_TYPE" \
        --boot-disk-size="${DISK_SIZE}GB" \
        --boot-disk-type=pd-standard \
        --container-image="$FULL_IMAGE" \
        --container-restart-policy=always \
        --container-env=NEXUS_HOST=0.0.0.0,NEXUS_PORT=${PORT},NEXUS_API_KEY=${API_KEY},NEXUS_BACKEND=${GCS_BUCKET:+gcs},NEXUS_GCS_BUCKET_NAME=${GCS_BUCKET},NEXUS_GCS_PROJECT_ID=${PROJECT_ID} \
        --container-mount-host-path=mount-path=/app/data,host-path=/var/lib/nexus,mode=rw \
        --tags=nexus-server,http-server \
        --scopes=cloud-platform \
        --metadata=enable-oslogin=TRUE

    log_success "VM instance created successfully"

    # Wait for instance to be ready
    log_info "Waiting for instance to be ready..."
    sleep 15
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

# Wait for container to start
log_info "Waiting for container to start..."
sleep 10

# Test server
log_info "Testing server health..."
MAX_RETRIES=10
RETRY_COUNT=0

while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if curl -sf "http://${EXTERNAL_IP}:${PORT}/health" > /dev/null; then
        log_success "Server is healthy!"
        break
    else
        RETRY_COUNT=$((RETRY_COUNT+1))
        if [ $RETRY_COUNT -lt $MAX_RETRIES ]; then
            log_info "Waiting for server to be ready... ($RETRY_COUNT/$MAX_RETRIES)"
            sleep 5
        else
            log_warn "Health check timed out. Server may still be starting..."
        fi
    fi
done

# Print summary
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log_success "Nexus Server Docker Deployment Complete!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Instance Details:"
echo "  - Name: $INSTANCE_NAME"
echo "  - Zone: $ZONE"
echo "  - External IP: $EXTERNAL_IP"
echo "  - Server URL: http://${EXTERNAL_IP}:${PORT}"
echo "  - Docker Image: $FULL_IMAGE"
echo ""
echo "Testing:"
echo "  curl http://${EXTERNAL_IP}:${PORT}/health"
echo ""
echo "Management:"
echo "  # SSH into instance"
echo "  gcloud compute ssh $INSTANCE_NAME --zone=$ZONE"
echo ""
echo "  # View container logs"
echo "  gcloud compute ssh $INSTANCE_NAME --zone=$ZONE --command='sudo docker logs -f \$(sudo docker ps -q)'"
echo ""
echo "  # Restart container"
echo "  gcloud compute ssh $INSTANCE_NAME --zone=$ZONE --command='sudo docker restart \$(sudo docker ps -q)'"
echo ""
echo "  # Update to latest image"
echo "  gcloud compute ssh $INSTANCE_NAME --zone=$ZONE --command='sudo docker pull $FULL_IMAGE && sudo docker restart \$(sudo docker ps -q)'"
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
    echo "API Key: $API_KEY"
    echo ""
fi
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

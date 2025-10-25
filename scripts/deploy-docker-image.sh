#!/usr/bin/env bash
#
# deploy-docker-image.sh - Deploy pre-built Docker image to nexus-server VM
#
# Usage:
#   ./deploy-docker-image.sh [OPTIONS]
#
# Options:
#   --project-id PROJECT_ID         GCP project ID (default: nexi-lab-888)
#   --instance-name NAME            VM instance name (default: nexus-server)
#   --zone ZONE                     GCP zone (default: us-west1-a)
#   --image IMAGE                   Docker image to deploy (default: gcr.io/$PROJECT_ID/nexus-server:latest)
#   --port PORT                     Server port (default: 8080)
#   --backend BACKEND               Backend type: local or gcs (default: gcs)
#   --gcs-bucket BUCKET             GCS bucket name (default: nexi-hub)
#   --gcs-project PROJECT           GCS project ID (default: same as --project-id)
#   --cloud-sql-instance NAME       Cloud SQL instance connection name (e.g., project:region:instance)
#   --db-name NAME                  Database name (default: nexus)
#   --db-user USER                  Database username (default: nexus)
#   --db-password PASS              Database password (required if using Cloud SQL)
#   --help                          Show this help message
#
# Examples:
#   # Deploy with GCS backend and SQLite metadata store
#   ./deploy-docker-image.sh --backend gcs --gcs-bucket my-bucket
#
#   # Deploy with GCS backend and Cloud SQL PostgreSQL
#   ./deploy-docker-image.sh \
#     --backend gcs \
#     --gcs-bucket nexi-hub \
#     --cloud-sql-instance nexi-lab-888:us-west1:nexus-hub \
#     --db-name nexus \
#     --db-user postgres \
#     --db-password "Nexus-Hub2025"
#
#   # Deploy with local backend and SQLite
#   ./deploy-docker-image.sh --backend local

set -euo pipefail

# Default values
PROJECT_ID="nexi-lab-888"
INSTANCE_NAME="nexus-server"
ZONE="us-west1-a"
IMAGE=""
PORT="8080"
DATA_DIR="/var/lib/nexus"
CONTAINER_NAME="nexus-container"
SQL_PROXY_CONTAINER="cloudsql-proxy"
BACKEND="gcs"
GCS_BUCKET="nexi-hub"
GCS_PROJECT=""
CLOUD_SQL_INSTANCE=""
DB_NAME="nexus"
DB_USER="nexus"
DB_PASSWORD=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --project-id) PROJECT_ID="$2"; shift 2 ;;
        --instance-name) INSTANCE_NAME="$2"; shift 2 ;;
        --zone) ZONE="$2"; shift 2 ;;
        --image) IMAGE="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --backend) BACKEND="$2"; shift 2 ;;
        --gcs-bucket) GCS_BUCKET="$2"; shift 2 ;;
        --gcs-project) GCS_PROJECT="$2"; shift 2 ;;
        --cloud-sql-instance) CLOUD_SQL_INSTANCE="$2"; shift 2 ;;
        --db-name) DB_NAME="$2"; shift 2 ;;
        --db-user) DB_USER="$2"; shift 2 ;;
        --db-password) DB_PASSWORD="$2"; shift 2 ;;
        --help|-h) grep '^#' "$0" | grep -v '#!/usr/bin/env' | sed 's/^# //' | sed 's/^#//'; exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Set GCS project to PROJECT_ID if not explicitly provided
if [[ -z "$GCS_PROJECT" ]]; then
    GCS_PROJECT="$PROJECT_ID"
fi

# Validate Cloud SQL options
if [[ -n "$CLOUD_SQL_INSTANCE" ]] && [[ -z "$DB_PASSWORD" ]]; then
    echo "Error: --db-password is required when using --cloud-sql-instance"
    exit 1
fi

# Set default image if not provided
if [[ -z "$IMAGE" ]]; then
    IMAGE="gcr.io/${PROJECT_ID}/nexus-server:latest"
fi

echo "Deploying $IMAGE to $INSTANCE_NAME..."
echo "  Backend: $BACKEND"
if [[ "$BACKEND" == "gcs" ]]; then
    echo "  GCS Bucket: $GCS_BUCKET"
    echo "  GCS Project: $GCS_PROJECT"
fi

if [[ -n "$CLOUD_SQL_INSTANCE" ]]; then
    echo "  Metadata Store: PostgreSQL (Cloud SQL)"
    echo "  Cloud SQL Instance: $CLOUD_SQL_INSTANCE"
    echo "  Database: $DB_NAME"
    echo "  User: $DB_USER"
else
    echo "  Metadata Store: SQLite (local file)"
fi

# Deploy to VM
DEPLOY_SCRIPT=$(cat <<'EOFSCRIPT'
set -e

# Authenticate Docker with GCR
ACCESS_TOKEN=$(curl -s -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" | grep -o '"access_token":"[^"]*' | cut -d'"' -f4)
export DOCKER_CONFIG=/tmp/.docker
mkdir -p $DOCKER_CONFIG
echo "$ACCESS_TOKEN" | sudo docker --config=$DOCKER_CONFIG login -u oauth2accesstoken --password-stdin https://gcr.io 2>&1 | grep -v "WARNING"

# Pull image
echo "Pulling Docker image..."
sudo docker --config=$DOCKER_CONFIG pull IMAGE_PLACEHOLDER

# Stop and remove existing containers
echo "Stopping existing containers..."
sudo docker stop CONTAINER_NAME_PLACEHOLDER 2>/dev/null || true
sudo docker rm CONTAINER_NAME_PLACEHOLDER 2>/dev/null || true
sudo docker stop SQL_PROXY_PLACEHOLDER 2>/dev/null || true
sudo docker rm SQL_PROXY_PLACEHOLDER 2>/dev/null || true

# Setup data directory
sudo mkdir -p DATA_DIR_PLACEHOLDER
sudo chown -R 1000:1000 DATA_DIR_PLACEHOLDER

# Check if using Cloud SQL
if [[ -n "CLOUD_SQL_INSTANCE_PLACEHOLDER" ]]; then
    echo "Setting up Cloud SQL Auth Proxy..."

    # Start Cloud SQL Auth Proxy container on host network
    # This allows both containers to access GCE metadata service
    sudo docker run -d \
      --name SQL_PROXY_PLACEHOLDER \
      --restart unless-stopped \
      --network host \
      gcr.io/cloud-sql-connectors/cloud-sql-proxy:latest \
      --address 127.0.0.1 \
      --port 5432 \
      CLOUD_SQL_INSTANCE_PLACEHOLDER

    # Wait for proxy to be ready
    echo "Waiting for Cloud SQL Proxy to be ready..."
    sleep 5

    # Build database URL for PostgreSQL (using localhost since both containers use host network)
    DATABASE_URL="postgresql://DB_USER_PLACEHOLDER:DB_PASSWORD_PLACEHOLDER@127.0.0.1:5432/DB_NAME_PLACEHOLDER"

    # Start Nexus container with PostgreSQL on host network
    # Host network is required to access GCE metadata service for GCS auth
    echo "Starting Nexus with PostgreSQL metadata store..."
    sudo docker run -d \
      --name CONTAINER_NAME_PLACEHOLDER \
      --restart unless-stopped \
      --network host \
      -e NEXUS_HOST=0.0.0.0 \
      -e NEXUS_PORT=PORT_PLACEHOLDER \
      -e NEXUS_BACKEND=BACKEND_PLACEHOLDER \
      -e NEXUS_GCS_BUCKET=GCS_BUCKET_PLACEHOLDER \
      -e NEXUS_GCS_PROJECT=GCS_PROJECT_PLACEHOLDER \
      -e NEXUS_DATA_DIR=/app/data \
      -e NEXUS_DATABASE_URL="$DATABASE_URL" \
      -v DATA_DIR_PLACEHOLDER:/app/data \
      IMAGE_PLACEHOLDER

    echo "✓ Using PostgreSQL metadata store (Cloud SQL)"
else
    # Start Nexus container with SQLite (no network needed, use host network for GCS metadata access)
    echo "Starting Nexus with SQLite metadata store..."
    sudo docker run -d \
      --name CONTAINER_NAME_PLACEHOLDER \
      --restart unless-stopped \
      --network host \
      -e NEXUS_HOST=0.0.0.0 \
      -e NEXUS_PORT=PORT_PLACEHOLDER \
      -e NEXUS_BACKEND=BACKEND_PLACEHOLDER \
      -e NEXUS_GCS_BUCKET=GCS_BUCKET_PLACEHOLDER \
      -e NEXUS_GCS_PROJECT=GCS_PROJECT_PLACEHOLDER \
      -e NEXUS_DATA_DIR=/app/data \
      -v DATA_DIR_PLACEHOLDER:/app/data \
      IMAGE_PLACEHOLDER

    echo "✓ Using SQLite metadata store (local file)"
fi

# Setup port 80 forwarding (excluding metadata service)
echo "Setting up port 80 → PORT_PLACEHOLDER forwarding..."
sudo iptables -t nat -C PREROUTING -p tcp --dport 80 -j REDIRECT --to-port PORT_PLACEHOLDER 2>/dev/null || \
  sudo iptables -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port PORT_PLACEHOLDER

# Wait and check health
echo "Waiting for server to start..."
sleep 15

if curl -f http://localhost:PORT_PLACEHOLDER/health 2>/dev/null; then
    echo "✓ Deployment successful!"
    echo "✓ Port 80 forwarding enabled"
else
    echo "⚠ Health check failed. Checking logs..."
    sudo docker logs CONTAINER_NAME_PLACEHOLDER
    if [[ -n "CLOUD_SQL_INSTANCE_PLACEHOLDER" ]]; then
        echo "Cloud SQL Proxy logs:"
        sudo docker logs SQL_PROXY_PLACEHOLDER
    fi
    exit 1
fi
EOFSCRIPT
)

# Replace placeholders in the script
DEPLOY_SCRIPT="${DEPLOY_SCRIPT//IMAGE_PLACEHOLDER/$IMAGE}"
DEPLOY_SCRIPT="${DEPLOY_SCRIPT//CONTAINER_NAME_PLACEHOLDER/$CONTAINER_NAME}"
DEPLOY_SCRIPT="${DEPLOY_SCRIPT//SQL_PROXY_PLACEHOLDER/$SQL_PROXY_CONTAINER}"
DEPLOY_SCRIPT="${DEPLOY_SCRIPT//DATA_DIR_PLACEHOLDER/$DATA_DIR}"
DEPLOY_SCRIPT="${DEPLOY_SCRIPT//PORT_PLACEHOLDER/$PORT}"
DEPLOY_SCRIPT="${DEPLOY_SCRIPT//PROJECT_ID_PLACEHOLDER/$PROJECT_ID}"
DEPLOY_SCRIPT="${DEPLOY_SCRIPT//BACKEND_PLACEHOLDER/$BACKEND}"
DEPLOY_SCRIPT="${DEPLOY_SCRIPT//GCS_BUCKET_PLACEHOLDER/$GCS_BUCKET}"
DEPLOY_SCRIPT="${DEPLOY_SCRIPT//GCS_PROJECT_PLACEHOLDER/$GCS_PROJECT}"
DEPLOY_SCRIPT="${DEPLOY_SCRIPT//CLOUD_SQL_INSTANCE_PLACEHOLDER/$CLOUD_SQL_INSTANCE}"
DEPLOY_SCRIPT="${DEPLOY_SCRIPT//DB_NAME_PLACEHOLDER/$DB_NAME}"
DEPLOY_SCRIPT="${DEPLOY_SCRIPT//DB_USER_PLACEHOLDER/$DB_USER}"
DEPLOY_SCRIPT="${DEPLOY_SCRIPT//DB_PASSWORD_PLACEHOLDER/$DB_PASSWORD}"

# Execute deployment script on VM
gcloud compute ssh "$INSTANCE_NAME" \
    --zone="$ZONE" \
    --project="$PROJECT_ID" \
    --command="$DEPLOY_SCRIPT"

# Create firewall rule for port 80
echo "Updating firewall for port 80..."
gcloud compute firewall-rules create allow-nexus-80 \
    --project="$PROJECT_ID" \
    --allow=tcp:80 \
    --target-tags=nexus-server \
    --description="Allow HTTP traffic on port 80" \
    2>/dev/null || true

# Get external IP
EXTERNAL_IP=$(gcloud compute instances describe "$INSTANCE_NAME" \
    --zone="$ZONE" \
    --project="$PROJECT_ID" \
    --format='get(networkInterfaces[0].accessConfigs[0].natIP)')

echo ""
echo "✓ Deployed successfully!"
echo "  Server URL: http://${EXTERNAL_IP} (port 80)"
echo "  Alternative: http://${EXTERNAL_IP}:${PORT} (port 8080)"
echo "  Backend: $BACKEND"
if [[ "$BACKEND" == "gcs" ]]; then
    echo "  GCS Bucket: $GCS_BUCKET"
fi
if [[ -n "$CLOUD_SQL_INSTANCE" ]]; then
    echo "  Metadata Store: PostgreSQL (Cloud SQL: $CLOUD_SQL_INSTANCE)"
else
    echo "  Metadata Store: SQLite (local file)"
fi
echo ""
echo "Test:"
echo "  curl http://${EXTERNAL_IP}/health"
echo ""
echo "Mount:"
echo "  nexus mount /tmp/nexus --remote-url http://${EXTERNAL_IP}"
echo ""

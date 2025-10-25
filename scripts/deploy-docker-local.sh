#!/usr/bin/env bash
#
# deploy-docker-local.sh - Build and deploy Docker image from local source
#
# This script builds a Docker image from your local source code and deploys
# it to the nexus-server VM. Use this for testing changes before releasing to PyPI.
#
# Usage:
#   ./deploy-docker-local.sh [OPTIONS]
#
# Options:
#   --project-id PROJECT_ID           GCP project ID (default: nexi-lab-888)
#   --instance-name NAME              VM instance name (default: nexus-server)
#   --zone ZONE                       GCP zone (default: us-west1-a)
#   --tag TAG                         Docker image tag (default: local-TIMESTAMP)
#   --port PORT                       Server port (default: 8080)
#   --cloud-sql-instance INSTANCE     Cloud SQL instance for PostgreSQL metadata
#   --db-name NAME                    Database name (default: nexus)
#   --db-user USER                    Database user (default: postgres)
#   --db-password PASSWORD            Database password (required if using Cloud SQL)
#   --skip-build                      Skip building new image, use existing
#   --help                            Show this help message
#
# Examples:
#   # Build and deploy from local source
#   ./deploy-docker-local.sh
#
#   # Deploy with Cloud SQL PostgreSQL
#   ./deploy-docker-local.sh \
#     --cloud-sql-instance nexi-lab-888:us-west1:nexus-hub \
#     --db-name nexus \
#     --db-user postgres \
#     --db-password "Nexus-Hub2025"
#
#   # Use existing build
#   ./deploy-docker-local.sh --tag local-20241024 --skip-build

set -euo pipefail

# Default values
PROJECT_ID="nexi-lab-888"
INSTANCE_NAME="nexus-server"
ZONE="us-west1-a"
TAG="local-$(date +%Y%m%d-%H%M%S)"
PORT="8080"
DATA_DIR="/var/lib/nexus"
CONTAINER_NAME="nexus-container"
SKIP_BUILD=false
CLOUD_SQL_INSTANCE=""
DB_NAME="nexus"
DB_USER="postgres"
DB_PASSWORD=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --project-id) PROJECT_ID="$2"; shift 2 ;;
        --instance-name) INSTANCE_NAME="$2"; shift 2 ;;
        --zone) ZONE="$2"; shift 2 ;;
        --tag) TAG="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --cloud-sql-instance) CLOUD_SQL_INSTANCE="$2"; shift 2 ;;
        --db-name) DB_NAME="$2"; shift 2 ;;
        --db-user) DB_USER="$2"; shift 2 ;;
        --db-password) DB_PASSWORD="$2"; shift 2 ;;
        --skip-build) SKIP_BUILD=true; shift ;;
        --help|-h) grep '^#' "$0" | grep -v '#!/usr/bin/env' | sed 's/^# //' | sed 's/^#//'; exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

IMAGE="gcr.io/${PROJECT_ID}/nexus-server:${TAG}"

# Build Docker image from local source
if [[ "$SKIP_BUILD" == "false" ]]; then
    echo "Building Docker image from local source..."
    echo "  Image: $IMAGE"

    # Get git commit hash for metadata
    GIT_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
    BUILD_DATE=$(date -u +'%Y-%m-%dT%H:%M:%SZ')

    # Build using local Dockerfile
    docker build \
        --file Dockerfile \
        --tag "$IMAGE" \
        --build-arg GIT_COMMIT="$GIT_COMMIT" \
        --build-arg BUILD_DATE="$BUILD_DATE" \
        --label "com.nexus.source=local" \
        --label "com.nexus.git.commit=$GIT_COMMIT" \
        .

    echo "✓ Image built successfully"

    # Push to GCR
    echo "Pushing image to GCR..."
    docker push "$IMAGE"
    echo "✓ Image pushed to $IMAGE"
else
    echo "Skipping build, using existing image: $IMAGE"
fi

echo ""
echo "Deploying $IMAGE to $INSTANCE_NAME..."

# Build deployment script
if [[ -n "$CLOUD_SQL_INSTANCE" ]]; then
    # Deploy with Cloud SQL PostgreSQL
    if [[ -z "$DB_PASSWORD" ]]; then
        echo "Error: --db-password required when using Cloud SQL"
        exit 1
    fi

    echo "  Using Cloud SQL: $CLOUD_SQL_INSTANCE"
    echo "  Database: $DB_NAME (user: $DB_USER)"

    DEPLOY_SCRIPT=$(cat <<EOF
set -e

# Authenticate Docker with GCR
ACCESS_TOKEN=\$(curl -s -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" | grep -o '"access_token":"[^"]*' | cut -d'"' -f4)
export DOCKER_CONFIG=/tmp/.docker
mkdir -p \$DOCKER_CONFIG
echo "\$ACCESS_TOKEN" | sudo docker --config=\$DOCKER_CONFIG login -u oauth2accesstoken --password-stdin https://gcr.io 2>&1 | grep -v "WARNING"

# Pull image
sudo docker --config=\$DOCKER_CONFIG pull $IMAGE

# Stop all running containers
echo "Stopping all running containers..."
sudo docker stop \$(sudo docker ps -q) 2>/dev/null || true
sudo docker rm \$(sudo docker ps -aq) 2>/dev/null || true

# Setup data directory
sudo mkdir -p $DATA_DIR
sudo chown -R 1000:1000 $DATA_DIR

# Start Cloud SQL Proxy
echo "Starting Cloud SQL Proxy..."
sudo docker run -d \
  --name cloudsql-proxy \
  --restart unless-stopped \
  --network=host \
  gcr.io/cloud-sql-connectors/cloud-sql-proxy:latest \
  --port 5432 \
  $CLOUD_SQL_INSTANCE

# Wait for proxy
sleep 5

# Start Nexus container with PostgreSQL
echo "Starting Nexus server..."
sudo docker run -d \
  --name $CONTAINER_NAME \
  --restart unless-stopped \
  --network=host \
  -e NEXUS_HOST=0.0.0.0 \
  -e NEXUS_PORT=$PORT \
  -e NEXUS_BACKEND=gcs \
  -e NEXUS_GCS_BUCKET=nexi-hub \
  -e NEXUS_GCS_PROJECT=$PROJECT_ID \
  -e NEXUS_DATA_DIR=/app/data \
  -e NEXUS_DATABASE_URL="postgresql://$DB_USER:$DB_PASSWORD@127.0.0.1:5432/$DB_NAME" \
  -e CLOUD_SQL_INSTANCE="$CLOUD_SQL_INSTANCE" \
  -v $DATA_DIR:/app/data \
  $IMAGE

# Setup port 80 forwarding
echo "Setting up port 80 → 8080 forwarding..."
sudo iptables -t nat -C PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8080 2>/dev/null || \
  sudo iptables -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8080

# Wait and check health
sleep 10
if curl -f http://localhost:$PORT/health 2>/dev/null; then
    echo "✓ Deployment successful!"
    echo "✓ Port 80 forwarding enabled"
    echo "✓ Cloud SQL Proxy running"
else
    echo "⚠ Health check failed. Check logs:"
    echo "  sudo docker logs $CONTAINER_NAME"
    echo "  sudo docker logs cloudsql-proxy"
    exit 1
fi
EOF
)
else
    # Deploy without Cloud SQL (GCS backend only)
    echo "  Using GCS backend only (no PostgreSQL)"

    DEPLOY_SCRIPT=$(cat <<EOF
set -e

# Authenticate Docker with GCR
ACCESS_TOKEN=\$(curl -s -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" | grep -o '"access_token":"[^"]*' | cut -d'"' -f4)
export DOCKER_CONFIG=/tmp/.docker
mkdir -p \$DOCKER_CONFIG
echo "\$ACCESS_TOKEN" | sudo docker --config=\$DOCKER_CONFIG login -u oauth2accesstoken --password-stdin https://gcr.io 2>&1 | grep -v "WARNING"

# Pull image
sudo docker --config=\$DOCKER_CONFIG pull $IMAGE

# Stop all running containers
echo "Stopping all running containers..."
sudo docker stop \$(sudo docker ps -q) 2>/dev/null || true
sudo docker rm \$(sudo docker ps -aq) 2>/dev/null || true

# Setup data directory
sudo mkdir -p $DATA_DIR
sudo chown -R 1000:1000 $DATA_DIR

# Start Nexus container with GCS backend
sudo docker run -d \
  --name $CONTAINER_NAME \
  --restart unless-stopped \
  --network=host \
  -e NEXUS_HOST=0.0.0.0 \
  -e NEXUS_PORT=$PORT \
  -e NEXUS_BACKEND=gcs \
  -e NEXUS_GCS_BUCKET=nexi-hub \
  -e NEXUS_GCS_PROJECT=$PROJECT_ID \
  -e NEXUS_DATA_DIR=/app/data \
  -v $DATA_DIR:/app/data \
  $IMAGE

# Setup port 80 forwarding
echo "Setting up port 80 → 8080 forwarding..."
sudo iptables -t nat -C PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8080 2>/dev/null || \
  sudo iptables -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8080

# Wait and check health
sleep 10
if curl -f http://localhost:$PORT/health 2>/dev/null; then
    echo "✓ Deployment successful!"
    echo "✓ Port 80 forwarding enabled"
else
    echo "⚠ Health check failed. Check logs:"
    echo "  gcloud compute ssh $INSTANCE_NAME --zone=$ZONE --command='sudo docker logs $CONTAINER_NAME'"
    exit 1
fi
EOF
)
fi

# Execute deployment on VM
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
echo "========================================="
echo "✓ Deployed successfully!"
echo "========================================="
echo ""
echo "Docker Image: $IMAGE"
echo "Source: Local build ($(git rev-parse --short HEAD 2>/dev/null || echo 'unknown'))"
echo ""
echo "Server:"
echo "  URL: http://${EXTERNAL_IP} (port 80)"
echo "  Alternative: http://${EXTERNAL_IP}:${PORT} (port 8080)"
echo ""
if [[ -n "$CLOUD_SQL_INSTANCE" ]]; then
echo "Database:"
echo "  Cloud SQL: $CLOUD_SQL_INSTANCE"
echo "  Database: $DB_NAME"
echo ""
fi
echo "Test:"
echo "  curl http://${EXTERNAL_IP}/health"
echo ""
echo "View logs:"
echo "  gcloud compute ssh $INSTANCE_NAME --zone=$ZONE --command='sudo docker logs -f $CONTAINER_NAME'"
echo ""
echo "Mount:"
echo "  nexus mount /tmp/nexus --remote-url http://${EXTERNAL_IP}"
echo ""

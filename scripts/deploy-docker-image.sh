#!/usr/bin/env bash
#
# deploy-docker-image.sh - Deploy pre-built Docker image to nexus-server VM
#
# Usage:
#   ./deploy-docker-image.sh [OPTIONS]
#
# Options:
#   --project-id PROJECT_ID  GCP project ID (default: nexi-lab-888)
#   --instance-name NAME     VM instance name (default: nexus-server)
#   --zone ZONE              GCP zone (default: us-west1-a)
#   --image IMAGE            Docker image to deploy (default: gcr.io/$PROJECT_ID/nexus-server:latest)
#   --port PORT              Server port (default: 8080)
#   --help                   Show this help message

set -euo pipefail

# Default values
PROJECT_ID="nexi-lab-888"
INSTANCE_NAME="nexus-server"
ZONE="us-west1-a"
IMAGE=""
PORT="8080"
DATA_DIR="/var/lib/nexus"
CONTAINER_NAME="nexus-container"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --project-id) PROJECT_ID="$2"; shift 2 ;;
        --instance-name) INSTANCE_NAME="$2"; shift 2 ;;
        --zone) ZONE="$2"; shift 2 ;;
        --image) IMAGE="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --help|-h) grep '^#' "$0" | grep -v '#!/usr/bin/env' | sed 's/^# //' | sed 's/^#//'; exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Set default image if not provided
if [[ -z "$IMAGE" ]]; then
    IMAGE="gcr.io/${PROJECT_ID}/nexus-server:latest"
fi

echo "Deploying $IMAGE to $INSTANCE_NAME..."

# Deploy to VM
DEPLOY_SCRIPT=$(cat <<EOF
set -e

# Authenticate Docker with GCR
ACCESS_TOKEN=\$(curl -s -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" | grep -o '"access_token":"[^"]*' | cut -d'"' -f4)
export DOCKER_CONFIG=/tmp/.docker
mkdir -p \$DOCKER_CONFIG
echo "\$ACCESS_TOKEN" | sudo docker --config=\$DOCKER_CONFIG login -u oauth2accesstoken --password-stdin https://gcr.io 2>&1 | grep -v "WARNING"

# Pull image
sudo docker --config=\$DOCKER_CONFIG pull $IMAGE

# Stop all running containers to free up port
echo "Stopping all running containers..."
sudo docker stop \$(sudo docker ps -q) 2>/dev/null || true
sudo docker rm \$(sudo docker ps -aq) 2>/dev/null || true

# Setup data directory
sudo mkdir -p $DATA_DIR
sudo chown -R 1000:1000 $DATA_DIR

# Start new container with GCS backend (using host network for metadata access)
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

# Setup port 80 forwarding (excluding metadata service)
echo "Setting up port 80 → 8080 forwarding..."
# Only redirect external traffic on port 80, not localhost or metadata service
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
echo ""
echo "Test:"
echo "  curl http://${EXTERNAL_IP}/health"
echo ""
echo "Mount:"
echo "  nexus mount /tmp/nexus --remote-url http://${EXTERNAL_IP}"
echo ""

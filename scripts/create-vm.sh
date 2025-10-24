#!/usr/bin/env bash
#
# create-vm.sh - Create a VM instance for running Nexus Docker containers
#
# Usage:
#   ./create-vm.sh [OPTIONS]
#
# Options:
#   --project-id PROJECT_ID  GCP project ID (default: nexi-lab-888)
#   --instance-name NAME     VM instance name (default: nexus-server)
#   --zone ZONE              GCP zone (default: us-west1-a)
#   --machine-type TYPE      Machine type (default: e2-medium)
#   --help                   Show this help message

set -euo pipefail

# Default values
PROJECT_ID="nexi-lab-888"
INSTANCE_NAME="nexus-server"
ZONE="us-west1-a"
MACHINE_TYPE="e2-medium"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --project-id) PROJECT_ID="$2"; shift 2 ;;
        --instance-name) INSTANCE_NAME="$2"; shift 2 ;;
        --zone) ZONE="$2"; shift 2 ;;
        --machine-type) MACHINE_TYPE="$2"; shift 2 ;;
        --help|-h) grep '^#' "$0" | grep -v '#!/usr/bin/env' | sed 's/^# //' | sed 's/^#//'; exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

echo "Creating VM instance: $INSTANCE_NAME in $ZONE..."

# Create VM with Container-Optimized OS
gcloud compute instances create "$INSTANCE_NAME" \
    --project="$PROJECT_ID" \
    --zone="$ZONE" \
    --machine-type="$MACHINE_TYPE" \
    --boot-disk-size=50GB \
    --image-family=cos-stable \
    --image-project=cos-cloud \
    --tags=nexus-server \
    --scopes=cloud-platform

# Create firewall rule for port 8080
gcloud compute firewall-rules create allow-nexus-8080 \
    --project="$PROJECT_ID" \
    --allow=tcp:8080 \
    --target-tags=nexus-server \
    --description="Allow Nexus server traffic on port 8080" \
    2>/dev/null || true

# Get external IP
EXTERNAL_IP=$(gcloud compute instances describe "$INSTANCE_NAME" \
    --zone="$ZONE" \
    --project="$PROJECT_ID" \
    --format='get(networkInterfaces[0].accessConfigs[0].natIP)')

echo ""
echo "✓ VM created successfully!"
echo ""
echo "  Instance: $INSTANCE_NAME"
echo "  External IP: $EXTERNAL_IP"
echo "  Server URL: http://${EXTERNAL_IP}:8080"
echo ""
echo "Next steps:"
echo "  1. Build Docker image: gcloud builds submit . --config=cloudbuild-pypi.yaml --project=$PROJECT_ID"
echo "  2. Deploy: ./scripts/deploy-docker-image.sh"
echo ""

#!/bin/bash
# Deploy Nexus Server to GCP Compute Engine VM
#
# Usage:
#   ./deploy-compute-vm.sh
#
# Required environment variables:
#   GCP_PROJECT_ID      - Your GCP project ID
#   NEXUS_ACCESS_KEY    - Access key for authentication
#   NEXUS_SECRET_KEY    - Secret key for authentication
#   NEXUS_GCS_BUCKET    - GCS bucket for storage
#
# Optional environment variables:
#   GCP_REGION          - GCP region (default: us-central1)
#   GCP_ZONE            - GCP zone (default: us-central1-a)
#   VM_NAME             - VM instance name (default: nexus-server)
#   MACHINE_TYPE        - Machine type (default: e2-small)
#   BOOT_DISK_SIZE      - Boot disk size in GB (default: 20)

set -e

# Configuration
GCP_PROJECT_ID="${GCP_PROJECT_ID}"
GCP_REGION="${GCP_REGION:-us-west1}"
GCP_ZONE="${GCP_ZONE:-us-west1-a}"
VM_NAME="${VM_NAME:-nexus-server}"
MACHINE_TYPE="${MACHINE_TYPE:-e2-small}"
BOOT_DISK_SIZE="${BOOT_DISK_SIZE:-20}"

# Authentication
NEXUS_ACCESS_KEY="${NEXUS_ACCESS_KEY}"
NEXUS_SECRET_KEY="${NEXUS_SECRET_KEY}"

# Storage configuration
NEXUS_GCS_BUCKET="${NEXUS_GCS_BUCKET}"

# Validate required variables
if [ -z "$GCP_PROJECT_ID" ]; then
    echo "Error: GCP_PROJECT_ID is required"
    exit 1
fi

if [ -z "$NEXUS_ACCESS_KEY" ] || [ -z "$NEXUS_SECRET_KEY" ]; then
    echo "Error: NEXUS_ACCESS_KEY and NEXUS_SECRET_KEY are required"
    exit 1
fi

if [ -z "$NEXUS_GCS_BUCKET" ]; then
    echo "Error: NEXUS_GCS_BUCKET is required"
    exit 1
fi

echo "=================================="
echo "Deploying Nexus Server to Compute Engine"
echo "=================================="
echo "Project ID: $GCP_PROJECT_ID"
echo "Zone: $GCP_ZONE"
echo "VM Name: $VM_NAME"
echo "Machine Type: $MACHINE_TYPE"
echo "GCS Bucket: $NEXUS_GCS_BUCKET"
echo "=================================="

# Set GCP project
echo "Setting GCP project..."
gcloud config set project "$GCP_PROJECT_ID"

# Enable required APIs
echo "Enabling required GCP APIs..."
gcloud services enable compute.googleapis.com storage-api.googleapis.com

# Create startup script
STARTUP_SCRIPT=$(cat <<'EOF'
#!/bin/bash
set -e

# Install Docker if not already installed
if ! command -v docker &> /dev/null; then
    echo "Installing Docker..."
    curl -fsSL https://get.docker.com -o get-docker.sh
    sh get-docker.sh
    usermod -aG docker $(whoami)
fi

# Pull and run the Nexus container
echo "Starting Nexus server..."
docker pull gcr.io/PROJECT_ID/nexus-server:latest || {
    echo "Image not found in GCR. Building locally..."
    cd /opt/nexus
    docker build -t nexus-server .
}

# Stop and remove any existing container
docker stop nexus-server 2>/dev/null || true
docker rm nexus-server 2>/dev/null || true

# Run the container
docker run -d \
    --name nexus-server \
    --restart unless-stopped \
    -p 8080:8080 \
    -v /var/lib/nexus:/app/data \
    -e NEXUS_ACCESS_KEY="ACCESS_KEY_PLACEHOLDER" \
    -e NEXUS_SECRET_KEY="SECRET_KEY_PLACEHOLDER" \
    -e NEXUS_STORAGE_BACKEND=gcs \
    -e NEXUS_GCS_BUCKET="GCS_BUCKET_PLACEHOLDER" \
    -e NEXUS_GCS_PROJECT="PROJECT_ID" \
    -e NEXUS_DB_SYNC=false \
    nexus-server

echo "Nexus server started successfully"

# Show initial logs (without following)
docker logs nexus-server

echo "Startup script completed. Use 'docker logs -f nexus-server' to follow logs."
EOF
)

# Replace placeholders in startup script
STARTUP_SCRIPT="${STARTUP_SCRIPT//PROJECT_ID/$GCP_PROJECT_ID}"
STARTUP_SCRIPT="${STARTUP_SCRIPT//ACCESS_KEY_PLACEHOLDER/$NEXUS_ACCESS_KEY}"
STARTUP_SCRIPT="${STARTUP_SCRIPT//SECRET_KEY_PLACEHOLDER/$NEXUS_SECRET_KEY}"
STARTUP_SCRIPT="${STARTUP_SCRIPT//GCS_BUCKET_PLACEHOLDER/$NEXUS_GCS_BUCKET}"

# Check if VM already exists
if gcloud compute instances describe "$VM_NAME" --zone="$GCP_ZONE" &>/dev/null; then
    echo "VM $VM_NAME already exists"
    read -p "Do you want to recreate it? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "Deleting existing VM..."
        gcloud compute instances delete "$VM_NAME" --zone="$GCP_ZONE" --quiet
    else
        echo "Updating existing VM..."
        gcloud compute instances add-metadata "$VM_NAME" \
            --zone="$GCP_ZONE" \
            --metadata=startup-script="$STARTUP_SCRIPT"

        echo "Restarting VM..."
        gcloud compute instances stop "$VM_NAME" --zone="$GCP_ZONE"
        gcloud compute instances start "$VM_NAME" --zone="$GCP_ZONE"

        VM_IP=$(gcloud compute instances describe "$VM_NAME" \
            --zone="$GCP_ZONE" \
            --format='get(networkInterfaces[0].accessConfigs[0].natIP)')

        echo ""
        echo "=================================="
        echo "VM updated and restarted!"
        echo "=================================="
        echo "VM IP: $VM_IP"
        echo "Service URL: http://$VM_IP:8080"
        echo ""
        echo "View logs with:"
        echo "  gcloud compute ssh $VM_NAME --zone=$GCP_ZONE -- 'docker logs -f nexus-server'"
        echo "=================================="
        exit 0
    fi
fi

# Create firewall rule for port 8080 (if not exists)
echo "Creating firewall rule..."
gcloud compute firewall-rules create allow-nexus-8080 \
    --allow tcp:8080 \
    --source-ranges 0.0.0.0/0 \
    --description "Allow Nexus server traffic" 2>/dev/null || echo "Firewall rule already exists"

# Build and push Docker image to GCR (optional, for faster deployment)
echo ""
read -p "Build and push image to GCR for faster VM startup? (y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "Building and pushing image to GCR..."
    gcloud builds submit --tag gcr.io/$GCP_PROJECT_ID/nexus-server:latest
fi

# Create VM instance
echo "Creating VM instance..."
gcloud compute instances create "$VM_NAME" \
    --zone="$GCP_ZONE" \
    --machine-type="$MACHINE_TYPE" \
    --boot-disk-size="${BOOT_DISK_SIZE}GB" \
    --boot-disk-type=pd-standard \
    --image-family=cos-stable \
    --image-project=cos-cloud \
    --scopes=storage-rw,logging-write,monitoring-write \
    --metadata=startup-script="$STARTUP_SCRIPT" \
    --tags=nexus-server

echo "Waiting for VM to start..."
sleep 30

# Get VM IP
VM_IP=$(gcloud compute instances describe "$VM_NAME" \
    --zone="$GCP_ZONE" \
    --format='get(networkInterfaces[0].accessConfigs[0].natIP)')

echo ""
echo "=================================="
echo "Deployment complete!"
echo "=================================="
echo "VM Name: $VM_NAME"
echo "VM IP: $VM_IP"
echo "Service URL: http://$VM_IP:8080"
echo ""
echo "SSH into VM:"
echo "  gcloud compute ssh $VM_NAME --zone=$GCP_ZONE"
echo ""
echo "View logs:"
echo "  gcloud compute ssh $VM_NAME --zone=$GCP_ZONE -- 'docker logs -f nexus-server'"
echo ""
echo "Configure rclone:"
echo "  rclone config create nexus s3 \\"
echo "    provider=Other \\"
echo "    endpoint=http://$VM_IP:8080 \\"
echo "    access_key_id=$NEXUS_ACCESS_KEY \\"
echo "    secret_access_key=$NEXUS_SECRET_KEY \\"
echo "    force_path_style=true"
echo ""
echo "Test with:"
echo "  rclone ls nexus:"
echo "=================================="

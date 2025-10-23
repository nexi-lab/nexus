# Nexus Server - Google Cloud Platform Deployment Guide

This guide walks you through deploying a Nexus RPC server to Google Cloud Platform (GCP) using an automated deployment script.

## Overview

The deployment script (`../../scripts/deploy-gcp.sh`) automates:
- Creating a GCP VM instance (Ubuntu 22.04)
- Installing system dependencies (Python 3.11, git, etc.)
- Cloning the Nexus repository
- Setting up a virtual environment
- Installing Nexus
- Creating a systemd service for automatic startup
- Configuring firewall rules
- Optionally configuring GCS backend storage

## Prerequisites

### 1. Install Google Cloud SDK

```bash
# macOS
brew install --cask google-cloud-sdk

# Linux
curl https://sdk.cloud.google.com | bash
exec -l $SHELL

# Verify installation
gcloud --version
```

### 2. Authenticate with GCP

```bash
# Login to your Google account
gcloud auth login

# Set up application default credentials
gcloud auth application-default login
```

### 3. Create a GCP Project

```bash
# Create a new project
gcloud projects create YOUR-PROJECT-ID --name="Nexus Server"

# Set as default project
gcloud config set project YOUR-PROJECT-ID

# Enable required APIs
gcloud services enable compute.googleapis.com
gcloud services enable storage.googleapis.com  # If using GCS backend
```

### 4. Set Up Billing

- Go to [GCP Console](https://console.cloud.google.com/)
- Navigate to Billing
- Link a billing account to your project

## Quick Start

### Basic Deployment (Local Storage)

```bash
# Deploy with default settings
../../scripts/deploy-gcp.sh --project-id YOUR-PROJECT-ID

# Deploy with custom settings
../../scripts/deploy-gcp.sh \
  --project-id YOUR-PROJECT-ID \
  --instance-name my-nexus-server \
  --zone us-west1-a \
  --machine-type e2-standard-2 \
  --api-key mysecretkey123
```

### Deployment with GCS Backend

```bash
# Create GCS bucket first
gsutil mb -p YOUR-PROJECT-ID -l us-central1 gs://your-nexus-bucket

# Deploy with GCS backend
../../scripts/deploy-gcp.sh \
  --project-id YOUR-PROJECT-ID \
  --gcs-bucket your-nexus-bucket \
  --api-key mysecretkey123
```

## Configuration Options

| Option | Description | Default |
|--------|-------------|---------|
| `--project-id` | GCP project ID (required) | - |
| `--instance-name` | VM instance name | `nexus-server` |
| `--zone` | GCP zone | `us-central1-a` |
| `--machine-type` | VM machine type | `e2-medium` |
| `--disk-size` | Boot disk size in GB | `50` |
| `--api-key` | Nexus API key for authentication | none |
| `--gcs-bucket` | GCS bucket for backend storage | none (local) |
| `--data-dir` | Data directory on VM | `/var/lib/nexus` |
| `--port` | Server port | `8080` |
| `--deploy-only` | Skip VM creation, only deploy code | false |

## Machine Type Recommendations

| Machine Type | vCPUs | Memory | Use Case | Monthly Cost* |
|--------------|-------|--------|----------|---------------|
| `e2-micro` | 0.25-2 | 1 GB | Testing only | ~$7 |
| `e2-small` | 0.5-2 | 2 GB | Light usage | ~$14 |
| `e2-medium` | 1-2 | 4 GB | **Recommended** for dev | ~$27 |
| `e2-standard-2` | 2 | 8 GB | Production (small) | ~$54 |
| `e2-standard-4` | 4 | 16 GB | Production (medium) | ~$108 |

*Costs are approximate for `us-central1` region with sustained use discounts.

## GCP Zones

Choose a zone close to your users:

| Region | Zones | Location |
|--------|-------|----------|
| `us-central1` | `-a, -b, -c, -f` | Iowa, USA |
| `us-west1` | `-a, -b, -c` | Oregon, USA |
| `us-east1` | `-b, -c, -d` | South Carolina, USA |
| `europe-west1` | `-b, -c, -d` | Belgium |
| `asia-east1` | `-a, -b, -c` | Taiwan |

See [all GCP zones](https://cloud.google.com/compute/docs/regions-zones).

## Step-by-Step Deployment

### 1. Prepare Your Configuration

```bash
# Set your project ID
export PROJECT_ID="your-gcp-project-id"

# Generate a secure API key
export API_KEY=$(openssl rand -hex 32)
echo "Save this API key: $API_KEY"

# Optional: Create GCS bucket for storage
export GCS_BUCKET="nexus-storage-$(date +%s)"
gsutil mb -p $PROJECT_ID gs://$GCS_BUCKET
```

### 2. Run Deployment Script

```bash
../../scripts/deploy-gcp.sh \
  --project-id "$PROJECT_ID" \
  --instance-name nexus-prod \
  --zone us-central1-a \
  --machine-type e2-medium \
  --api-key "$API_KEY" \
  --gcs-bucket "$GCS_BUCKET"
```

The script will:
1. Create the VM instance (takes ~1-2 minutes)
2. Set up firewall rules
3. Install system dependencies
4. Clone and install Nexus
5. Create systemd service
6. Start the server
7. Verify health

### 3. Verify Deployment

```bash
# Get the external IP from script output
export EXTERNAL_IP="<ip-from-script-output>"

# Test health endpoint
curl http://$EXTERNAL_IP:8080/health

# Test status endpoint
curl http://$EXTERNAL_IP:8080/api/nfs/status

# Test with API key
curl -X POST http://$EXTERNAL_IP:8080/api/nfs/list \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_KEY" \
  -d '{"jsonrpc": "2.0", "method": "list", "params": {"path": "/"}, "id": 1}'
```

## Server Management

### SSH into the VM

```bash
# SSH into instance
gcloud compute ssh nexus-server --zone=us-central1-a

# Once inside, check server status
sudo systemctl status nexus-server

# View logs
sudo journalctl -u nexus-server -f
```

### Control the Service

```bash
# Restart the server
gcloud compute ssh nexus-server --zone=us-central1-a \
  --command='sudo systemctl restart nexus-server'

# Stop the server
gcloud compute ssh nexus-server --zone=us-central1-a \
  --command='sudo systemctl stop nexus-server'

# Start the server
gcloud compute ssh nexus-server --zone=us-central1-a \
  --command='sudo systemctl start nexus-server'

# View real-time logs
gcloud compute ssh nexus-server --zone=us-central1-a \
  --command='sudo journalctl -u nexus-server -f'
```

### Update the Server

```bash
# Redeploy with latest code (without recreating VM)
../../scripts/deploy-gcp.sh \
  --project-id YOUR-PROJECT-ID \
  --deploy-only
```

### Stop/Start Instance (Save Costs)

```bash
# Stop instance when not in use (keeps disk, stops compute charges)
gcloud compute instances stop nexus-server --zone=us-central1-a

# Start instance when needed
gcloud compute instances start nexus-server --zone=us-central1-a

# Get new external IP after starting
gcloud compute instances describe nexus-server \
  --zone=us-central1-a \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)'
```

## Troubleshooting

### Server Won't Start

```bash
# SSH into instance
gcloud compute ssh nexus-server --zone=us-central1-a

# Check service status
sudo systemctl status nexus-server

# View detailed logs
sudo journalctl -u nexus-server -n 100 --no-pager

# Check if port is in use
sudo lsof -i :8080

# Try starting manually for debugging
cd /opt/nexus/repo
sudo -u nexus .venv/bin/python -m nexus.cli serve --host 0.0.0.0 --port 8080
```

### Firewall Issues

```bash
# List firewall rules
gcloud compute firewall-rules list

# Check if rule exists
gcloud compute firewall-rules describe allow-nexus-8080

# Recreate firewall rule
gcloud compute firewall-rules delete allow-nexus-8080
gcloud compute firewall-rules create allow-nexus-8080 \
  --allow=tcp:8080 \
  --target-tags=nexus-server
```

### GCS Permission Issues

```bash
# Check service account permissions
gcloud projects get-iam-policy YOUR-PROJECT-ID

# Grant Storage Admin role to compute service account
gcloud projects add-iam-policy-binding YOUR-PROJECT-ID \
  --member="serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
  --role="roles/storage.admin"

# Find your project number
gcloud projects describe YOUR-PROJECT-ID --format='value(projectNumber)'
```

### Out of Memory

If the server crashes with OOM errors:

```bash
# Upgrade to larger machine type
gcloud compute instances stop nexus-server --zone=us-central1-a

gcloud compute instances set-machine-type nexus-server \
  --zone=us-central1-a \
  --machine-type=e2-standard-2

gcloud compute instances start nexus-server --zone=us-central1-a
```

## Cost Optimization

### Development Environment

```bash
# Use preemptible instance (up to 80% discount, can be terminated)
gcloud compute instances create nexus-dev \
  --zone=us-central1-a \
  --machine-type=e2-medium \
  --preemptible \
  --boot-disk-size=30GB \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud

# Stop instance when not in use
gcloud compute instances stop nexus-dev --zone=us-central1-a
```

### Use Spot VMs

Spot VMs are up to 91% cheaper but can be preempted:

```bash
gcloud compute instances create nexus-spot \
  --zone=us-central1-a \
  --machine-type=e2-medium \
  --provisioning-model=SPOT \
  --instance-termination-action=STOP
```

### Set Up Budget Alerts

```bash
# Go to GCP Console > Billing > Budgets & Alerts
# Set up alert at 50%, 90%, and 100% of monthly budget
```

## Production Deployment Checklist

- [ ] Use a strong API key (32+ characters)
- [ ] Configure GCS backend for persistence
- [ ] Use e2-standard-2 or larger machine type
- [ ] Set up Cloud Monitoring alerts
- [ ] Enable automatic backups of GCS bucket
- [ ] Use a reserved external IP address
- [ ] Set up Cloud Armor for DDoS protection (if public)
- [ ] Configure SSL/TLS with Cloud Load Balancer
- [ ] Set up Cloud Logging for centralized logs
- [ ] Create snapshots of boot disk periodically
- [ ] Document your API key in a secure location (1Password, etc.)

## Monitoring & Logging

### View Logs

```bash
# View logs in Cloud Console
# Go to: Logging > Logs Explorer
# Filter: resource.type="gce_instance" AND resource.labels.instance_id="<instance-id>"

# Or via CLI
gcloud logging read "resource.type=gce_instance AND resource.labels.instance_id=<instance-id>" \
  --limit=50 \
  --format=json
```

### Set Up Monitoring

```bash
# Install Cloud Monitoring agent (if needed)
gcloud compute ssh nexus-server --zone=us-central1-a --command='
  curl -sSO https://dl.google.com/cloudagents/add-google-cloud-ops-agent-repo.sh
  sudo bash add-google-cloud-ops-agent-repo.sh --also-install
'
```

## Cleanup

### Delete Everything

```bash
# Stop and delete instance
gcloud compute instances delete nexus-server --zone=us-central1-a --quiet

# Delete firewall rule
gcloud compute firewall-rules delete allow-nexus-8080 --quiet

# Delete GCS bucket (WARNING: This deletes all data!)
gsutil rm -r gs://your-nexus-bucket
```

## Using the Deployed Server

### From Python

```python
from nexus import RemoteNexusFS

# Connect to your GCP server
nx = RemoteNexusFS(
    server_url="http://YOUR-EXTERNAL-IP:8080",
    api_key="your-api-key"
)

# Use normally
nx.write("/workspace/hello.txt", b"Hello from GCP!")
content = nx.read("/workspace/hello.txt")
files = nx.list("/workspace", recursive=True)
```

### From Frontend

Update your frontend `.env` file:

```env
VITE_API_URL=http://YOUR-EXTERNAL-IP:8080
VITE_API_KEY=your-api-key
```

## Security Best Practices

1. **API Key**: Use a strong, randomly generated API key
2. **Firewall**: Restrict access by IP if possible
3. **SSL/TLS**: Use Cloud Load Balancer with SSL for production
4. **Service Account**: Use least-privilege service accounts
5. **VPC**: Deploy in a private VPC for enhanced security
6. **Secrets**: Store API keys in Secret Manager, not environment variables
7. **Updates**: Regularly update the VM and Nexus code

## Advanced: SSL/TLS Setup

For production, set up HTTPS using Cloud Load Balancer:

```bash
# Reserve a static IP
gcloud compute addresses create nexus-ip --global

# Create a managed SSL certificate
gcloud compute ssl-certificates create nexus-cert \
  --domains=nexus.yourdomain.com \
  --global

# Create backend service, URL map, target proxy, and forwarding rule
# (See GCP documentation for complete load balancer setup)
```

## Support

If you encounter issues:
1. Check the [Troubleshooting](#troubleshooting) section
2. Review logs: `sudo journalctl -u nexus-server -f`
3. Open an issue on GitHub: [nexi-lab/nexus/issues](https://github.com/nexi-lab/nexus/issues)

## References

- [GCP Compute Engine Documentation](https://cloud.google.com/compute/docs)
- [GCP Cloud Storage Documentation](https://cloud.google.com/storage/docs)
- [Nexus RPC Server Documentation](../../README.md#remote-nexus-server)

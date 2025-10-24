# Docker Deployment Guide

Complete guide for building and deploying Nexus server using Docker on GCP with GCS backend.

## TL;DR

```bash
# 0. Create VM (first time only)
./scripts/create-vm.sh

# 1. Build from PyPI
gcloud builds submit . --config=cloudbuild-pypi.yaml --project=nexi-lab-888

# 2. Deploy to VM
./scripts/deploy-docker-image.sh

# 3. Verify
curl http://136.117.224.98/health
```

## Server Details

- **Static IP**: `136.117.224.98` (permanent)
- **Server URL**: `http://136.117.224.98` (port 80) or `http://136.117.224.98:8080`
- **VM Instance**: `nexus-server`
- **Zone**: `us-west1-a`
- **Backend**: Google Cloud Storage (GCS)
- **GCS Bucket**: `nexi-hub`
- **Project**: `nexi-lab-888`

## Prerequisites

- GCP project `nexi-lab-888` with Cloud Build enabled
- `gcloud` CLI installed and authenticated
- Package `nexus-ai-fs` published to PyPI
- VM service account with `roles/storage.objectAdmin` on `gs://nexi-hub`

## Step 0: Create VM Instance (First Time Only)

If you don't have a VM yet, create one:

```bash
# Create VM with default settings
./scripts/create-vm.sh

# Or customize
./scripts/create-vm.sh --instance-name my-server --zone us-east1-b --machine-type e2-standard-2
```

This creates a VM with:
- Docker pre-installed (Container-Optimized OS)
- Firewall rules for ports 80 and 8080
- 50GB disk
- Auto-restart policy
- Static IP `136.117.224.98`

## Step 1: Build Docker Image from PyPI

Build the Docker image using the published PyPI package:

```bash
# Build from latest PyPI version
gcloud builds submit . --config=cloudbuild-pypi.yaml --project=nexi-lab-888

# Build from specific PyPI version (e.g., 0.3.9)
gcloud builds submit . --config=cloudbuild-pypi.yaml --substitutions=_VERSION=0.3.9 --project=nexi-lab-888
```

### What This Does

1. Downloads `nexus-ai-fs` from PyPI (not local source code)
2. Builds a Docker image using [Dockerfile.pypi](Dockerfile.pypi)
3. Pushes to Google Container Registry: `gcr.io/nexi-lab-888/nexus-server`
4. Tags with version and `latest`
5. Verifies image health

### Output Images

- `gcr.io/nexi-lab-888/nexus-server:VERSION` (e.g., `0.3.9`)
- `gcr.io/nexi-lab-888/nexus-server:latest`

## Step 2: Deploy to VM

Deploy the built image to your VM:

```bash
# Deploy latest image
./scripts/deploy-docker-image.sh

# Deploy specific version
./scripts/deploy-docker-image.sh --image gcr.io/nexi-lab-888/nexus-server:0.3.9

# Deploy to different instance
./scripts/deploy-docker-image.sh --instance-name my-server --zone us-east1-b
```

### What This Does

1. Authenticates Docker with GCR using VM metadata service
2. Pulls the specified image
3. Stops all running containers
4. Starts new container with GCS backend configuration
5. Sets up port forwarding (80 → 8080)
6. Verifies health check

## GCS Backend Configuration

The server uses Google Cloud Storage as the backend for scalable, durable storage.

### Configuration

- **Backend**: GCS
- **Bucket**: `nexi-hub`
- **Project**: `nexi-lab-888`
- **Authentication**: VM service account (`685112155035-compute@developer.gserviceaccount.com`)

### Environment Variables

The deployment automatically sets:
- `NEXUS_BACKEND=gcs`
- `NEXUS_GCS_BUCKET=nexi-hub`
- `NEXUS_GCS_PROJECT=nexi-lab-888`

### Benefits

- ✅ **Scalable**: No disk space limits
- ✅ **Durable**: 99.999999999% durability
- ✅ **Shared**: Multiple VMs can access the same data
- ✅ **Backup**: Automatic redundancy across regions
- ✅ **Cost-effective**: Pay only for what you store

### Data Storage

- **Content (CAS)**: Stored in GCS bucket `gs://nexi-hub/`
- **Metadata (SQLite)**: Stored locally in `/var/lib/nexus/` for performance

## Verification & Testing

### Health Check

```bash
# Via port 80
curl http://136.117.224.98/health

# Via port 8080
curl http://136.117.224.98:8080/health
```

Expected response:
```json
{"status": "healthy", "service": "nexus-rpc"}
```

### Mount Filesystem

```bash
# Mount remote filesystem
nexus mount /tmp/nexus --remote-url http://136.117.224.98

# Write a file (will be stored in GCS)
echo "Hello from GCS!" > /tmp/nexus/workspace/test.txt

# Read it back
cat /tmp/nexus/workspace/test.txt
```

### Verify in GCS

```bash
# List files in bucket
gsutil ls -r gs://nexi-hub/

# Check bucket size
gsutil du -sh gs://nexi-hub/
```

## Monitoring

### Container Logs

```bash
# View logs
gcloud compute ssh nexus-server --zone=us-west1-a \
  --command='sudo docker logs nexus-container'

# Follow logs
gcloud compute ssh nexus-server --zone=us-west1-a \
  --command='sudo docker logs -f nexus-container'
```

### Container Status

```bash
# Check if container is running
gcloud compute ssh nexus-server --zone=us-west1-a \
  --command='sudo docker ps'

# Restart container
gcloud compute ssh nexus-server --zone=us-west1-a \
  --command='sudo docker restart nexus-container'
```

### GCS Monitoring

```bash
# Check GCS bucket size
gsutil du -sh gs://nexi-hub/

# List files
gsutil ls -r gs://nexi-hub/

# View bucket details
gcloud storage buckets describe gs://nexi-hub --project=nexi-lab-888
```

## Static IP Management

### View Details

```bash
gcloud compute addresses describe nexus-server-ip \
  --project=nexi-lab-888 \
  --region=us-west1
```

### List All Static IPs

```bash
gcloud compute addresses list --project=nexi-lab-888
```

### Cost

Static IPs cost **$0.01/hour** (~$7.30/month) when:
- Assigned to a stopped VM
- Not assigned to any VM

**Free** when attached to a running VM (current setup).

## Files

- **[Dockerfile.pypi](Dockerfile.pypi)** - Dockerfile that installs from PyPI with GCS support
- **[cloudbuild-pypi.yaml](cloudbuild-pypi.yaml)** - GCP Cloud Build configuration
- **[scripts/create-vm.sh](scripts/create-vm.sh)** - VM creation script
- **[scripts/deploy-docker-image.sh](scripts/deploy-docker-image.sh)** - Deployment script

## Troubleshooting

### Container won't start

```bash
# Check logs
gcloud compute ssh nexus-server --zone=us-west1-a \
  --command='sudo docker logs nexus-container'

# Check if port is in use
gcloud compute ssh nexus-server --zone=us-west1-a \
  --command='sudo netstat -tulpn | grep 8080'
```

### GCS authentication issues

```bash
# Verify VM service account has permissions
gcloud storage buckets get-iam-policy gs://nexi-hub --project=nexi-lab-888

# Grant permission if needed
gcloud storage buckets add-iam-policy-binding gs://nexi-hub \
  --member="serviceAccount:685112155035-compute@developer.gserviceaccount.com" \
  --role="roles/storage.objectAdmin" \
  --project=nexi-lab-888
```

### Port forwarding not working

```bash
# Check firewall rules
gcloud compute firewall-rules list --project=nexi-lab-888 | grep nexus

# Verify iptables rules on VM
gcloud compute ssh nexus-server --zone=us-west1-a \
  --command='sudo iptables -t nat -L -n -v'
```

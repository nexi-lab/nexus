# Nexus Server - Docker Deployment Guide

This guide covers deploying Nexus RPC Server using Docker and Docker Compose, including deployment to Google Cloud Platform.

## Table of Contents

- [Quick Start](#quick-start)
- [Local Development](#local-development)
- [Production Deployment](#production-deployment)
- [GCP Deployment with Docker](#gcp-deployment-with-docker)
- [Configuration](#configuration)
- [Management](#management)
- [Troubleshooting](#troubleshooting)

## Quick Start

### Prerequisites

```bash
# Install Docker
# macOS: brew install --cask docker
# Linux: curl -fsSL https://get.docker.com | sh
# Verify installation
docker --version
docker-compose --version
```

### Run with Docker Compose

```bash
# 1. Copy environment file
cp .env.docker.example .env

# 2. Edit .env with your configuration
# At minimum, set a secure API key:
#   NEXUS_API_KEY=$(openssl rand -hex 32)

# 3. Start the server
docker-compose up -d

# 4. Test the server
curl http://localhost:8080/health
```

That's it! The server is now running at `http://localhost:8080`.

## Local Development

### Build and Run

```bash
# Build the image
docker build -t nexus-server:latest .

# Run with default settings
docker run -d \
  --name nexus-server \
  -p 8080:8080 \
  -v nexus-data:/app/data \
  nexus-server:latest

# Run with API key
docker run -d \
  --name nexus-server \
  -p 8080:8080 \
  -e NEXUS_API_KEY="mysecretkey" \
  -v nexus-data:/app/data \
  nexus-server:latest
```

### Development with Docker Compose

```bash
# Start in foreground (see logs)
docker-compose up

# Start in background
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down

# Stop and remove volumes (⚠️ deletes data)
docker-compose down -v
```

### Hot Reload Development

For development with code changes, mount the source directory:

```bash
docker run -d \
  --name nexus-dev \
  -p 8080:8080 \
  -v $(pwd)/src:/app/src \
  -v nexus-data:/app/data \
  -e NEXUS_API_KEY="devkey" \
  nexus-server:latest
```

## Production Deployment

### Using Docker Compose (Recommended)

1. **Prepare Environment**

```bash
# Generate secure API key
export API_KEY=$(openssl rand -hex 32)
echo "Save this: $API_KEY"

# Create .env file
cat > .env <<EOF
PORT=8080
NEXUS_API_KEY=$API_KEY
NEXUS_BACKEND=local
EOF
```

2. **Optional: Configure GCS Backend**

```bash
# Add to .env
cat >> .env <<EOF
NEXUS_BACKEND=gcs
NEXUS_GCS_BUCKET_NAME=your-nexus-bucket
NEXUS_GCS_PROJECT_ID=your-project-id
GCS_CREDENTIALS_PATH=./gcs-credentials.json
GOOGLE_APPLICATION_CREDENTIALS=/app/gcs-credentials.json
EOF

# Place your GCS credentials
cp /path/to/service-account-key.json ./gcs-credentials.json
```

3. **Deploy**

```bash
# Start services
docker-compose up -d

# Verify health
curl http://localhost:8080/health

# View logs
docker-compose logs -f nexus-server
```

### Production Best Practices

#### 1. Use Multi-Stage Build

The Dockerfile already uses multi-stage builds for optimal image size:

```dockerfile
FROM python:3.11-slim as builder
# ... build stage ...

FROM python:3.11-slim
# ... production stage (smaller) ...
```

#### 2. Set Resource Limits

Create `docker-compose.prod.yml`:

```yaml
version: '3.8'

services:
  nexus-server:
    extends:
      file: docker-compose.yml
      service: nexus-server
    deploy:
      resources:
        limits:
          cpus: '2.0'
          memory: 4G
        reservations:
          cpus: '1.0'
          memory: 2G
    restart: always
```

Run with:
```bash
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

#### 3. Enable Log Rotation

Already configured in `docker-compose.yml`:

```yaml
logging:
  driver: "json-file"
  options:
    max-size: "10m"
    max-file: "3"
```

#### 4. Regular Backups

```bash
# Backup data volume
docker run --rm \
  -v nexus-data:/data \
  -v $(pwd)/backups:/backup \
  ubuntu tar czf /backup/nexus-data-$(date +%Y%m%d).tar.gz /data

# Restore from backup
docker run --rm \
  -v nexus-data:/data \
  -v $(pwd)/backups:/backup \
  ubuntu tar xzf /backup/nexus-data-20250119.tar.gz -C /
```

#### 5. Health Monitoring

```bash
# Check container health
docker inspect --format='{{.State.Health.Status}}' nexus-server

# Automated health check with restart
docker-compose up -d
# Docker automatically restarts unhealthy containers
```

## GCP Deployment with Docker

### Method 1: Using Deployment Script (Recommended)

```bash
# Build locally and deploy
./deploy-gcp-docker.sh \
  --project-id your-project-id \
  --api-key $(openssl rand -hex 32) \
  --build-local

# Use pre-built image from GCR
./deploy-gcp-docker.sh \
  --project-id your-project-id \
  --api-key mysecret \
  --registry gcr.io/your-project-id

# With GCS backend
./deploy-gcp-docker.sh \
  --project-id your-project-id \
  --api-key mysecret \
  --gcs-bucket your-nexus-bucket \
  --machine-type e2-standard-2 \
  --build-local
```

### Method 2: Manual GCP Deployment

#### Step 1: Build and Push Image

```bash
# Set project
export PROJECT_ID="your-project-id"
gcloud config set project $PROJECT_ID

# Build image
docker build -t gcr.io/$PROJECT_ID/nexus-server:latest .

# Configure Docker for GCR
gcloud auth configure-docker gcr.io

# Push to Google Container Registry
docker push gcr.io/$PROJECT_ID/nexus-server:latest
```

#### Step 2: Create VM with Container

```bash
# Generate API key
export API_KEY=$(openssl rand -hex 32)

# Create instance
gcloud compute instances create-with-container nexus-docker \
  --zone=us-central1-a \
  --machine-type=e2-medium \
  --boot-disk-size=50GB \
  --container-image=gcr.io/$PROJECT_ID/nexus-server:latest \
  --container-restart-policy=always \
  --container-env=NEXUS_API_KEY=$API_KEY \
  --container-mount-host-path=mount-path=/app/data,host-path=/var/lib/nexus,mode=rw \
  --tags=nexus-server \
  --scopes=cloud-platform

# Create firewall rule
gcloud compute firewall-rules create allow-nexus-8080 \
  --allow=tcp:8080 \
  --target-tags=nexus-server
```

#### Step 3: Get External IP and Test

```bash
# Get external IP
EXTERNAL_IP=$(gcloud compute instances describe nexus-docker \
  --zone=us-central1-a \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)')

# Test
curl http://$EXTERNAL_IP:8080/health
```

### Method 3: Google Cloud Run (Serverless)

```bash
# Deploy to Cloud Run (automatic scaling)
gcloud run deploy nexus-server \
  --image=gcr.io/$PROJECT_ID/nexus-server:latest \
  --platform=managed \
  --region=us-central1 \
  --allow-unauthenticated \
  --port=8080 \
  --memory=2Gi \
  --cpu=2 \
  --set-env-vars=NEXUS_API_KEY=$API_KEY,NEXUS_BACKEND=gcs,NEXUS_GCS_BUCKET_NAME=your-bucket

# Get URL
gcloud run services describe nexus-server \
  --region=us-central1 \
  --format='value(status.url)'
```

**Note**: Cloud Run is better for variable workloads. For constant traffic, Compute Engine with containers is more cost-effective.

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `NEXUS_HOST` | Server bind host | `0.0.0.0` |
| `NEXUS_PORT` | Server port | `8080` |
| `NEXUS_DATA_DIR` | Data directory path | `/app/data` |
| `NEXUS_API_KEY` | API authentication key | none |
| `NEXUS_BACKEND` | Storage backend (`local` or `gcs`) | `local` |
| `NEXUS_GCS_BUCKET_NAME` | GCS bucket name | - |
| `NEXUS_GCS_PROJECT_ID` | GCP project ID | - |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to GCS credentials | - |

### Volume Mounts

| Container Path | Purpose | Recommended Host Path |
|----------------|---------|----------------------|
| `/app/data` | Nexus metadata and local storage | Named volume or `/var/lib/nexus` |
| `/app/gcs-credentials.json` | GCS service account key (optional) | `./gcs-credentials.json` |

## Management

### View Logs

```bash
# Docker Compose
docker-compose logs -f

# Docker directly
docker logs -f nexus-server

# Last 100 lines
docker logs --tail 100 nexus-server

# GCP VM
gcloud compute ssh nexus-docker --zone=us-central1-a \
  --command='sudo docker logs -f $(sudo docker ps -q)'
```

### Restart Container

```bash
# Docker Compose
docker-compose restart

# Docker directly
docker restart nexus-server

# GCP VM
gcloud compute ssh nexus-docker --zone=us-central1-a \
  --command='sudo docker restart $(sudo docker ps -q)'
```

### Update to Latest Version

```bash
# Pull latest image
docker-compose pull

# Recreate containers
docker-compose up -d

# Or in one command
docker-compose pull && docker-compose up -d
```

### Scale Horizontally (Load Balancing)

For high availability, run multiple instances behind a load balancer:

```bash
# docker-compose.scale.yml
version: '3.8'
services:
  nexus-server:
    # ... same config ...
    deploy:
      replicas: 3

  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
    depends_on:
      - nexus-server
```

### Execute Commands in Container

```bash
# Open shell
docker exec -it nexus-server /bin/bash

# Run nexus CLI command
docker exec nexus-server nexus ls /workspace

# Check Python version
docker exec nexus-server python --version
```

## Troubleshooting

### Container Won't Start

```bash
# Check logs
docker logs nexus-server

# Check if port is in use
lsof -i :8080

# Check container status
docker ps -a | grep nexus-server

# Inspect container
docker inspect nexus-server
```

### Permission Issues

If you see permission errors:

```bash
# Fix volume permissions
docker run --rm -v nexus-data:/data alpine chown -R 1000:1000 /data

# Or run as root (not recommended for production)
docker run -d \
  --name nexus-server \
  --user root \
  -p 8080:8080 \
  nexus-server:latest
```

### Out of Memory

```bash
# Check container resource usage
docker stats nexus-server

# Increase memory limit
docker run -d \
  --name nexus-server \
  --memory="4g" \
  --memory-swap="4g" \
  -p 8080:8080 \
  nexus-server:latest
```

### GCS Authentication Issues

```bash
# Verify credentials file
docker exec nexus-server cat /app/gcs-credentials.json

# Test GCS access
docker exec nexus-server python -c "
from google.cloud import storage
client = storage.Client()
buckets = list(client.list_buckets())
print(f'Accessible buckets: {len(buckets)}')
"
```

### Rebuild Image Without Cache

```bash
# Full rebuild
docker build --no-cache -t nexus-server:latest .

# Or with docker-compose
docker-compose build --no-cache
```

## Performance Tuning

### Multi-Stage Build Optimization

Already implemented in the Dockerfile. The builder stage is ~1GB, but the final image is only ~300MB.

### Use BuildKit

```bash
# Enable BuildKit for faster builds
export DOCKER_BUILDKIT=1
docker build -t nexus-server:latest .

# Or with docker-compose
DOCKER_BUILDKIT=1 docker-compose build
```

### Layer Caching

The Dockerfile is optimized for caching:
1. System dependencies (rarely change)
2. Python dependencies (change occasionally)
3. Application code (changes frequently)

### Resource Limits

Set appropriate limits based on your workload:

```yaml
# docker-compose.yml
deploy:
  resources:
    limits:
      cpus: '2.0'
      memory: 4G
    reservations:
      cpus: '0.5'
      memory: 1G
```

## Security Best Practices

1. **Never commit .env files** - Add to `.gitignore`
2. **Use secrets management** - For production, use Docker secrets or GCP Secret Manager
3. **Run as non-root** - Already configured in Dockerfile (`USER nexus`)
4. **Scan images regularly** - Use `docker scan nexus-server:latest`
5. **Keep images updated** - Rebuild regularly to get security patches
6. **Use minimal base images** - Using `python:3.11-slim` (not `alpine` for better compatibility)
7. **Restrict network access** - Use firewall rules and security groups
8. **Enable TLS** - Use reverse proxy (nginx, Caddy) for HTTPS

## Cost Optimization

### Stop Containers When Not in Use

```bash
# Stop but keep data
docker-compose stop

# Start again
docker-compose start

# GCP: Stop VM to save compute costs (keeps disk)
gcloud compute instances stop nexus-docker --zone=us-central1-a
```

### Use Spot/Preemptible Instances

For GCP deployment, use preemptible instances (up to 80% cheaper):

```bash
gcloud compute instances create-with-container nexus-docker \
  --preemptible \
  --container-image=gcr.io/$PROJECT_ID/nexus-server:latest \
  # ... other options ...
```

### Resource Limits

Set appropriate CPU/memory limits to avoid over-provisioning:

```yaml
deploy:
  resources:
    limits:
      cpus: '1.0'  # Adjust based on your needs
      memory: 2G
```

## References

- [Docker Documentation](https://docs.docker.com/)
- [Docker Compose Documentation](https://docs.docker.com/compose/)
- [GCP Container-Optimized OS](https://cloud.google.com/container-optimized-os/docs)
- [Google Container Registry](https://cloud.google.com/container-registry/docs)
- [Nexus RPC Server Documentation](../../README.md#remote-nexus-server)

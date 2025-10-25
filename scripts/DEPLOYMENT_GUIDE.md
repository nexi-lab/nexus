# Nexus Server Deployment Guide

## Quick Start

Deploy nexus-server to GCP with Cloud SQL PostgreSQL metadata store:

```bash
cd nexus
./scripts/deploy-docker-image.sh \
  --cloud-sql-instance nexi-lab-888:us-west1:nexus-hub \
  --db-name nexus \
  --db-user postgres \
  --db-password "Nexus-Hub2025"
```

## Configuration Options

### Backend Storage

**Option 1: GCS Backend (Recommended for Production)**
```bash
./scripts/deploy-docker-image.sh \
  --backend gcs \
  --gcs-bucket nexi-hub \
  --gcs-project nexi-lab-888
```

**Option 2: Local Backend (Development)**
```bash
./scripts/deploy-docker-image.sh \
  --backend local
```

### Metadata Store

**Option 1: PostgreSQL (Recommended for Production)**
```bash
./scripts/deploy-docker-image.sh \
  --cloud-sql-instance nexi-lab-888:us-west1:nexus-hub \
  --db-name nexus \
  --db-user postgres \
  --db-password "Nexus-Hub2025"
```

**Option 2: SQLite (Development/Single Server)**
```bash
./scripts/deploy-docker-image.sh
# No database options = SQLite by default
```

## Complete Deployment Examples

### Production Deployment (GCS + PostgreSQL)
```bash
./scripts/deploy-docker-image.sh \
  --backend gcs \
  --gcs-bucket nexi-hub \
  --gcs-project nexi-lab-888 \
  --cloud-sql-instance nexi-lab-888:us-west1:nexus-hub \
  --db-name nexus \
  --db-user postgres \
  --db-password "Nexus-Hub2025"
```

### Development Deployment (Local + SQLite)
```bash
./scripts/deploy-docker-image.sh \
  --backend local
```

### Custom Configuration
```bash
./scripts/deploy-docker-image.sh \
  --project-id my-project \
  --instance-name my-nexus-vm \
  --zone us-central1-a \
  --port 8080 \
  --backend gcs \
  --gcs-bucket my-nexus-bucket \
  --cloud-sql-instance my-project:us-central1:my-db \
  --db-name nexus \
  --db-user nexus \
  --db-password "secure-password"
```

## All Available Options

```bash
--project-id PROJECT_ID         # GCP project (default: nexi-lab-888)
--instance-name NAME            # VM instance (default: nexus-server)
--zone ZONE                     # GCP zone (default: us-west1-a)
--image IMAGE                   # Docker image (default: gcr.io/$PROJECT_ID/nexus-server:latest)
--port PORT                     # Server port (default: 8080)
--backend BACKEND               # Backend type: local or gcs (default: gcs)
--gcs-bucket BUCKET             # GCS bucket (default: nexi-hub)
--gcs-project PROJECT           # GCS project (default: same as --project-id)
--cloud-sql-instance NAME       # Cloud SQL instance connection name
--db-name NAME                  # Database name (default: nexus)
--db-user USER                  # Database username (default: nexus)
--db-password PASS              # Database password (required if using Cloud SQL)
```

## Architecture

### With PostgreSQL (Production)
```
┌─────────────────────────────────────────┐
│         nexus-server (GCE VM)           │
│                                         │
│  ┌──────────────┐   ┌───────────────┐  │
│  │   nexus      │   │  cloudsql     │  │
│  │  container   │───│  proxy        │  │
│  │              │   │  container    │  │
│  └──────────────┘   └───────────────┘  │
│         │                   │           │
│         │                   │           │
└─────────┼───────────────────┼───────────┘
          │                   │
          │                   │
          ▼                   ▼
   ┌─────────────┐    ┌─────────────┐
   │  GCS Bucket │    │  Cloud SQL  │
   │  (Storage)  │    │ (Metadata)  │
   └─────────────┘    └─────────────┘
```

### With SQLite (Development)
```
┌─────────────────────────────────────────┐
│         nexus-server (GCE VM)           │
│                                         │
│  ┌──────────────┐                      │
│  │   nexus      │                      │
│  │  container   │                      │
│  │              │                      │
│  └──────────────┘                      │
│         │                               │
│         │                               │
└─────────┼───────────────────────────────┘
          │
          │
          ▼
   ┌─────────────┐
   │  GCS Bucket │
   │  (Storage)  │
   └─────────────┘

   SQLite DB stored
   in VM local disk
```

## Verification

After deployment, verify the service:

```bash
# Check health endpoint
curl http://35.230.4.67/health

# Check Docker containers
gcloud compute ssh nexus-server --zone=us-west1-a \
  --command="sudo docker ps"

# View Nexus logs
gcloud compute ssh nexus-server --zone=us-west1-a \
  --command="sudo docker logs nexus-container"

# View Cloud SQL Proxy logs (if using PostgreSQL)
gcloud compute ssh nexus-server --zone=us-west1-a \
  --command="sudo docker logs cloudsql-proxy"
```

## Troubleshooting

### Container not starting
```bash
# SSH into VM
gcloud compute ssh nexus-server --zone=us-west1-a

# Check logs
sudo docker logs nexus-container
sudo docker logs cloudsql-proxy  # If using PostgreSQL

# Check Docker network
sudo docker network ls
sudo docker network inspect nexus-network  # If using PostgreSQL
```

### Database connection issues
```bash
# Verify Cloud SQL instance is running
gcloud sql instances describe nexus-hub --project=nexi-lab-888

# Check Cloud SQL Proxy logs
gcloud compute ssh nexus-server --zone=us-west1-a \
  --command="sudo docker logs cloudsql-proxy"

# Test database connection from VM
gcloud compute ssh nexus-server --zone=us-west1-a
sudo docker exec -it cloudsql-proxy /bin/sh
# Inside container, test connection to PostgreSQL
```

### GCS access issues
```bash
# Verify VM has correct service account permissions
gcloud compute instances describe nexus-server \
  --zone=us-west1-a \
  --format="value(serviceAccounts[].email)"

# Check bucket permissions
gsutil iam get gs://nexi-hub

# Grant Storage Admin role if needed
gcloud projects add-iam-policy-binding nexi-lab-888 \
  --member="serviceAccount:SERVICE_ACCOUNT_EMAIL" \
  --role="roles/storage.admin"
```

## Updating the Deployment

To update the deployment with a new Docker image:

```bash
# Build and push new image
cd nexus
gcloud builds submit . --config=cloudbuild-pypi.yaml --project=nexi-lab-888

# Deploy (will automatically pull latest image)
./scripts/deploy-docker-image.sh \
  --cloud-sql-instance nexi-lab-888:us-west1:nexus-hub \
  --db-name nexus \
  --db-user postgres \
  --db-password "Nexus-Hub2025"
```

## Best Practices

1. **Production**: Always use GCS backend + PostgreSQL metadata store
2. **Development**: Local backend + SQLite is sufficient for testing
3. **Security**: Store database password in Secret Manager, not in scripts
4. **Monitoring**: Set up Cloud Monitoring alerts for container health
5. **Backups**: Enable automated backups for Cloud SQL instance
6. **Networking**: Use VPC peering for better security between VM and Cloud SQL

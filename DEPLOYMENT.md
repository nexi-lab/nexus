# Deploying Nexus Server to GCP

This guide walks you through deploying the Nexus HTTP server to Google Cloud Platform. Two deployment options are available:

1. **Compute Engine VM** (Recommended) - Persistent disk, simpler setup, better for SQLite
2. **Cloud Run** - Serverless, scales to zero, requires database sync

## Prerequisites

1. **GCP Account**: Active GCP project with billing enabled
2. **gcloud CLI**: Installed and authenticated
   ```bash
   gcloud auth login
   gcloud auth application-default login
   ```

---

## Deployment Option 1: Compute Engine VM (Recommended)

### Why Compute Engine?

- **Persistent Storage**: SQLite database stored on persistent disk
- **No Database Sync**: No need for complex database synchronization
- **Always On**: VM runs continuously (can be stopped when not needed)
- **Simple**: Standard Docker deployment on a VM
- **Cost**: ~$13-15/month for e2-small instance

### Quick Deploy to Compute Engine

```bash
# Set required environment variables
export GCP_PROJECT_ID="your-gcp-project-id"
export NEXUS_ACCESS_KEY="your-access-key"
export NEXUS_SECRET_KEY="your-secret-key"
export NEXUS_GCS_BUCKET="your-gcs-bucket-name"

# Optional configuration
export GCP_ZONE="us-central1-a"
export MACHINE_TYPE="e2-small"  # Or e2-medium for better performance

# Deploy!
./deploy-compute-vm.sh
```

The script will:
- Create a Compute Engine VM with Container-Optimized OS
- Configure firewall rules for port 8080
- Install and run Nexus in Docker
- Mount persistent disk for SQLite database
- Output the service URL and connection details

### Managing Your VM

```bash
# View server logs
gcloud compute ssh nexus-server --zone=us-central1-a -- 'docker logs -f nexus-server'

# SSH into the VM
gcloud compute ssh nexus-server --zone=us-central1-a

# Stop the VM (to save costs)
gcloud compute instances stop nexus-server --zone=us-central1-a

# Start the VM
gcloud compute instances start nexus-server --zone=us-central1-a

# Delete the VM
gcloud compute instances delete nexus-server --zone=us-central1-a
```

### Custom Domain and HTTPS (Optional)

For production, you can:
1. Reserve a static IP
2. Point your domain to the IP
3. Use nginx or Caddy as reverse proxy with automatic HTTPS

---

## Deployment Option 2: Cloud Run

### Why Cloud Run?

- **Serverless**: No VM management
- **Auto-scaling**: Scales to zero when not in use
- **Cost**: Pay only for requests (can be cheaper for low usage)
- **Note**: Requires database sync to GCS (more complex)

### Quick Deploy to Cloud Run

```bash
# Set required environment variables
export GCP_PROJECT_ID="your-gcp-project-id"
export NEXUS_ACCESS_KEY="your-access-key"
export NEXUS_SECRET_KEY="your-secret-key"
export NEXUS_GCS_BUCKET="your-gcs-bucket-name"

# Deploy
./deploy-cloudrun.sh
```

The script will:
- Enable required GCP APIs
- Create an Artifact Registry repository
- Build and push the Docker image
- Deploy to Cloud Run with database sync enabled
- Output the service URL and rclone configuration

---

## Manual Deployment (Advanced)

### Manual Compute Engine Deployment

1. **Create a VM**:
   ```bash
   gcloud compute instances create nexus-server \
     --zone=us-central1-a \
     --machine-type=e2-small \
     --boot-disk-size=20GB \
     --image-family=cos-stable \
     --image-project=cos-cloud \
     --scopes=storage-rw
   ```

2. **SSH into VM and run Docker**:
   ```bash
   gcloud compute ssh nexus-server --zone=us-central1-a

   # On the VM:
   docker run -d \
     --name nexus-server \
     --restart unless-stopped \
     -p 8080:8080 \
     -v /var/lib/nexus:/app/data \
     -e NEXUS_ACCESS_KEY=your-key \
     -e NEXUS_SECRET_KEY=your-secret \
     -e NEXUS_STORAGE_BACKEND=gcs \
     -e NEXUS_GCS_BUCKET=your-bucket \
     gcr.io/your-project/nexus-server:latest
   ```

3. **Configure firewall**:
   ```bash
   gcloud compute firewall-rules create allow-nexus \
     --allow tcp:8080 \
     --source-ranges 0.0.0.0/0
   ```

### Manual Cloud Run Deployment

1. **Build the image locally**:
   ```bash
   docker build -t nexus-server .
   ```

2. **Tag and push to Artifact Registry**:
   ```bash
   # Set variables
   PROJECT_ID="your-gcp-project-id"
   REGION="us-central1"

   # Create repository
   gcloud artifacts repositories create nexus \
     --repository-format=docker \
     --location=$REGION

   # Tag image
   docker tag nexus-server \
     $REGION-docker.pkg.dev/$PROJECT_ID/nexus/nexus-server:latest

   # Configure Docker auth
   gcloud auth configure-docker $REGION-docker.pkg.dev

   # Push image
   docker push $REGION-docker.pkg.dev/$PROJECT_ID/nexus/nexus-server:latest
   ```

3. **Deploy to Cloud Run**:
   ```bash
   gcloud run deploy nexus-server \
     --image $REGION-docker.pkg.dev/$PROJECT_ID/nexus/nexus-server:latest \
     --platform managed \
     --region $REGION \
     --allow-unauthenticated \
     --port 8080 \
     --memory 512Mi \
     --set-env-vars NEXUS_ACCESS_KEY=your-key,NEXUS_SECRET_KEY=your-secret
   ```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `NEXUS_HOST` | Server host | `0.0.0.0` |
| `NEXUS_PORT` | Server port | `8080` |
| `NEXUS_BUCKET` | Virtual bucket name | `nexus` |
| `NEXUS_ACCESS_KEY` | Authentication access key | `nexus-key` |
| `NEXUS_SECRET_KEY` | Authentication secret key | `nexus-secret` |
| `NEXUS_STORAGE_BACKEND` | Storage backend (`local` or `gcs`) | `local` |
| `NEXUS_STORAGE_PATH` | Local storage path | `/tmp/nexus-data` |
| `NEXUS_GCS_BUCKET` | GCS bucket name (if using GCS) | - |
| `NEXUS_GCS_PROJECT` | GCS project ID | - |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to GCS credentials JSON | - |
| `NEXUS_DB_SYNC` | Enable SQLite database sync to GCS | `true` |
| `NEXUS_DB_SYNC_INTERVAL` | Database sync interval in seconds | `60` |

### Database Persistence on Cloud Run

**Important**: Cloud Run instances have ephemeral storage. When using the GCS backend, Nexus automatically syncs the SQLite metadata database to GCS for persistence:

- **On Startup**: Downloads the database from `gs://your-bucket/metadata/nexus-metadata.db`
- **During Runtime**: Uploads the database every 60 seconds (configurable)
- **On Shutdown**: Final upload before the container stops

This ensures your metadata (file listings, timestamps, etc.) persists across container restarts, while file content is stored directly in GCS.

**Note**: If multiple Cloud Run instances run simultaneously (high traffic), there's a small risk of database conflicts. For high-concurrency scenarios, consider using Cloud SQL instead of SQLite.

### Using GCS Backend

For production deployments, it's recommended to use Google Cloud Storage:

1. **Create a GCS bucket**:
   ```bash
   gsutil mb -p your-project-id gs://your-nexus-bucket
   ```

2. **Deploy with GCS**:
   ```bash
   export NEXUS_GCS_BUCKET="your-nexus-bucket"
   ./deploy-cloudrun.sh
   ```

Cloud Run automatically provides GCS credentials when the service is deployed in the same project.

## Testing Your Deployment

Once deployed, you'll receive a service URL like `https://nexus-server-xxx-uc.a.run.app`

### Test with curl

```bash
# List objects (will be empty initially)
curl -X GET "https://your-service-url/nexus?list-type=2" \
  --aws-sigv4 "aws:amz:us-east-1:s3" \
  --user "your-access-key:your-secret-key"
```

### Configure rclone

```bash
rclone config create nexus s3 \
  provider=Other \
  endpoint=https://your-service-url \
  access_key_id=your-access-key \
  secret_access_key=your-secret-key \
  force_path_style=true

# Test
rclone ls nexus:
```

## Production Considerations

1. **Storage**: Use GCS backend for persistent, scalable storage
2. **Authentication**: Use strong, randomly generated credentials
3. **Secrets Management**: Store credentials in Secret Manager:
   ```bash
   gcloud run deploy nexus-server \
     --update-secrets=NEXUS_SECRET_KEY=nexus-secret:latest
   ```
4. **Custom Domain**: Map a custom domain to your Cloud Run service
5. **Monitoring**: Enable Cloud Logging and Cloud Monitoring
6. **Scaling**: Adjust `--max-instances` based on your needs
7. **Memory/CPU**: Tune `--memory` and `--cpu` based on workload

## Cost Optimization

Cloud Run pricing is pay-per-use:
- Free tier: 2 million requests/month
- Charged only when handling requests
- No charges when idle

For storage:
- Local storage: Ephemeral, lost on restart (not recommended for production)
- GCS storage: Persistent, charged per GB stored

## Troubleshooting

### View logs
```bash
gcloud run logs read nexus-server --region us-central1
```

### Check service status
```bash
gcloud run services describe nexus-server --region us-central1
```

### Test locally first
```bash
docker build -t nexus-server .
docker run -p 8080:8080 \
  -e NEXUS_ACCESS_KEY=test-key \
  -e NEXUS_SECRET_KEY=test-secret \
  nexus-server
```

## Updating the Deployment

To update after making changes:

```bash
# Rebuild and redeploy
./deploy-cloudrun.sh

# Or update specific settings
gcloud run services update nexus-server \
  --region us-central1 \
  --set-env-vars NEXUS_ACCESS_KEY=new-key
```

## Cleanup

To delete the deployment:

```bash
# Delete Cloud Run service
gcloud run services delete nexus-server --region us-central1

# Delete images
gcloud artifacts repositories delete nexus --location us-central1

# Delete GCS bucket (if created)
gsutil rm -r gs://your-nexus-bucket
```

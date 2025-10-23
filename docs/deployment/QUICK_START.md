# Nexus Server - Deployment Quick Start

Choose your deployment method based on your needs:

## 1. Docker Compose (Recommended for Local/Dev)

**Best for:** Local development, testing, small production deployments

```bash
# 1. Copy environment file
cp .env.docker.example .env

# 2. Generate secure API key
echo "NEXUS_API_KEY=$(openssl rand -hex 32)" >> .env

# 3. Start server
docker-compose up -d

# 4. Test
curl http://localhost:8080/health
```

**Docs:** [Docker Deployment Guide](docs/deployment/DOCKER_DEPLOYMENT.md)

---

## 2. GCP with Docker (Recommended for Production)

**Best for:** Production deployments, automatic scaling, managed infrastructure

```bash
# One command deployment!
../../scripts/deploy-gcp-docker.sh \
  --project-id your-gcp-project \
  --api-key $(openssl rand -hex 32) \
  --machine-type e2-standard-2 \
  --build-local

# With GCS backend
../../scripts/deploy-gcp-docker.sh \
  --project-id your-gcp-project \
  --api-key $(openssl rand -hex 32) \
  --gcs-bucket your-nexus-bucket \
  --machine-type e2-standard-2 \
  --build-local
```

**Docs:** [Docker Deployment Guide](docs/deployment/DOCKER_DEPLOYMENT.md#gcp-deployment-with-docker)

---

## 3. GCP with Direct Install

**Best for:** Custom VM setups, full control over environment

```bash
# Initial deployment
../../scripts/deploy-gcp.sh \
  --project-id your-gcp-project \
  --instance-name nexus-server \
  --api-key $(openssl rand -hex 32) \
  --machine-type e2-standard-2

# Redeploy code to existing instance (recommended for updates)
../../scripts/deploy-gcp.sh \
  --project-id your-gcp-project \
  --instance-name nexus-server \
  --deploy-only
```

**Docs:** [GCP Deployment Guide](docs/deployment/GCP_DEPLOYMENT.md)

---

## 4. Local Direct Install

**Best for:** Development on your local machine

```bash
# 1. Activate virtual environment
source .venv/bin/activate

# 2. Start server
./start-server.sh --api-key mysecret

# Or manually
python -m nexus.cli serve --host localhost --port 8080
```

**Docs:** [README - Remote Nexus Server](README.md#remote-nexus-server)

---

## Feature Comparison

| Method | Setup Time | Cost | Scalability | Best For |
|--------|-----------|------|-------------|----------|
| **Docker Compose** | 2 minutes | Free | Manual | Local dev, testing |
| **GCP + Docker** | 5 minutes | ~$27/month | Easy | Production (recommended) |
| **GCP Direct** | 5 minutes | ~$27/month | Easy | Production, custom setup |
| **Local Install** | 1 minute | Free | N/A | Development |

---

## Quick Commands Reference

### Testing

```bash
# Health check
curl http://localhost:8080/health

# Status check
curl http://localhost:8080/api/nfs/status

# List files (with API key)
curl -X POST http://localhost:8080/api/nfs/list \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR-API-KEY" \
  -d '{"jsonrpc": "2.0", "method": "list", "params": {"path": "/"}, "id": 1}'
```

### Management

```bash
# Docker Compose
docker-compose logs -f          # View logs
docker-compose restart          # Restart
docker-compose down             # Stop
docker-compose pull && docker-compose up -d  # Update

# GCP + Docker
gcloud compute ssh instance-name --zone=us-central1-a \
  --command='sudo docker logs -f $(sudo docker ps -q)'  # View logs

# GCP Direct Install
gcloud compute ssh nexus-server --zone=us-west1-a \
  --command='sudo journalctl -u nexus-server -f'  # View logs
gcloud compute ssh nexus-server --zone=us-west1-a \
  --command='sudo systemctl restart nexus-server'  # Restart
../../scripts/deploy-gcp.sh --project-id PROJECT_ID --instance-name nexus-server --deploy-only  # Redeploy code

# Local
./start-server.sh               # Start
lsof -ti:8080 | xargs kill -9  # Stop
```

---

## Need Help?

- [Full Documentation](README.md)
- [Docker Guide](docs/deployment/DOCKER_DEPLOYMENT.md)
- [GCP Guide](docs/deployment/GCP_DEPLOYMENT.md)
- [GitHub Issues](https://github.com/nexi-lab/nexus/issues)

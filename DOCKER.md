# Nexus Docker Development Environment

This directory contains a complete Docker setup for running the Nexus development environment with all services containerized.

## Overview

The Docker setup includes 4 services:

1. **PostgreSQL** - Database for metadata and authentication
2. **Nexus Server** - Core RPC server with file system operations
3. **LangGraph** - AI agent runtime with tool integrations
4. **Frontend** - React-based web UI

## Quick Start

### Prerequisites

- Docker Desktop (Mac/Windows) or Docker Engine (Linux)
- Docker Compose v2.0+
- API keys for LLM providers (Anthropic/OpenAI)

### 1. Setup Environment

```bash
# Copy example environment file
cp .env.example .env

# Edit .env and add your API keys
# Required:
#   - ANTHROPIC_API_KEY
#   - OPENAI_API_KEY
nano .env
```

### 2. Start Services

```bash
# Simple start (recommended)
./docker-start.sh

# Or using docker compose directly
docker compose -f docker-compose.demo.yml up -d
```

### 3. Get Admin API Key

On first startup, Nexus automatically creates an admin API key. Retrieve it from the logs:

```bash
# View Nexus server logs
docker logs nexus-server

# Or grep for the API key specifically
docker logs nexus-server 2>&1 | grep "API Key:"
```

The output will show:
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ADMIN API KEY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  User:    admin
  API Key: nxk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

  To use this key:
    export NEXUS_API_KEY='nxk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
    export NEXUS_URL='http://localhost:8080'
```

**Save this API key** - you'll need it to authenticate with the Nexus API.

### 4. Access Services

- **Frontend**: http://localhost:5173
- **Nexus API**: http://localhost:8080
- **LangGraph**: http://localhost:2024
- **PostgreSQL**: localhost:5432

## docker-start.sh Usage

The `docker-start.sh` script provides a convenient interface to manage the Docker environment:

```bash
# Start all services (default)
./docker-start.sh

# Rebuild images and start
./docker-start.sh --build

# View logs in real-time
./docker-start.sh --logs

# Check service status
./docker-start.sh --status

# Restart all services
./docker-start.sh --restart

# Stop all services
./docker-start.sh --stop

# Clean everything (remove volumes/data)
./docker-start.sh --clean

# Full initialization (clean + build + start)
./docker-start.sh --init

# Show help
./docker-start.sh --help
```

## Docker Compose Commands

For more control, use `docker compose` directly:

```bash
# Start services
docker compose -f docker-compose.demo.yml up -d

# Stop services
docker compose -f docker-compose.demo.yml down

# View logs (all services)
docker compose -f docker-compose.demo.yml logs -f

# View logs (specific service)
docker compose -f docker-compose.demo.yml logs -f nexus

# Rebuild specific service
docker compose -f docker-compose.demo.yml build nexus

# Restart specific service
docker compose -f docker-compose.demo.yml restart nexus

# Execute command in container
docker compose -f docker-compose.demo.yml exec nexus sh

# Scale services (e.g., multiple workers)
docker compose -f docker-compose.demo.yml up -d --scale nexus=3
```

## Service Details

### PostgreSQL

- **Image**: `postgres:15-alpine`
- **Port**: 5432
- **Database**: nexus
- **User**: postgres
- **Password**: nexus (configurable in .env)
- **Data**: Persisted in Docker volume `postgres-data`

**Access database:**
```bash
docker exec -it nexus-postgres psql -U postgres -d nexus
```

### Nexus Server

- **Image**: Built from [Dockerfile](./Dockerfile)
- **Port**: 8080
- **Backend**: Local file system (configurable to GCS)
- **Database**: PostgreSQL
- **Data**: Persisted in Docker volume `nexus-data`

**Environment variables:**
- `NEXUS_DATABASE_URL` - PostgreSQL connection string
- `NEXUS_API_KEY` - Admin API key (auto-generated if not provided)
- `NEXUS_BACKEND` - Storage backend (local/gcs)
- `NEXUS_GCS_BUCKET` - GCS bucket name (if backend=gcs)

**View logs:**
```bash
docker logs -f nexus-server
```

**Shell access:**
```bash
docker exec -it nexus-server sh
```

### LangGraph

- **Image**: Built from [examples/langgraph/Dockerfile](./examples/langgraph/Dockerfile)
- **Port**: 2024
- **Dependencies**: Nexus server (auto-configured)

**Environment variables:**
- `NEXUS_SERVER_URL` - Nexus API URL (http://nexus:8080)
- `ANTHROPIC_API_KEY` - Anthropic API key (required)
- `OPENAI_API_KEY` - OpenAI API key (required)
- `TAVILY_API_KEY` - Tavily search API key (optional)
- `E2B_API_KEY` - E2B code execution API key (optional)

**View logs:**
```bash
docker logs -f nexus-langgraph
```

### Frontend

- **Image**: Built from [nexus-frontend/Dockerfile](../nexus-frontend/Dockerfile)
- **Port**: 5173 (mapped to container port 80)
- **Server**: Nginx
- **Build**: React + Vite

**Environment variables:**
- `VITE_NEXUS_API_URL` - Nexus backend URL
- `VITE_LANGGRAPH_API_URL` - LangGraph API URL

**View logs:**
```bash
docker logs -f nexus-frontend
```

## Networking

All services run on a custom bridge network `nexus-network`, allowing them to communicate using service names:

- `postgres:5432` - PostgreSQL
- `nexus:8080` - Nexus server
- `langgraph:2024` - LangGraph server
- `frontend:80` - Frontend (internal)

External access uses `localhost` with mapped ports.

## Data Persistence

Data is persisted using Docker volumes:

- **postgres-data**: PostgreSQL database files
- **nexus-data**: Nexus file system data (local backend)

**View volumes:**
```bash
docker volume ls
```

**Inspect volume:**
```bash
docker volume inspect nexus_postgres-data
```

**Remove volumes (⚠️ deletes all data):**
```bash
docker compose -f docker-compose.demo.yml down -v
```

## Health Checks

All services include health checks:

```bash
# Check Nexus
curl http://localhost:8080/health

# Check Frontend
curl http://localhost:5173/health

# Check LangGraph
curl http://localhost:2024/health

# Check PostgreSQL
docker exec nexus-postgres pg_isready -U postgres
```

## Troubleshooting

### "Invalid or missing API key" error

If you get an RPC error about missing API key:

```bash
# 1. Get the API key from logs
docker logs nexus-server 2>&1 | grep "API Key:"

# 2. Set it in your environment
export NEXUS_API_KEY='nxk_your_key_here'

# 3. Or add it to .env file
echo "NEXUS_API_KEY=nxk_your_key_here" >> .env

# 4. Restart if you modified .env
docker compose -f docker-compose.demo.yml restart nexus
```

The API key is also saved in the container at `/app/data/.admin-api-key`:

```bash
# Retrieve from container filesystem
docker exec nexus-server cat /app/data/.admin-api-key
```

### Service won't start

```bash
# Check service status
docker compose -f docker-compose.demo.yml ps

# View logs
docker compose -f docker-compose.demo.yml logs nexus

# Check health
docker compose -f docker-compose.demo.yml exec nexus curl http://localhost:8080/health
```

### Database connection issues

```bash
# Check PostgreSQL is running
docker compose -f docker-compose.demo.yml ps postgres

# Test connection
docker exec -it nexus-postgres psql -U postgres -d nexus -c "SELECT 1;"

# Check Nexus database URL
docker compose -f docker-compose.demo.yml exec nexus env | grep DATABASE_URL
```

### Port conflicts

If ports are already in use, edit `.env` to change port mappings:

```bash
# .env
POSTGRES_PORT=5433  # Instead of 5432
NEXUS_PORT=8081     # Instead of 8080
LANGGRAPH_PORT=2025 # Instead of 2024
FRONTEND_PORT=5174  # Instead of 5173
```

### Rebuild specific service

```bash
# Rebuild Nexus server
docker compose -f docker-compose.demo.yml build nexus

# Rebuild and restart
docker compose -f docker-compose.demo.yml up -d --build nexus
```

### Clean start

```bash
# Stop everything and remove volumes
docker compose -f docker-compose.demo.yml down -v

# Remove all images
docker compose -f docker-compose.demo.yml down -v --rmi all

# Rebuild and start fresh
./docker-start.sh --init
```

## Production Deployment

For production deployment, see the main [CLAUDE.md](../.claude/CLAUDE.md) for deployment to GCP with:

- Cloud SQL PostgreSQL
- GCS backend for file storage
- Docker images pushed to GCR
- Deployment to VM instances

## Comparison with demo.sh

| Feature | demo.sh | docker-start.sh |
|---------|---------|-----------------|
| PostgreSQL | Local install | Docker container |
| Nexus | Python venv | Docker container |
| LangGraph | Python venv | Docker container |
| Frontend | npm dev server | Docker container (Nginx) |
| Ngrok | Required | Not needed (local) |
| Dependencies | Manual install | Auto-installed in containers |
| Cleanup | Kill processes | `docker compose down` |
| Data persistence | Local files | Docker volumes |
| Multi-platform | macOS/Linux | Any OS with Docker |

## Files

- `docker-compose.demo.yml` - Main compose configuration
- `docker-start.sh` - Convenience wrapper script
- `.env.example` - Example environment configuration
- `Dockerfile` - Nexus server image
- `examples/langgraph/Dockerfile` - LangGraph server image
- `../nexus-frontend/Dockerfile` - Frontend image
- `DOCKER.md` - This file

## Next Steps

1. **Configure environment**: Edit `.env` with your API keys (ANTHROPIC_API_KEY, OPENAI_API_KEY)
2. **Start services**: Run `./docker-start.sh`
3. **Get admin API key**: Run `docker logs nexus-server 2>&1 | grep "API Key:"`
4. **Save API key**: Export it or save to `.env` file
5. **Access frontend**: Open http://localhost:5173
6. **Check logs**: Run `./docker-start.sh --logs`
7. **Develop**: Services auto-reload on code changes (mount volumes if needed)

For development with live reloading, see [Development Mode](#development-mode).

## Development Mode

To enable live code reloading during development, mount your source code into containers:

```yaml
# Add to docker-compose.demo.yml under 'nexus' service:
volumes:
  - ./src:/app/src:ro
  - nexus-data:/app/data
```

Then restart the service:
```bash
docker compose -f docker-compose.demo.yml restart nexus
```

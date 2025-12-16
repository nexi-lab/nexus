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

- **Docker Desktop** (Mac/Windows) or Docker Engine (Linux)
  - Windows: [Docker Desktop for Windows](https://docs.docker.com/desktop/install/windows-install/) - Make sure WSL 2 backend is enabled
  - Mac: [Docker Desktop for Mac](https://docs.docker.com/desktop/install/mac-install/)
  - Linux: [Docker Engine](https://docs.docker.com/engine/install/)
- **Docker Compose v2.0+** (included with Docker Desktop)
- **Git Bash** (Windows only) - Comes with [Git for Windows](https://gitforwindows.org/)
- **API keys** for LLM providers (Anthropic/OpenAI)

> **Windows Users**: Run all commands in **Git Bash** (not PowerShell or CMD) unless otherwise specified.

### 1. Setup Environment

```bash
# Option 1: Create .env.local (recommended)
cp .env.example .env.local

# Edit .env.local and add your API keys
# Required:
#   - ANTHROPIC_API_KEY (required for LangGraph)
# Optional:
#   - OPENAI_API_KEY (optional for LangGraph)
#   - TAVILY_API_KEY (for web search)
#   - E2B_API_KEY (for cloud sandboxes)
#   - FIRECRAWL_API_KEY (for web scraping)
nano .env.local

# Option 2: Edit .env.example directly
# The script will use .env.example if no .env.local or .env is found
nano .env.example
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

## docker-demo.sh Usage

The `docker-demo.sh` script provides a convenient interface to manage the Docker environment:

```bash
# Start all services (default)
./docker-demo.sh

# Rebuild images and start
./docker-demo.sh --build

# View logs in real-time
./docker-demo.sh --logs

# Check service status
./docker-demo.sh --status

# Restart all services
./docker-demo.sh --restart

# Stop all services
./docker-demo.sh --stop

# Clean everything (remove volumes/data)
./docker-demo.sh --clean

# Full initialization (clean + build + start)
./docker-demo.sh --init

# Show help
./docker-demo.sh --help
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
- `GOOGLE_APPLICATION_CREDENTIALS` - Path to GCS service account credentials

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
curl http://localhost:2024/ok

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

## Using GCS Backend (Google Cloud Storage)

### Setup Service Account (One-time)

Create a service account with long-lived credentials (no daily re-auth):

```bash
# Set your GCP project ID
export PROJECT_ID="your-gcp-project-id"

# Create service account
gcloud iam service-accounts create nexus-storage-sa \
    --display-name="Nexus Storage Service Account" \
    --project=$PROJECT_ID

# Grant Storage Admin permissions
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:nexus-storage-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/storage.admin"

# Download credentials to nexus directory
gcloud iam service-accounts keys create ./gcs-credentials.json \
    --iam-account=nexus-storage-sa@${PROJECT_ID}.iam.gserviceaccount.com

# Verify file was created
ls -lh ./gcs-credentials.json
```

### Configure Docker Environment

Edit your `.env.local` or `.env` file:

```bash
# Enable GCS backend
NEXUS_BACKEND=gcs

# Set your GCS bucket and project
NEXUS_GCS_BUCKET=your-bucket-name
NEXUS_GCS_PROJECT=your-gcp-project-id

# Credentials will be mounted automatically at /app/gcs-credentials.json
GOOGLE_APPLICATION_CREDENTIALS=/app/gcs-credentials.json
```

### Start with GCS Backend

```bash
# The gcs-credentials.json file is automatically mounted by docker-compose
./docker-start.sh --restart
```

The credentials file is:
- ✅ Mounted from `./gcs-credentials.json` to `/app/gcs-credentials.json` in container
- ✅ Excluded from git (in `.gitignore`)
- ✅ Never expires (long-lived service account credentials)
- ✅ No daily re-authentication needed

**Important**: Keep `gcs-credentials.json` secure:
- Never commit to git (already in `.gitignore`)
- Restrict file permissions: `chmod 600 gcs-credentials.json`
- Rotate keys every 90 days (see [GCS_SERVICE_ACCOUNT_SETUP.md](./GCS_SERVICE_ACCOUNT_SETUP.md))

For detailed setup and troubleshooting, see [GCS_SERVICE_ACCOUNT_SETUP.md](./GCS_SERVICE_ACCOUNT_SETUP.md).

## Production Deployment

For production deployment, see the main [CLAUDE.md](../.claude/CLAUDE.md) for deployment to GCP with:

- Cloud SQL PostgreSQL
- GCS backend for file storage (using service account)
- Docker images pushed to GCR
- Deployment to VM instances

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

---

## Windows-Specific Guide

### Prerequisites for Windows

1. **Enable WSL 2**
   ```powershell
   # Run in PowerShell as Administrator
   wsl --install
   # Restart your computer
   ```

2. **Install Docker Desktop**
   - Download from [Docker Desktop for Windows](https://docs.docker.com/desktop/install/windows-install/)
   - During installation, ensure "Use WSL 2 instead of Hyper-V" is selected
   - After installation, open Docker Desktop and wait for it to fully start

3. **Install Git for Windows**
   - Download from [Git for Windows](https://gitforwindows.org/)
   - During installation, select "Git Bash Here" context menu option
   - Use Git Bash for all commands in this guide

### Common Windows Issues

#### 1. Line Ending Issues (CRLF vs LF)

**Problem**: `exec /usr/local/bin/docker-entrypoint.sh: no such file or directory`

**Cause**: Windows uses CRLF (`\r\n`) line endings while Linux/Docker expects LF (`\n`).

**Solutions**:

a) **Automatic Fix** (Recommended): The `docker-demo.sh` script detects and fixes this automatically. Just wait for the rebuild.

b) **Manual Fix**:
```bash
# Using dos2unix (if installed)
dos2unix docker-entrypoint.sh
dos2unix docker-demo.sh

# Or using sed
sed -i 's/\r$//' docker-entrypoint.sh
sed -i 's/\r$//' docker-demo.sh

# Then rebuild
./docker-demo.sh --build
```

c) **Prevention**: Configure Git to not convert line endings:
```bash
# For current repository only
git config core.autocrlf false

# Or globally for all repositories
git config --global core.autocrlf input
```

#### 2. Docker Desktop Not Running

**Problem**: `Cannot connect to the Docker daemon`

**Solutions**:
1. Check if Docker Desktop is running (look for whale icon in system tray)
2. Start Docker Desktop from Start menu
3. Wait 30-60 seconds for Docker to fully initialize
4. Verify: `docker version` should show both client and server info

#### 3. Permission Issues

**Problem**: `permission denied` errors when mounting volumes

**Solutions**:
1. Make sure files are on a drive shared with Docker Desktop:
   - Open Docker Desktop → Settings → Resources → File Sharing
   - Add your project drive (e.g., `C:\`)

2. Run Git Bash as Administrator (right-click → "Run as administrator")

#### 4. Slow Performance

**Problem**: Docker builds or containers run slowly on Windows

**Solutions**:
1. **Increase Docker Desktop Resources**:
   - Open Docker Desktop → Settings → Resources
   - Increase CPUs to at least 4
   - Increase Memory to at least 8GB
   - Increase Disk image size if needed

2. **Use WSL 2 Backend** (not Hyper-V):
   - Docker Desktop → Settings → General
   - Ensure "Use the WSL 2 based engine" is checked

3. **Store Project in WSL Filesystem** (Advanced):
   ```bash
   # Access WSL filesystem
   \wsl$\Ubuntu\home\youruser\nexus

   # Clone directly in WSL
   wsl
   cd ~
   git clone https://github.com/nexi-lab/nexus.git
   ```

#### 5. Path Issues

**Problem**: Scripts can't find files due to Windows vs Unix path differences

**Solutions**:
- Always use forward slashes `/` in scripts
- Use Git Bash which handles path translation automatically
- Avoid spaces in directory names

#### 6. Firewall/Antivirus Blocking Docker

**Problem**: Services fail to start or network connections timeout

**Solutions**:
1. Add Docker Desktop to Windows Firewall exceptions
2. Temporarily disable antivirus to test
3. Add Nexus project folder to antivirus exclusions

### Windows Development Workflow

```bash
# 1. Open Git Bash in project directory
cd /c/Users/youruser/projects/nexus

# 2. Ensure Docker Desktop is running
# Check system tray for Docker icon

# 3. Pull latest changes
git pull

# 4. Start services
./docker-start.sh --build

# 5. Check logs in real-time
./docker-start.sh --logs

# 6. Stop services when done
./docker-start.sh --stop
```

### Using PowerShell (Alternative)

If you prefer PowerShell over Git Bash:

```powershell
# Set environment variables (PowerShell syntax)
$env:NEXUS_API_KEY='your-key-here'
$env:NEXUS_URL='http://localhost:8080'

# Use docker compose directly (bash scripts won't work)
docker compose -f docker-compose.demo.yml up -d --build

# View logs
docker compose -f docker-compose.demo.yml logs -f

# Stop services
docker compose -f docker-compose.demo.yml down
```

### Verifying Windows Setup

Run these commands to verify your setup:

```bash
# 1. Check Docker is running
docker version
# Should show both Client and Server versions

# 2. Check Docker Compose
docker compose version
# Should show v2.x.x or higher

# 3. Check Git Bash
bash --version
# Should show 4.x or higher

# 4. Check line endings config
git config core.autocrlf
# Should show "false" or "input"

# 5. Test Docker networking
curl http://localhost:8080/health
# Should return: {"status": "healthy", "service": "nexus-rpc"}
```

### Windows Performance Tips

1. **Use SSD**: Store project on SSD rather than HDD
2. **Close Unused Apps**: Free up RAM for Docker
3. **Disable Real-time Antivirus Scanning** for project folder
4. **Use Native WSL 2 Storage**: Clone project directly in WSL filesystem for best performance
5. **Restart Docker Desktop** weekly to clear cached data

### Getting Help

If you encounter issues not covered here:

1. **Check Docker Desktop Logs**:
   - Docker Desktop → Troubleshoot → View Logs

2. **Check Windows Event Viewer**:
   - Search for "Event Viewer" in Start menu
   - Windows Logs → Application

3. **Report Issues**:
   - Include your Windows version: `winver`
   - Include Docker Desktop version: `docker version`
   - Include error messages from: `docker logs nexus-server`

---

# Nexus Docker Quick Start

Get Nexus running with Docker in 3 minutes.

## Prerequisites

- **Docker Desktop** (Mac/Windows) or Docker Engine (Linux)
  - Windows: [Docker Desktop for Windows](https://docs.docker.com/desktop/install/windows-install/)
  - Mac: [Docker Desktop for Mac](https://docs.docker.com/desktop/install/mac-install/)
  - Linux: [Docker Engine](https://docs.docker.com/engine/install/)
- **Git Bash** (Windows only) - Comes with [Git for Windows](https://gitforwindows.org/)
- **Anthropic API key** (get from https://console.anthropic.com/)
- **OpenAI API key** (optional, get from https://platform.openai.com/)

> **Windows Users**: This guide uses Bash commands. Run all commands in **Git Bash** (not PowerShell or CMD).

## Clone Repositories

```bash
# 1. Clone Nexus backend
git clone https://github.com/nexi-lab/nexus.git
cd nexus

# 2. Clone frontend (as sibling directory)
cd ..
git clone https://github.com/nexi-lab/nexus-frontend.git
cd nexus  # Back to nexus directory
```

## Start Services

### Step 1: Configure Environment

<details>
<summary><b>Unix/Mac/Linux</b></summary>

```bash
# Copy environment template
cp .env.example .env

# Edit .env and add your API keys
nano .env  # or vim, code, etc.
```
</details>

<details>
<summary><b>Windows (Git Bash)</b></summary>

```bash
# Copy environment template
cp .env.example .env

# Edit .env in your favorite editor
code .env     # VS Code
notepad .env  # Notepad
# or
nano .env     # If you have nano installed
```
</details>

**Required in `.env`:**
- `ANTHROPIC_API_KEY` - Your Anthropic API key
- `OPENAI_API_KEY` - Your OpenAI API key (optional)

### Step 2: Start Services

```bash
# Make sure Docker Desktop is running (Windows/Mac)
# Then start all services
./docker-demo.sh --build
```

> **Windows Note**: If you see a "CRLF line terminator" error, the script will automatically fix it. Just wait for the rebuild to complete.

## Get Your Admin API Key

After services start, retrieve your admin API key:

```bash
docker logs nexus-server 2>&1 | grep "API Key:"
```

You'll see output like:
```
  API Key: nxk_abc123def456...
```

**Save this key!** You'll need it to use Nexus.

## Set Up Environment

<details>
<summary><b>Unix/Mac/Linux</b></summary>

```bash
# Export the API key
export NEXUS_API_KEY='nxk_your_key_here'
export NEXUS_URL='http://localhost:8080'

# Or add to your shell profile (~/.bashrc, ~/.zshrc)
echo "export NEXUS_API_KEY='nxk_your_key_here'" >> ~/.bashrc
echo "export NEXUS_URL='http://localhost:8080'" >> ~/.bashrc
```
</details>

<details>
<summary><b>Windows (Git Bash)</b></summary>

```bash
# For current session (Git Bash)
export NEXUS_API_KEY='nxk_your_key_here'
export NEXUS_URL='http://localhost:8080'

# Or add to ~/.bashrc for persistence
echo "export NEXUS_API_KEY='nxk_your_key_here'" >> ~/.bashrc
echo "export NEXUS_URL='http://localhost:8080'" >> ~/.bashrc
```

**Windows PowerShell Alternative:**
```powershell
$env:NEXUS_API_KEY='nxk_your_key_here'
$env:NEXUS_URL='http://localhost:8080'
```
</details>

## Access Services

- **Web UI**: http://localhost:5173
- **Nexus API**: http://localhost:8080
- **LangGraph**: http://localhost:2024

## Test It Works

```bash
# Check Nexus health
curl http://localhost:8080/health

# List files (requires API key)
curl -H "Authorization: Bearer $NEXUS_API_KEY" \
     http://localhost:8080/list?path=/
```

## Next Steps

- Open the frontend: http://localhost:5173
- Read the full docs: [DOCKER.md](DOCKER.md)
- View logs: `./docker-demo.sh --logs`
- Stop services: `./docker-demo.sh --stop`

## Common Issues

### "Invalid or missing API key"

Get the key from logs:
```bash
docker logs nexus-server 2>&1 | grep "API Key:"
export NEXUS_API_KEY='nxk_...'
```

### "Cannot connect to Docker daemon"

**Windows/Mac:**
- Make sure Docker Desktop is running
- Check system tray (Windows) or menu bar (Mac) for Docker icon
- Wait for Docker to fully start (can take 30-60 seconds)

**Linux:**
```bash
sudo systemctl start docker
```

### "exec /usr/local/bin/docker-entrypoint.sh: no such file or directory"

**Cause**: Windows CRLF line endings in shell scripts.

**Solution**: The script now auto-fixes this, but if needed manually:
```bash
# Install dos2unix if not available
dos2unix docker-entrypoint.sh

# Or use sed
sed -i 's/\r$//' docker-entrypoint.sh

# Then rebuild
./docker-demo.sh --build
```

### Port already in use

Edit `.env` to change ports:
```bash
NEXUS_PORT=8081
FRONTEND_PORT=5174
LANGGRAPH_PORT=2025
```

### Docker build hangs on Windows

If the build seems stuck:
1. Check Docker Desktop resource settings (increase RAM/CPU if needed)
2. Try cleaning Docker build cache: `docker system prune -a`
3. Restart Docker Desktop

## Full Documentation

See [DOCKER.md](DOCKER.md) for complete documentation including:
- Detailed service configuration
- Production deployment
- Development workflow
- Advanced troubleshooting

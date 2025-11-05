# Nexus Docker Quick Start

Get Nexus running with Docker in 3 minutes.

## Prerequisites

- Docker Desktop or Docker Engine
- Anthropic API key (get from https://console.anthropic.com/)
- OpenAI API key (optional, get from https://platform.openai.com/)

## Start Services

```bash
# 1. Copy environment template
cp .env.example .env

# 2. Edit .env and add your API keys
#    Required: ANTHROPIC_API_KEY
#    Optional: OPENAI_API_KEY
nano .env  # or use your favorite editor

# 3. Start all services
./docker-start.sh
```

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

```bash
# Export the API key
export NEXUS_API_KEY='nxk_your_key_here'
export NEXUS_URL='http://localhost:8080'

# Or add to your shell profile (~/.bashrc, ~/.zshrc)
echo "export NEXUS_API_KEY='nxk_your_key_here'" >> ~/.bashrc
echo "export NEXUS_URL='http://localhost:8080'" >> ~/.bashrc
```

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
- View logs: `./docker-start.sh --logs`
- Stop services: `./docker-start.sh --stop`

## Common Issues

### "Invalid or missing API key"

Get the key from logs:
```bash
docker logs nexus-server 2>&1 | grep "API Key:"
export NEXUS_API_KEY='nxk_...'
```

### "Cannot connect to Docker daemon"

Start Docker Desktop or run:
```bash
sudo systemctl start docker  # Linux
```

### Port already in use

Edit `.env` to change ports:
```bash
NEXUS_PORT=8081
FRONTEND_PORT=5174
```

## Full Documentation

See [DOCKER.md](DOCKER.md) for complete documentation including:
- Detailed service configuration
- Production deployment
- Development workflow
- Advanced troubleshooting

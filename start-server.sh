#!/bin/bash
# Nexus RPC Server Startup Script

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}Starting Nexus RPC Server...${NC}"

# Check if virtual environment exists
if [ ! -d ".venv" ]; then
    echo -e "${RED}Error: Virtual environment not found at .venv${NC}"
    echo "Please run: python -m venv .venv && source .venv/bin/activate && pip install -e ."
    exit 1
fi

# Activate virtual environment
source .venv/bin/activate

# Check if nexus is installed
if ! python -c "import nexus" 2>/dev/null; then
    echo -e "${RED}Error: Nexus package not found${NC}"
    echo "Please run: pip install -e ."
    exit 1
fi

# Default values
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8080}"
DATA_DIR="${DATA_DIR:-./nexus-data}"
API_KEY="${API_KEY:-}"

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --host)
            HOST="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --data-dir)
            DATA_DIR="$2"
            shift 2
            ;;
        --api-key)
            API_KEY="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--host HOST] [--port PORT] [--data-dir DIR] [--api-key KEY]"
            exit 1
            ;;
    esac
done

# Create data directory if it doesn't exist
if [ ! -d "$DATA_DIR" ]; then
    echo -e "${BLUE}Creating data directory: $DATA_DIR${NC}"
    mkdir -p "$DATA_DIR"
fi

# Build command
CMD="python -m nexus.cli serve --host $HOST --port $PORT --data-dir $DATA_DIR"

if [ -n "$API_KEY" ]; then
    CMD="$CMD --api-key $API_KEY"
fi

echo -e "${GREEN}Configuration:${NC}"
echo "  Host: $HOST"
echo "  Port: $PORT"
echo "  Data Directory: $DATA_DIR"
if [ -n "$API_KEY" ]; then
    echo "  Authentication: Enabled (API key required)"
else
    echo "  Authentication: Disabled (open access)"
fi
echo ""
echo -e "${GREEN}Starting server...${NC}"
echo "  Endpoint: http://$HOST:$PORT/api/nfs/{method}"
echo "  Health check: http://$HOST:$PORT/health"
echo ""
echo -e "${BLUE}Press Ctrl+C to stop the server${NC}"
echo ""

# Run the server
exec $CMD

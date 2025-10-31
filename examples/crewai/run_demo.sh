#!/bin/bash
# Run CrewAI + Nexus demo
#
# This script provides an easy way to run the CrewAI demo with proper
# environment setup and checks.
#
# Usage:
#   ./run_demo.sh [demo_number]
#
# Examples:
#   ./run_demo.sh        # Interactive mode
#   ./run_demo.sh 1      # Run demo 1 (File Analysis)
#   ./run_demo.sh 2      # Run demo 2 (Research with Memory)
#   ./run_demo.sh 3      # Run demo 3 (Multi-Agent Collaboration)

set -e

# Configuration
NEXUS_URL="${NEXUS_URL:-http://localhost:8080}"

echo "========================================================================"
echo "CrewAI + Nexus MCP Integration Demo"
echo "========================================================================"
echo ""

# Check if API key is set
if [ -z "$ANTHROPIC_API_KEY" ] && [ -z "$OPENAI_API_KEY" ] && [ -z "$OPENROUTER_API_KEY" ]; then
    echo "✗ Error: No LLM API key found"
    echo ""
    echo "Please set one of:"
    echo "  export ANTHROPIC_API_KEY='your-key'"
    echo "  export OPENAI_API_KEY='your-key'"
    echo "  export OPENROUTER_API_KEY='your-key'"
    echo ""
    exit 1
fi

# Check if Nexus server is running
echo "Checking Nexus server at $NEXUS_URL..."
if ! curl -s -f "$NEXUS_URL/health" > /dev/null 2>&1; then
    echo "✗ Error: Nexus server is not running"
    echo ""
    echo "Please start the server first:"
    echo "  Terminal 1: ./start_nexus_server.sh"
    echo "  Terminal 2: ./run_demo.sh"
    echo ""
    exit 1
fi

echo "✓ Nexus server is running"
echo ""

# Export environment
export NEXUS_URL="$NEXUS_URL"

# Run the demo
if [ -n "$1" ]; then
    echo "Running demo $1..."
    echo ""
    echo "$1" | python crewai_nexus_demo.py
else
    echo "Running in interactive mode..."
    echo ""
    python crewai_nexus_demo.py
fi

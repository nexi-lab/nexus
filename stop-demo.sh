#!/bin/bash
# stop-demo.sh - Stop all Nexus demo services

echo "🛑 Stopping Nexus demo services..."
echo ""

# Stop by port
echo "Stopping Nexus backend (port 8080)..."
lsof -ti:8080 | xargs kill -9 2>/dev/null || echo "  (not running)"

echo "Stopping frontend (port 5173)..."
lsof -ti:5173 | xargs kill -9 2>/dev/null || echo "  (not running)"

echo "Stopping LangGraph (port 2024)..."
lsof -ti:2024 | xargs kill -9 2>/dev/null || echo "  (not running)"

echo "Stopping ngrok..."
pkill ngrok 2>/dev/null || echo "  (not running)"

echo ""
echo "✅ All services stopped"

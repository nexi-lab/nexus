#!/bin/bash
# Production deployment script for Nexus on GCP
# This script updates both nexus and nexus-frontend repos, then rebuilds containers

set -e  # Exit on error

NEXUS_REPO="$HOME/nexus"
FRONTEND_REPO="$HOME/nexus-frontend"
COMPOSE_FILE="docker-compose.demo.yml"

echo "🚀 Starting production deployment..."

# Update Nexus main repository
echo ""
echo "📦 Updating Nexus repository..."
cd "$NEXUS_REPO"
git pull origin main
echo "✅ Nexus repository updated"

# Update Frontend repository
echo ""
echo "🎨 Updating Frontend repository..."
cd "$FRONTEND_REPO"
git pull origin main
echo "✅ Frontend repository updated"

# Return to Nexus repo for docker-compose
cd "$NEXUS_REPO"

# Rebuild and restart services
echo ""
echo "🔨 Rebuilding Docker images..."
docker-compose -f "$COMPOSE_FILE" build --no-cache

echo ""
echo "🔄 Restarting services..."
docker-compose -f "$COMPOSE_FILE" up -d

echo ""
echo "⏳ Waiting for services to be healthy..."
sleep 10

# Check service health
echo ""
echo "🏥 Checking service health..."
docker-compose -f "$COMPOSE_FILE" ps

echo ""
echo "✅ Deployment complete!"
echo ""
echo "Service endpoints:"
echo "  - Frontend: http://35.197.30.59:5173"
echo "  - API:      http://35.197.30.59:8080"
echo "  - Health:   http://35.197.30.59:8080/health"

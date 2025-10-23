#!/usr/bin/env bash
#
# Example GCP Deployment Script
# Copy this file to deploy-gcp.local.sh and customize for your needs
#
# Usage:
#   cp deploy-gcp.example.sh deploy-gcp.local.sh
#   # Edit deploy-gcp.local.sh with your values
#   chmod +x deploy-gcp.local.sh
#   ./deploy-gcp.local.sh

set -euo pipefail

# =============================================================================
# CONFIGURATION - Edit these values
# =============================================================================

# Required: Your GCP project ID
PROJECT_ID="your-gcp-project-id"

# Optional: Generate a secure API key
# You can generate one with: openssl rand -hex 32
API_KEY="your-secure-api-key-here"

# Optional: GCS bucket for backend storage
# Leave empty to use local storage
GCS_BUCKET=""  # e.g., "nexus-storage-bucket"

# Instance configuration
INSTANCE_NAME="nexus-server"
ZONE="us-central1-a"
MACHINE_TYPE="e2-medium"
DISK_SIZE="50"
PORT="8080"

# =============================================================================
# RUN DEPLOYMENT
# =============================================================================

echo "Deploying Nexus server with configuration:"
echo "  Project ID: $PROJECT_ID"
echo "  Instance: $INSTANCE_NAME"
echo "  Zone: $ZONE"
echo "  Machine: $MACHINE_TYPE"
echo "  Disk: ${DISK_SIZE}GB"
echo "  Port: $PORT"
if [[ -n "$GCS_BUCKET" ]]; then
    echo "  Backend: GCS (bucket: $GCS_BUCKET)"
else
    echo "  Backend: Local storage"
fi
echo ""

read -p "Continue with deployment? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Deployment cancelled"
    exit 0
fi

# Build command
CMD="./deploy-gcp.sh --project-id $PROJECT_ID"

if [[ -n "$INSTANCE_NAME" ]]; then
    CMD="$CMD --instance-name $INSTANCE_NAME"
fi

if [[ -n "$ZONE" ]]; then
    CMD="$CMD --zone $ZONE"
fi

if [[ -n "$MACHINE_TYPE" ]]; then
    CMD="$CMD --machine-type $MACHINE_TYPE"
fi

if [[ -n "$DISK_SIZE" ]]; then
    CMD="$CMD --disk-size $DISK_SIZE"
fi

if [[ -n "$PORT" ]]; then
    CMD="$CMD --port $PORT"
fi

if [[ -n "$API_KEY" ]]; then
    CMD="$CMD --api-key $API_KEY"
fi

if [[ -n "$GCS_BUCKET" ]]; then
    CMD="$CMD --gcs-bucket $GCS_BUCKET"
fi

# Run deployment
echo "Running: $CMD"
echo ""
eval "$CMD"

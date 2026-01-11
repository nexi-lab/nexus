#!/bin/bash
# Script to save Slack credentials to Google Secret Manager
#
# Prerequisites:
# 1. Install gcloud CLI: https://cloud.google.com/sdk/docs/install
# 2. Authenticate: gcloud auth login
# 3. Set project: gcloud config set project YOUR_PROJECT_ID
#
# Usage:
#   ./scripts/setup_slack_secrets.sh

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to print colored output
print_success() { echo -e "${GREEN}✓${NC} $1"; }
print_error() { echo -e "${RED}✗${NC} $1"; }
print_info() { echo -e "${YELLOW}ℹ${NC} $1"; }

# Check if gcloud is installed
if ! command -v gcloud &> /dev/null; then
    print_error "gcloud CLI not installed"
    echo "Install from: https://cloud.google.com/sdk/docs/install"
    exit 1
fi

print_success "gcloud CLI found"

# Get current project
PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
if [ -z "$PROJECT_ID" ]; then
    print_error "No GCP project configured"
    echo "Set project with: gcloud config set project YOUR_PROJECT_ID"
    exit 1
fi

print_success "Using project: $PROJECT_ID"

# Slack credentials (replace with your actual values)
SLACK_CLIENT_ID="8308475064551.10276416798532"
SLACK_CLIENT_SECRET="8235dc7061624feadd355861ef49731"
SLACK_APP_ID="A0A84C8PGFN"
SLACK_SIGNING_SECRET="3cc6dbee6f6bb71d9c72410b8253ba7b"
SLACK_VERIFICATION_TOKEN="vq78gjgL1psmW8oLJ2Ecb3c7"

echo ""
print_info "Creating secrets in Google Secret Manager..."
echo ""

# Function to create or update secret
create_secret() {
    local secret_name=$1
    local secret_value=$2

    # Check if secret exists
    if gcloud secrets describe "$secret_name" --project="$PROJECT_ID" &>/dev/null; then
        print_info "Updating existing secret: $secret_name"
        echo -n "$secret_value" | gcloud secrets versions add "$secret_name" \
            --data-file=- \
            --project="$PROJECT_ID"
    else
        print_info "Creating new secret: $secret_name"
        echo -n "$secret_value" | gcloud secrets create "$secret_name" \
            --data-file=- \
            --replication-policy="automatic" \
            --project="$PROJECT_ID"
    fi

    if [ $? -eq 0 ]; then
        print_success "Saved: $secret_name"
    else
        print_error "Failed: $secret_name"
    fi
}

# Create secrets
create_secret "nexus-slack-client-id" "$SLACK_CLIENT_ID"
create_secret "nexus-slack-client-secret" "$SLACK_CLIENT_SECRET"
create_secret "nexus-slack-app-id" "$SLACK_APP_ID"
create_secret "nexus-slack-signing-secret" "$SLACK_SIGNING_SECRET"
create_secret "nexus-slack-verification-token" "$SLACK_VERIFICATION_TOKEN"

echo ""
print_success "All secrets saved to Google Secret Manager!"
echo ""
print_info "To access these secrets in your app:"
echo ""
echo "# Python example:"
echo "from google.cloud import secretmanager"
echo ""
echo "client = secretmanager.SecretManagerServiceClient()"
echo "name = f'projects/$PROJECT_ID/secrets/nexus-slack-client-id/versions/latest'"
echo "response = client.access_secret_version(request={'name': name})"
echo "client_id = response.payload.data.decode('UTF-8')"
echo ""
echo "# Or use environment variable:"
echo "export NEXUS_OAUTH_SLACK_CLIENT_ID=\$(gcloud secrets versions access latest --secret=nexus-slack-client-id)"
echo ""
print_info "View secrets in console:"
echo "https://console.cloud.google.com/security/secret-manager?project=$PROJECT_ID"

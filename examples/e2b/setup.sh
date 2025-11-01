#!/bin/bash

# E2B Template Setup Script
# This script automates the creation and deployment of the Nexus AI FS E2B sandbox template

set -e  # Exit on error

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=========================================="
echo "E2B Sandbox Template Setup"
echo "=========================================="
echo ""

# Check if E2B CLI is installed
if ! command -v e2b &> /dev/null; then
    echo "❌ E2B CLI is not installed."
    echo ""
    echo "Please install it using one of the following methods:"
    echo ""
    echo "  Homebrew (macOS):"
    echo "    brew install e2b"
    echo ""
    echo "  NPM:"
    echo "    npm i -g @e2b/cli"
    echo ""
    exit 1
fi

echo "✅ E2B CLI found: $(e2b --version)"
echo ""

# Check if authenticated
echo "Checking E2B authentication..."
if ! e2b auth whoami &> /dev/null; then
    echo "❌ Not authenticated with E2B."
    echo ""
    echo "Starting authentication process..."
    echo "This will open your browser for login."
    echo ""
    e2b auth login
    echo ""
    echo "✅ Authentication successful!"
else
    echo "✅ Already authenticated with E2B"
    e2b auth whoami
fi
echo ""

# Check if Dockerfile exists
if [ ! -f "e2b.Dockerfile" ]; then
    echo "❌ e2b.Dockerfile not found in current directory!"
    exit 1
fi

echo "✅ Found e2b.Dockerfile"
echo ""

# Ask user if they want to build
echo "Ready to build the template with:"
echo "  - Base: e2bdev/code-interpreter:latest"
echo "  - FUSE support"
echo "  - fusepy"
echo "  - nexus-ai-fs"
echo ""
read -p "Build template now? (y/n) " -n 1 -r
echo ""

if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Build cancelled."
    exit 0
fi

echo ""
echo "=========================================="
echo "Building E2B Template..."
echo "=========================================="
echo ""

# Build the template
if [ -f "e2b.toml" ]; then
    echo "Using existing e2b.toml configuration..."
    e2b template build
else
    echo "Building with startup command..."
    e2b template build -c "/root/.jupyter/start-up.sh"
fi

echo ""
echo "=========================================="
echo "✅ Template Build Complete!"
echo "=========================================="
echo ""

# Extract and display template ID
if [ -f "e2b.toml" ]; then
    TEMPLATE_ID=$(grep 'template_id' e2b.toml | cut -d'"' -f2)
    echo "Template ID: $TEMPLATE_ID"
    echo ""
    echo "Python usage:"
    echo "  from e2b import Sandbox"
    echo "  sandbox = Sandbox.create(\"$TEMPLATE_ID\")"
    echo ""
    echo "JavaScript usage:"
    echo "  import { Sandbox } from 'e2b'"
    echo "  const sandbox = await Sandbox.create('$TEMPLATE_ID')"
    echo ""
else
    echo "⚠️  e2b.toml not found. Template ID should be displayed above."
fi

echo "=========================================="
echo "Next Steps:"
echo "=========================================="
echo ""
echo "1. Use the template ID in your code to create sandboxes"
echo "2. To rebuild after changes: e2b template build"
echo "3. To test: e2b sandbox spawn $TEMPLATE_ID"
echo ""
echo "See README.md for more details."
echo ""

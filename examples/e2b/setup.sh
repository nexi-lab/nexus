#!/bin/bash

# E2B Template Setup Script
#
# Usage:
#   ./setup.sh              # Interactive build with local nexus source
#   ./setup.sh --yes        # Non-interactive build with local source
#   ./setup.sh --from-pypi  # Build using nexus-ai-fs from PyPI
#   ./setup.sh --yes --from-pypi  # Non-interactive PyPI build
#
set -e  # Exit on error

# Parse arguments
AUTO_YES=false
FROM_PYPI=false
for arg in "$@"; do
    if [ "$arg" == "--yes" ] || [ "$arg" == "-y" ]; then
        AUTO_YES=true
    elif [ "$arg" == "--from-pypi" ]; then
        FROM_PYPI=true
    fi
done

# this script lives in:  <repo>/examples/e2b/setup.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2B_DIR="$SCRIPT_DIR"                 # .../examples/e2b
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"   # .../   (your repo root)

echo "=========================================="
echo "E2B Sandbox Template Setup"
echo "=========================================="
echo ""
echo "Repo root: $REPO_ROOT"
echo "E2B dir:   $E2B_DIR"
echo ""

# 1) Check E2B CLI
if ! command -v e2b &> /dev/null; then
    echo "❌ E2B CLI is not installed."
    echo "Install with: brew install e2b   OR   npm i -g @e2b/cli"
    exit 1
fi

echo "✅ E2B CLI found: $(e2b --version)"
echo ""

# 2) Check auth
echo "Checking E2B authentication..."
if ! e2b auth whoami &> /dev/null; then
    echo "❌ Not authenticated with E2B. Logging in..."
    e2b auth login
    echo "✅ Authentication successful!"
else
    echo "✅ Already authenticated with E2B"
    e2b auth whoami
fi
echo ""

# 3) Check Dockerfile templates (in examples/e2b)
E2B_DOCKERFILE_LOCAL="$E2B_DIR/e2b.Dockerfile.local"
E2B_DOCKERFILE_PYPI="$E2B_DIR/e2b.Dockerfile.pypi"
E2B_DOCKERFILE="$E2B_DIR/e2b.Dockerfile"

if [ ! -f "$E2B_DOCKERFILE_LOCAL" ]; then
    echo "❌ e2b.Dockerfile.local not found at: $E2B_DOCKERFILE_LOCAL"
    exit 1
fi

if [ ! -f "$E2B_DOCKERFILE_PYPI" ]; then
    echo "❌ e2b.Dockerfile.pypi not found at: $E2B_DOCKERFILE_PYPI"
    exit 1
fi

echo "✅ Found Dockerfile templates (local & PyPI)"
echo ""

if [ "$FROM_PYPI" = true ]; then
    echo "Ready to build the template with:"
    echo "  - mode: PyPI (nexus-ai-fs from PyPI)"
    echo "  - dockerfile: e2b.Dockerfile (using PyPI version)"
    echo "  - startup: /root/.jupyter/start-up.sh"
    echo ""
else
    echo "Ready to build the template with:"
    echo "  - mode: Local source"
    echo "  - source: $REPO_ROOT"
    echo "  - dockerfile: e2b.Dockerfile (using local nexus)"
    echo "  - startup: /root/.jupyter/start-up.sh"
    echo ""
fi

if [ "$AUTO_YES" = false ]; then
    read -p "Build template now? (y/n) " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Build cancelled."
        exit 0
    fi
else
    echo "Auto-confirming build (--yes flag provided)"
fi

echo ""
echo "=========================================="
echo "Building E2B Template..."
echo "=========================================="
echo ""

cd "$E2B_DIR"

if [ "$FROM_PYPI" = true ]; then
    # PyPI mode: Use PyPI Dockerfile (no need to copy nexus)
    echo "Using PyPI mode - skipping nexus copy..."

    # Remove existing nexus symlink or directory if present
    if [ -L "nexus" ]; then
        echo "Removing existing nexus symlink..."
        rm nexus
    elif [ -d "nexus" ]; then
        echo "Removing existing nexus directory..."
        rm -rf nexus
    fi

    echo "✅ PyPI mode configured"
else
    # Local source mode: Copy nexus package for Docker build context
    # Docker COPY cannot follow symlinks outside the build context

    # Remove existing nexus symlink or directory
    if [ -L "nexus" ]; then
        echo "Removing existing nexus symlink..."
        rm nexus
    elif [ -d "nexus" ]; then
        echo "Removing existing nexus directory..."
        rm -rf nexus
    fi

    # Copy nexus package into build context
    echo "Copying nexus package into build context..."
    # REPO_ROOT is the nexus package directory itself, so we copy it
    cp -r "$REPO_ROOT" "$E2B_DIR/nexus"
    echo "✅ Copied nexus package for Docker build"
fi

# Check if e2b.toml exists and has template_id
# Note: e2b.toml is only created after the first build, not by 'e2b template init'
if [ -f "e2b.toml" ]; then
    EXISTING_TEMPLATE_ID=$(grep 'template_id' "e2b.toml" | cut -d'"' -f2 || true)
    if [ -n "$EXISTING_TEMPLATE_ID" ]; then
        echo "✅ Found existing e2b.toml with template_id: $EXISTING_TEMPLATE_ID"

        # Ensure e2b.Dockerfile exists (copy from appropriate source if needed)
        if [ ! -f "e2b.Dockerfile" ]; then
            if [ "$FROM_PYPI" = true ]; then
                echo "Copying e2b.Dockerfile from PyPI template..."
                cp e2b.Dockerfile.pypi e2b.Dockerfile
                echo "✅ Created e2b.Dockerfile (PyPI mode)"
            else
                echo "Copying e2b.Dockerfile from local source template..."
                cp e2b.Dockerfile.local e2b.Dockerfile
                echo "✅ Created e2b.Dockerfile (local source mode)"
            fi
        fi
    else
        echo "⚠️  e2b.toml exists but has no template_id (this shouldn't happen)"
    fi
else
    echo "No e2b.toml found. Setting up E2B template..."

    # If e2b.Dockerfile exists, back it up temporarily
    # e2b template init will create its own skeleton Dockerfile
    if [ -f "e2b.Dockerfile" ]; then
        echo "Backing up existing e2b.Dockerfile..."
        mv e2b.Dockerfile e2b.Dockerfile.backup
    fi

    # Run e2b template init - this creates a skeleton e2b.Dockerfile but no e2b.toml
    echo "Running e2b template init to create project structure..."
    if e2b template init; then
        echo "✅ E2B template initialized (created skeleton files)"
    else
        # Restore backup if init failed
        if [ -f "e2b.Dockerfile.backup" ]; then
            mv e2b.Dockerfile.backup e2b.Dockerfile
        fi
        echo "❌ e2b template init failed"
        exit 1
    fi

    # Replace the generated skeleton Dockerfile with our custom one
    echo "Replacing skeleton Dockerfile with custom configuration..."
    if [ "$FROM_PYPI" = true ]; then
        cp e2b.Dockerfile.pypi e2b.Dockerfile
        echo "✅ Installed custom e2b.Dockerfile (PyPI mode)"
    else
        cp e2b.Dockerfile.local e2b.Dockerfile
        echo "✅ Installed custom e2b.Dockerfile (local source mode)"
    fi

    # Clean up backup
    rm -f e2b.Dockerfile.backup

    # Note: e2b.toml will be created automatically during the build step
    echo "ℹ️  e2b.toml will be created automatically during build"
fi

echo ""
echo "Building from e2b directory..."
echo "Context: $E2B_DIR (includes copied nexus)"
echo "Dockerfile: e2b.Dockerfile"
echo ""

# Build from e2b directory - context includes copied nexus
e2b template build

# Clean up copied nexus directory after build
echo ""
echo "Cleaning up build context..."
if [ -d "$E2B_DIR/nexus" ]; then
    rm -rf "$E2B_DIR/nexus"
    echo "✅ Removed copied nexus directory"
fi

echo ""
echo "=========================================="
echo "✅ Template Build Complete!"
echo "=========================================="
echo ""

# Read template_id from e2b.toml in e2b dir
E2B_TOML="$E2B_DIR/e2b.toml"
if [ -f "$E2B_TOML" ]; then
    TEMPLATE_ID=$(grep 'template_id' "$E2B_TOML" | cut -d'"' -f2 || true)
    if [ -n "$TEMPLATE_ID" ]; then
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
        echo "⚠️ Could not extract template_id from $E2B_TOML"
    fi
else
    echo "⚠️ $E2B_TOML not found. Template ID should have been printed by the CLI."
fi

echo "=========================================="
echo "Next Steps:"
echo "=========================================="
echo "1. To rebuild after code changes: run this script again"
echo "2. The script copies nexus package during build and cleans up after"
echo "3. Your E2B_TEMPLATE_ID: $TEMPLATE_ID"
echo ""

#!/bin/bash
set -e

REPO="nexi-lab/nexus"

echo "Setting up GitHub repository and creating issues..."
echo ""

# Check if gh is installed
if ! command -v gh &> /dev/null; then
    echo "Error: GitHub CLI (gh) is not installed."
    echo "Install it from: https://cli.github.com/"
    exit 1
fi

# Check if authenticated
if ! gh auth status &> /dev/null; then
    echo "Error: Not authenticated with GitHub CLI."
    echo "Run: gh auth login"
    exit 1
fi

# Check if repo exists
if ! gh repo view $REPO &> /dev/null; then
    echo "Repository doesn't exist. Creating..."
    gh repo create $REPO \
        --public \
        --description "AI-Native Distributed Filesystem - Nexus provides a complete AI agent infrastructure platform with embedded mode, document processing, and semantic search" \
        --homepage "https://github.com/$REPO"

    echo "✓ Repository created: https://github.com/$REPO"
    echo ""

    # Push local code to GitHub
    echo "Pushing local code to GitHub..."
    git remote add origin https://github.com/$REPO.git || git remote set-url origin https://github.com/$REPO.git
    git push -u origin main

    echo "✓ Code pushed to GitHub"
    echo ""
else
    echo "✓ Repository already exists: https://github.com/$REPO"
    echo ""
fi

# Create labels
echo "Creating labels..."
./scripts/create-issues.sh --labels-only || echo "Labels may already exist"

echo ""

# Create issues
echo "Creating issues for v0.1.0 and v0.2.0..."
./scripts/create-v0.1-v0.2-issues.sh

echo ""
echo "======================================"
echo "✅ Setup complete!"
echo "======================================"
echo ""
echo "View your repository:"
echo "  https://github.com/$REPO"
echo ""
echo "View issues:"
echo "  https://github.com/$REPO/issues"
echo ""
echo "Start development:"
echo "  gh issue list"
echo "  gh issue view 1"

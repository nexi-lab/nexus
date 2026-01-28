#!/bin/bash
# Setup pre-commit hooks for Windows compatibility
# The default shell-based hook doesn't work reliably in all environments (e.g., Claude Code)
# This script creates a Python-based hook that works everywhere

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK_PATH="$REPO_ROOT/.git/hooks/pre-commit"

echo "Installing pre-commit hooks..."

# First run normal pre-commit install to set up the environment
python -m pre_commit install --install-hooks

# Replace the shell hook with a Python hook for better compatibility
cat > "$HOOK_PATH" << 'HOOK_CONTENT'
#!/usr/bin/env python3
"""Pre-commit hook that runs pre-commit framework.

This Python-based hook replaces the default shell script for better
compatibility with Windows and various shell environments (e.g., Claude Code).
"""
import subprocess
import sys
import os

# Get the repo root (two levels up from .git/hooks)
repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Find the venv Python
if sys.platform == 'win32':
    venv_python = os.path.join(repo_root, '.venv', 'Scripts', 'python.exe')
else:
    venv_python = os.path.join(repo_root, '.venv', 'bin', 'python')

if not os.path.exists(venv_python):
    print(f"ERROR: Could not find venv Python at {venv_python}", file=sys.stderr)
    sys.exit(1)

# Run pre-commit
result = subprocess.run(
    [venv_python, '-m', 'pre_commit', 'run', '--config', '.pre-commit-config.yaml'],
    cwd=repo_root
)
sys.exit(result.returncode)
HOOK_CONTENT

# Make hook executable
chmod +x "$HOOK_PATH"

echo "Done! Pre-commit hooks installed with Python-based hook for cross-platform compatibility."

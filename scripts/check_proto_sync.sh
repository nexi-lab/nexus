#!/bin/bash
# Verify password_vault.proto stays in sync with nexus-vfs SSOT.
# Run in CI to catch drift before it reaches production.
set -euo pipefail

LOCAL="rust/services/proto/nexus/password_vault/v1/password_vault.proto"
REMOTE_URL="https://raw.githubusercontent.com/nexi-lab/nexus-vfs/main/proto/nexus/password_vault/v1/password_vault.proto"

if [ ! -f "$LOCAL" ]; then
    echo "SKIP: $LOCAL not found (service-password-vault feature not present)"
    exit 0
fi

REMOTE=$(curl -sfL "$REMOTE_URL") || { echo "WARN: could not fetch nexus-vfs proto (network); skipping sync check"; exit 0; }

if ! diff <(echo "$REMOTE") "$LOCAL" > /dev/null 2>&1; then
    echo "ERROR: password_vault.proto has drifted from nexus-vfs SSOT"
    echo "  local:  $LOCAL"
    echo "  remote: $REMOTE_URL"
    diff <(echo "$REMOTE") "$LOCAL" || true
    exit 1
fi

echo "OK: password_vault.proto in sync with nexus-vfs"

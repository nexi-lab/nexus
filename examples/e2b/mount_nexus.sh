#!/bin/bash
# Mount remote Nexus server in E2B sandbox
#
# Usage:
#   NEXUS_URL=http://nexus.sudorouter.ai NEXUS_API_KEY=your-key ./mount_nexus.sh
#
# Environment Variables:
#   NEXUS_URL      - Remote Nexus server URL (default: http://nexus.sudorouter.ai)
#   NEXUS_API_KEY  - API key for authentication (optional)
#   MOUNT_PATH     - Local mount point (default: /home/user/nexus)

set -e

# Configuration
NEXUS_URL="${NEXUS_URL:-http://nexus.sudorouter.ai}"
NEXUS_API_KEY="${NEXUS_API_KEY:-}"
MOUNT_PATH="${MOUNT_PATH:-/home/user/nexus}"

# Create mount point if it doesn't exist
mkdir -p "$MOUNT_PATH"

# Build mount command
MOUNT_CMD="nexus mount $MOUNT_PATH --remote $NEXUS_URL --daemon"
if [ -n "$NEXUS_API_KEY" ]; then
    MOUNT_CMD="$MOUNT_CMD --api-key $NEXUS_API_KEY"
fi

# Mount Nexus
echo "Mounting Nexus from $NEXUS_URL to $MOUNT_PATH..."
eval "$MOUNT_CMD"

# Wait a moment for mount to complete
sleep 2

# Verify mount
if mountpoint -q "$MOUNT_PATH" 2>/dev/null || [ "$(ls -A "$MOUNT_PATH" 2>/dev/null)" ]; then
    echo "✓ Nexus mounted successfully"
    echo ""
    echo "Available namespaces:"
    ls -la "$MOUNT_PATH" 2>/dev/null || echo "(Unable to list - mount may still be initializing)"
    echo ""
    echo "You can now use standard Unix tools:"
    echo "  ls $MOUNT_PATH/workspace/"
    echo "  cat $MOUNT_PATH/workspace/file.txt"
    echo "  echo 'content' > $MOUNT_PATH/workspace/newfile.txt"
    echo ""
    echo "To unmount: nexus unmount $MOUNT_PATH"
else
    echo "✗ Mount failed - directory is empty or not accessible"
    exit 1
fi

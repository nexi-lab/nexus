#!/bin/bash

# Nexus rclone mount helper script for macOS
# Usage: ./nexus-mount.sh

set -e

MOUNT_POINT="/Users/jinjingzhou/nexus"
REMOTE="nexus:nexi-lab"
LOG_FILE="/tmp/rclone-nexus.log"
PID_FILE="/tmp/rclone-nexus.pid"

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if already mounted
if mount | grep -q "$MOUNT_POINT"; then
    echo -e "${YELLOW}Warning: $MOUNT_POINT is already mounted${NC}"
    echo "Run ./nexus-unmount.sh first if you want to remount"
    exit 1
fi

# Check if mount point exists
if [ ! -d "$MOUNT_POINT" ]; then
    echo -e "${YELLOW}Creating mount point: $MOUNT_POINT${NC}"
    mkdir -p "$MOUNT_POINT"
fi

# Check if there's a stale PID file
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if ps -p "$OLD_PID" > /dev/null 2>&1; then
        echo -e "${RED}Error: rclone process already running (PID: $OLD_PID)${NC}"
        echo "Run ./nexus-unmount.sh first"
        exit 1
    else
        echo -e "${YELLOW}Removing stale PID file${NC}"
        rm -f "$PID_FILE"
    fi
fi

echo -e "${GREEN}Mounting $REMOTE to $MOUNT_POINT...${NC}"

# Mount with daemon mode for macOS
rclone mount "$REMOTE" "$MOUNT_POINT" \
    --daemon \
    --vfs-cache-mode full \
    --vfs-cache-max-age 24h \
    --vfs-cache-max-size 10G \
    --umask 022 \
    --allow-other \
    --log-level INFO \
    --log-file "$LOG_FILE" \
    --volname "Nexus S3"

# Give it a moment to start
sleep 2

# Find the rclone PID
RCLONE_PID=$(pgrep -f "rclone mount $REMOTE" | head -1)
if [ -n "$RCLONE_PID" ]; then
    echo "$RCLONE_PID" > "$PID_FILE"
    echo -e "${GREEN}✓ Successfully mounted! (PID: $RCLONE_PID)${NC}"
    echo -e "${GREEN}✓ Mount point: $MOUNT_POINT${NC}"
    echo -e "${GREEN}✓ Log file: $LOG_FILE${NC}"
else
    echo -e "${RED}Error: Failed to start rclone mount${NC}"
    echo "Check the log file: $LOG_FILE"
    exit 1
fi

# Verify mount is working
if mount | grep -q "$MOUNT_POINT"; then
    echo -e "${GREEN}✓ Mount verified${NC}"
else
    echo -e "${RED}Error: Mount point not detected${NC}"
    exit 1
fi

echo ""
echo "To unmount, run: ./nexus-unmount.sh"

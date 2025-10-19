#!/bin/bash

# Nexus rclone unmount helper script for macOS
# Usage: ./nexus-unmount.sh

MOUNT_POINT="/Users/jinjingzhou/nexus"
PID_FILE="/tmp/rclone-nexus.pid"

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${YELLOW}Unmounting $MOUNT_POINT...${NC}"

# Check if mounted
if ! mount | grep -q "$MOUNT_POINT"; then
    echo -e "${YELLOW}Warning: $MOUNT_POINT is not currently mounted${NC}"

    # Clean up stale processes anyway
    if [ -f "$PID_FILE" ]; then
        OLD_PID=$(cat "$PID_FILE")
        if ps -p "$OLD_PID" > /dev/null 2>&1; then
            echo -e "${YELLOW}Found stale rclone process (PID: $OLD_PID), killing it...${NC}"
            kill -9 "$OLD_PID" 2>/dev/null || true
        fi
        rm -f "$PID_FILE"
    fi

    # Kill any remaining rclone processes for this mount
    pkill -9 -f "rclone mount nexus:nexi-lab" 2>/dev/null || true

    echo -e "${GREEN}✓ Cleanup complete${NC}"
    exit 0
fi

# Method 1: Try graceful unmount with diskutil (macOS preferred method)
echo "Attempting graceful unmount..."
if diskutil unmount "$MOUNT_POINT" 2>/dev/null; then
    echo -e "${GREEN}✓ Unmounted successfully${NC}"
    SUCCESS=1
else
    echo -e "${YELLOW}Graceful unmount failed, trying force unmount...${NC}"

    # Method 2: Force unmount with diskutil
    if diskutil unmount force "$MOUNT_POINT" 2>/dev/null; then
        echo -e "${GREEN}✓ Force unmounted successfully${NC}"
        SUCCESS=1
    else
        echo -e "${YELLOW}diskutil failed, trying umount...${NC}"

        # Method 3: Try standard umount
        if umount "$MOUNT_POINT" 2>/dev/null; then
            echo -e "${GREEN}✓ Unmounted successfully${NC}"
            SUCCESS=1
        else
            echo -e "${RED}Standard unmount failed, using force...${NC}"

            # Method 4: Force umount (may require sudo)
            if umount -f "$MOUNT_POINT" 2>/dev/null; then
                echo -e "${GREEN}✓ Force unmounted successfully${NC}"
                SUCCESS=1
            else
                echo -e "${RED}All unmount methods failed${NC}"
                SUCCESS=0
            fi
        fi
    fi
fi

# Kill the rclone process
if [ -f "$PID_FILE" ]; then
    RCLONE_PID=$(cat "$PID_FILE")
    if ps -p "$RCLONE_PID" > /dev/null 2>&1; then
        echo "Stopping rclone process (PID: $RCLONE_PID)..."
        kill "$RCLONE_PID" 2>/dev/null || kill -9 "$RCLONE_PID" 2>/dev/null || true
        sleep 1

        # Verify it's stopped
        if ps -p "$RCLONE_PID" > /dev/null 2>&1; then
            echo -e "${YELLOW}Force killing rclone process...${NC}"
            kill -9 "$RCLONE_PID" 2>/dev/null || true
        fi
    fi
    rm -f "$PID_FILE"
fi

# Kill any remaining rclone processes for this mount
pkill -f "rclone mount nexus:nexi-lab" 2>/dev/null || true

# Wait a moment and verify
sleep 1

if mount | grep -q "$MOUNT_POINT"; then
    echo -e "${RED}Error: Mount point still appears to be mounted${NC}"
    echo "You may need to restart your terminal or run:"
    echo "  sudo diskutil unmount force $MOUNT_POINT"
    exit 1
else
    echo -e "${GREEN}✓ All cleanup complete${NC}"
    exit 0
fi

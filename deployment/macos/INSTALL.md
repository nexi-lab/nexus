# Installing Nexus Server as a Background Service (macOS)

This guide explains how to set up the Nexus RPC server as an "Always-On" background service using `launchd`.

## Prerequisites
- Nexus must be installed and initialized (`nexus init`).
- Identify your nexus path: `which nexus`
- Identify your project path (where `src` and `.venv` are located).

## Installation Steps

1. **Copy the template:**
   ```bash
   cp deployment/macos/ai.nexus.server.plist ~/Library/LaunchAgents/
   ```

2. **Configure the plist:**
   Open `~/Library/LaunchAgents/ai.nexus.server.plist` in an editor and replace the following placeholders:
   - `{{NEXUS_PATH}}`: The output of `which nexus` (e.g., `/usr/local/bin/nexus`).
   - `{{USER}}`: Your macOS username.
   - `{{PYTHONPATH}}`: Absolute path to your project `src` directory and site-packages (e.g., `/path/to/nexus/src:/path/to/nexus/.venv/lib/python3.13/site-packages`).

3. **Load the service:**
   ```bash
   launchctl load ~/Library/LaunchAgents/ai.nexus.server.plist
   ```

## Management Commands

- **Check Status:**
  ```bash
  launchctl list | grep nexi
  ```
- **Stop Service:**
  ```bash
  launchctl unload ~/Library/LaunchAgents/ai.nexus.server.plist
  ```
- **View Logs:**
  ```bash
  tail -f ~/.nexus/logs/launcher-stdout.log
  tail -f ~/.nexus/logs/launcher-stderr.log
  ```

## Why run as a background service?
- **Always-On:** Required for real-time features like the Feishu WebSocket Gateway.
- **Auto-Restart:** Automatically restarts if the process crashes.
- **Login Start:** Starts automatically when you log into your Mac.

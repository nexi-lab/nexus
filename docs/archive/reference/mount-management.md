# Mount Management - Quick Reference

**TL;DR:** Dynamic user mounting with persistence. Add/remove backend mounts on-the-fly, stored in database, restored on restart.

---

## 🚀 Quick Start (30 seconds)

### CLI
```bash
# List mounts
nexus mounts list

# Add mount
nexus mounts add /personal/alice google_drive \
  '{"access_token":"ya29.xxx","user_email":"alice@acme.com"}' \
  --owner "google:alice123"

# Remove mount
nexus mounts remove /personal/alice
```

### Python SDK
```python
from nexus import NexusFS, LocalBackend
from nexus.core.mount_manager import MountManager

nx = NexusFS(backend=LocalBackend("/var/nexus"))
manager = MountManager(nx.metadata.SessionLocal)

# Add mount
manager.save_mount(
    mount_point="/personal/alice",
    backend_type="google_drive",
    backend_config={"access_token": "...", "user_email": "..."},
owner_user_id="google:alice123"
)

# List mounts
mounts = manager.list_mounts()
```

---

## 📚 API Reference

### Router Helper Methods
```python
# Check if mount exists
nx.router.has_mount("/personal/alice")  # → bool

# Get mount details
mount = nx.router.get_mount("/personal/alice")  # → MountConfig | None

# Remove mount from router
nx.router.remove_mount("/personal/alice")  # → bool

# List all active mounts
mounts = nx.router.list_mounts()  # → list[MountConfig]
```

### MountManager (Persistence)
```python
# Save mount to database
mount_id = manager.save_mount(
    mount_point="/personal/alice",
    backend_type="google_drive",
    backend_config={"access_token": "...", "user_email": "..."},
readonly=False,
    owner_user_id="google:alice123",
    zone_id="acme",
    description="Alice's Google Drive"
)

# Update mount (e.g., refresh token)
manager.update_mount(
    mount_point="/personal/alice",
    backend_config={"access_token": "new_token", "user_email": "..."}
)

# Get mount from database
mount = manager.get_mount("/personal/alice")  # → dict | None

# List mounts with filtering
all_mounts = manager.list_mounts()
user_mounts = manager.list_mounts(owner_user_id="alice")
zone_mounts = manager.list_mounts(zone_id="acme")

# Remove mount from database
manager.remove_mount("/personal/alice")  # → bool

# Restore mounts on startup
mount_configs = manager.restore_mounts(backend_factory)
for mc in mount_configs:
    nx.router.add_mount(mc.mount_point, mc.backend, mc.readonly)
```

### CLI Commands
```bash
# List all mounts
nexus mounts list

# List with filtering
nexus mounts list --owner "google:alice123"
nexus mounts list --zone "acme"

# JSON output
nexus mounts list --json

# Add mount
nexus mounts add MOUNT_POINT BACKEND_TYPE CONFIG_JSON [OPTIONS]
# Options: --readonly, --owner USER, --zone ZONE, --description TEXT

# Show mount details
nexus mounts info MOUNT_POINT
nexus mounts info MOUNT_POINT --show-config  # Show secrets

# Remove mount
nexus mounts remove MOUNT_POINT
```

---

## 💡 Common Patterns

### Pattern 1: User Login (New User)
```python
def on_user_login(user_id, user_email, google_token, refresh_token):
    mount_point = f"/personal/{user_id}"

    if not manager.get_mount(mount_point):
        # New user - create mount
        manager.save_mount(
            mount_point=mount_point,
            backend_type="google_drive",
            backend_config={
                "access_token": google_token,
                "refresh_token": refresh_token,
                "user_email": user_email
            },
            owner_user_id=user_id
        )

        # Mount immediately
        from your_backends import GoogleDriveBackend
        nx.router.add_mount(
            mount_point,
            GoogleDriveBackend(access_token=google_token, user_email=user_email),
        )
```

### Pattern 2: User Re-Login (Token Refresh)
```python
def on_user_login(user_id, user_email, google_token, refresh_token):
    mount_point = f"/personal/{user_id}"

    if manager.get_mount(mount_point):
        # Existing user - refresh token
        manager.update_mount(
            mount_point,
            backend_config={
                "access_token": google_token,
                "refresh_token": refresh_token,
                "user_email": user_email
            }
        )

        # Update router mount (recreate backend)
        if nx.router.has_mount(mount_point):
            nx.router.remove_mount(mount_point)

        from your_backends import GoogleDriveBackend
        nx.router.add_mount(
            mount_point,
            GoogleDriveBackend(access_token=google_token, user_email=user_email),
        )
```

### Pattern 3: Server Startup (Restore Mounts)
```python
def restore_all_mounts():
    """Called on server startup."""

    def backend_factory(backend_type, config):
        if backend_type == "google_drive":
            from your_backends import GoogleDriveBackend
            return GoogleDriveBackend(**config)
        elif backend_type == "gcs":
            from nexus import GCSBackend
            return GCSBackend(**config)
        elif backend_type == "local":
            from nexus import LocalBackend
            return LocalBackend(**config)
        else:
            raise ValueError(f"Unknown backend: {backend_type}")

    # Restore all mounts
    mount_configs = manager.restore_mounts(backend_factory)

    for mc in mount_configs:
        nx.router.add_mount(mc.mount_point, mc.backend, mc.readonly)
        print(f"✓ Restored: {mc.mount_point}")
```

### Pattern 4: User Leaves (Remove Mount)
```python
def on_user_leave(user_id):
    mount_point = f"/personal/{user_id}"

    # Remove from router
    nx.router.remove_mount(mount_point)

    # Remove from database
    manager.remove_mount(mount_point)
```

---

## 📁 Project Structure

```
nexus/
├── src/nexus/
│   ├── core/
│   │   ├── router.py              # Router helpers: has_mount(), get_mount(), ...
│   │   └── mount_manager.py       # NEW: MountManager class
│   ├── storage/
│   │   └── models.py              # MountConfigModel table
│   └── cli/
│       └── commands/
│           └── mounts.py          # NEW: CLI commands
│
├── examples/
│   ├── script_demo/
│   │   └── mount_management_demo.sh          # CLI demo
│   ├── py_demo/
│   │   └── mount_management_sdk_demo.py      # SDK demo
│   └── MOUNT_MANAGEMENT_EXAMPLES.md          # Examples docs
│
├── MOUNT_MANAGEMENT_COMPLETE.md              # Feature docs
├── IMPLEMENTATION_SUMMARY.md                 # Implementation summary
└── MOUNT_MANAGEMENT_QUICK_REFERENCE.md       # This file
```

---

## 🔍 Debugging

### Check if mount exists
```python
# In router (runtime)
nx.router.has_mount("/personal/alice")

# In database (persisted)
mount = manager.get_mount("/personal/alice")
print(mount is not None)
```

### List all mounts
```python
# Runtime mounts
for m in nx.router.list_mounts():
    print(f"Runtime: {m.mount_point}")

# Persisted mounts
for m in manager.list_mounts():
    print(f"Database: {m['mount_point']}")
```

### Check mount details
```bash
# CLI
nexus mounts info /personal/alice

# SDK
mount = manager.get_mount("/personal/alice")
print(f"Owner: {mount['owner_user_id']}")
print(f"Config: {mount['backend_config']}")
```

---

## ⚠️ Important Notes

### 1. Tokens Expire
```python
# Always store refresh tokens
manager.save_mount(
    ...,
    backend_config={
        "access_token": "...",      # Expires in 1 hour
        "refresh_token": "...",     # ✅ Use this to get new access tokens
        "user_email": "..."
    }
)
```

### 2. Mount != Persistence
```python
# Saving to database doesn't mount to router
manager.save_mount(...)  # → Database only

# You must also mount to router
nx.router.add_mount(...)  # → Runtime

# On restart, restore from database
mount_configs = manager.restore_mounts(backend_factory)
for mc in mount_configs:
    nx.router.add_mount(...)
```

### 3. Backend Config is JSON
```python
# Good: Serializable types
backend_config = {
    "access_token": "string",
    "user_email": "string",
    "bucket_name": "string"
}

# Bad: Non-serializable types
backend_config = {
    "backend_instance": LocalBackend(...)  # ❌ Can't serialize
}
```

---

## 🎯 Use Cases

### ✅ Personal Google Drives
```python
# Mount each user's personal Google Drive when they join
manager.save_mount(
    f"/personal/{user_id}",
    "google_drive",
    {"access_token": "...", "user_email": "..."}
)
```

### ✅ Team Shared Buckets
```python
# Mount team-specific GCS buckets
manager.save_mount(
    f"/teams/{team_id}/bucket",
    "gcs",
    {"bucket_name": f"team-{team_id}-data"}
)
```

### ✅ Multi-Region Storage
```python
# Mount S3 buckets in different regions
manager.save_mount("/us-west", "s3", {"bucket": "data-us-west", "region": "us-west-2"})
manager.save_mount("/eu-central", "s3", {"bucket": "data-eu", "region": "eu-central-1"})
```

### ✅ Legacy Data Migration
```python
# Mount old storage as read-only
manager.save_mount(
    "/legacy/data",
    "local",
    {"root_path": "/mnt/old-storage"},
    readonly=True
)
```

---

## 📚 Learn More

- **Complete Docs:** [MOUNT_MANAGEMENT_COMPLETE.md](MOUNT_MANAGEMENT_COMPLETE.md)
- **Examples:** [examples/MOUNT_MANAGEMENT_EXAMPLES.md](examples/MOUNT_MANAGEMENT_EXAMPLES.md)
- **CLI Demo:** `./examples/script_demo/mount_management_demo.sh`
- **SDK Demo:** `python examples/py_demo/mount_management_sdk_demo.py`
- **CLI Help:** `nexus mounts --help`

---

## 🤝 Support

**Questions?**
- Read the complete docs: `MOUNT_MANAGEMENT_COMPLETE.md`
- Run the examples to see it in action
- Check CLI help: `nexus mounts --help`

**Need help implementing?**
- See integration examples in `MOUNT_MANAGEMENT_EXAMPLES.md`
- Look at the SDK demo for code patterns

---

Made with ❤️ for dynamic user mounting

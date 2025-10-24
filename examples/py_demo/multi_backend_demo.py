#!/usr/bin/env python3
"""
Multi-Backend Demo - Mount Local and GCS backends to different paths.

This demonstrates Nexus's path routing capability:
- Mount local backend to /local
- Mount GCS backend to /cloud
- Access both through a single unified filesystem
"""

from pathlib import Path

from nexus import GCSBackend, LocalBackend, NexusFS

# Create backends
local_backend = LocalBackend(root_path=Path("./nexus-local-data"))
gcs_backend = GCSBackend(
    bucket_name="nexi-hub",
    project_id="nexi-lab-888",
    # credentials_path="path/to/creds.json",  # Optional
)

# Create NexusFS with default local backend
nx = NexusFS(
    backend=local_backend,
    db_path="./nexus-multi-backend.db",
)

# Mount GCS backend to /cloud path
nx.router.add_mount("/cloud", gcs_backend, priority=10)

# Now you can use both backends!
print("Multi-Backend Nexus Filesystem")
print("=" * 50)

# Write to local backend (mounted at /)
print("\n1. Writing to local backend at /local-file.txt")
nx.write("/local-file.txt", b"This is stored locally")

# Write to GCS backend (mounted at /cloud)
print("2. Writing to GCS backend at /cloud/cloud-file.txt")
nx.write("/cloud/cloud-file.txt", b"This is stored in GCS")

# Read from both
print("\n3. Reading from local:")
print(f"   {nx.read('/local-file.txt').decode()}")

print("4. Reading from GCS:")
print(f"   {nx.read('/cloud/cloud-file.txt').decode()}")

# List both
print("\n5. Listing root (local backend):")
for item in nx.ls("/"):
    print(f"   - {item}")

print("\n6. Listing /cloud (GCS backend):")
for item in nx.ls("/cloud"):
    print(f"   - {item}")

print("\n✓ Multi-backend filesystem working!")
print("\nPath Routing:")
print("  /local-file.txt     → Local Backend")
print("  /cloud/*            → GCS Backend")

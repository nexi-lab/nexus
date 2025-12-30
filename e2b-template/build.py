"""Build script for Nexus E2B template."""

import os

from e2b import Template, default_build_logger
from template import template

# Get E2B API key from environment or config
api_key = os.getenv("E2B_API_KEY")
if not api_key:
    # Try to read from E2B config file
    import json

    config_path = os.path.expanduser("~/.e2b/config.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            config = json.load(f)
            api_key = config.get("teamApiKey")

if not api_key:
    raise ValueError("E2B_API_KEY not set and not found in ~/.e2b/config.json")

# Build and push the template
print("🔨 Building Nexus E2B template...")
result = Template.build(
    template,
    alias="nexus-sandbox-latest",
    api_key=api_key,
    on_build_logs=default_build_logger(),
)

print()
print("✅ Template built successfully!")
print(f"   Template ID: {result.template_id}")
print("   Template name: nexus-sandbox-latest")

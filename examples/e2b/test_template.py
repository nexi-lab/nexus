#!/usr/bin/env python3
"""Quick test of the built Nexus E2B template"""

import os

from e2b import Sandbox

# Use our custom-built template
template_id = "7ebpm01g5wtzdvlf75lx"
template_name = "nexus-integration-test"
nexus_url = os.getenv("NEXUS_URL", "http://nexus.sudorouter.ai")
api_key = os.getenv("E2B_API_KEY", "")

print(f"Creating sandbox with template: {template_name} ({template_id})")
print(f"Using API key: {api_key[:10]}..." if api_key else "No API key set")
sandbox = Sandbox.create(template=template_name, api_key=api_key)
print(f"✓ Sandbox created: {sandbox.sandbox_id}")

try:
    # Verify Nexus is installed
    result = sandbox.commands.run("python3.11 -m pip show nexus-ai-fs")
    if result.exit_code == 0:
        print("✓ nexus-ai-fs is installed")
        print(result.stdout)
    else:
        print("✗ nexus-ai-fs not found")
        print(result.stderr)

    # Verify nexus command works
    result = sandbox.commands.run("nexus --help")
    if result.exit_code == 0:
        print("✓ nexus CLI works")
    else:
        print("✗ nexus CLI failed")
        print(result.stderr)

finally:
    sandbox.kill()
    print("✓ Sandbox killed")

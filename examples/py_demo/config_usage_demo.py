"""Demo showing how to use Nexus with configuration files and custom namespaces."""

import tempfile
from pathlib import Path

import nexus


def demo_dict_config():
    """Example 1: Using dict config (programmatic)."""
    print("=" * 70)
    print("Example 1: Dict Config with Custom Namespaces")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        # Configure with dict - useful for dynamic configuration
        nx = nexus.connect(
            config={
                "data_dir": tmpdir,
                "tenant_id": "acme",
                "agent_id": "agent1",
                "is_admin": False,
                "namespaces": [
                    {
                        "name": "analytics",
                        "readonly": False,
                        "admin_only": False,
                        "requires_tenant": True,
                    },
                    {
                        "name": "models",
                        "readonly": False,
                        "admin_only": False,
                        "requires_tenant": True,
                    },
                ],
            }
        )

        print(f"✓ Connected as tenant: {nx.tenant_id}, agent: {nx.agent_id}")
        print(f"✓ Namespaces: {list(nx.router._namespaces.keys())}")

        # Use custom namespaces
        nx.write("/analytics/acme/daily_report.json", b'{"revenue": 50000}')
        print("✓ Wrote to /analytics/acme/daily_report.json")

        nx.write("/models/acme/classifier-v1.pkl", b"model data")
        print("✓ Wrote to /models/acme/classifier-v1.pkl")

        # Also use default namespaces
        nx.write("/workspace/acme/agent1/scratch.txt", b"temp work")
        print("✓ Wrote to /workspace/acme/agent1/scratch.txt")

        # List all files
        files = nx.list()
        print(f"\n✓ Total files created: {len(files)}")
        for f in sorted(files):
            print(f"  - {f}")

        nx.close()


def demo_yaml_config():
    """Example 2: Using YAML config file."""
    print("\n" + "=" * 70)
    print("Example 2: YAML Config File")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create YAML config
        config_file = tmpdir / "nexus.yaml"
        config_file.write_text(
            """
mode: embedded
data_dir: ./nexus-data
tenant_id: acme
agent_id: agent1
is_admin: false

namespaces:
  - name: analytics
    readonly: false
    admin_only: false
    requires_tenant: true

  - name: audit
    readonly: false
    admin_only: true
    requires_tenant: false
"""
        )

        print(f"✓ Created config file: {config_file}")

        # Connect using config file
        nx = nexus.connect(config=str(config_file))

        print("✓ Connected from config file")
        print(f"  tenant_id: {nx.tenant_id}")
        print(f"  agent_id: {nx.agent_id}")
        print("  Custom namespaces: analytics, audit")

        # Test tenant isolation
        nx.write("/analytics/acme/metrics.json", b'{"users": 1000}')
        print("✓ Wrote to /analytics (tenant-isolated)")

        # Try to access audit (should fail - admin only)
        try:
            nx.write("/audit/logs.txt", b"log entry")
            print("✗ Should have failed (audit is admin-only)")
        except Exception as e:
            print(f"✓ Audit access correctly denied: {type(e).__name__}")

        nx.close()


def demo_multi_tenant():
    """Example 3: Multiple tenants with isolation."""
    print("\n" + "=" * 70)
    print("Example 3: Multi-Tenant Isolation")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        # Define shared namespace config
        namespace_config = [
            {"name": "analytics", "readonly": False, "admin_only": False, "requires_tenant": True}
        ]

        # Tenant ACME
        nx_acme = nexus.connect(
            config={
                "data_dir": tmpdir,
                "tenant_id": "acme",
                "namespaces": namespace_config,
            }
        )

        # Tenant TechInc
        nx_tech = nexus.connect(
            config={
                "data_dir": tmpdir,
                "tenant_id": "techinc",
                "namespaces": namespace_config,
            }
        )

        # ACME writes data
        nx_acme.write("/analytics/acme/report.json", b'{"tenant": "acme"}')
        print("✓ ACME wrote to /analytics/acme/report.json")

        # TechInc writes data
        nx_tech.write("/analytics/techinc/report.json", b'{"tenant": "techinc"}')
        print("✓ TechInc wrote to /analytics/techinc/report.json")

        # ACME can read their own data
        acme_data = nx_acme.read("/analytics/acme/report.json")
        print(f"✓ ACME read their own data: {acme_data.decode()}")

        # TechInc CANNOT read ACME's data
        try:
            nx_tech.read("/analytics/acme/report.json")
            print("✗ Should have failed (tenant isolation)")
        except Exception as e:
            print(f"✓ Tenant isolation enforced: {type(e).__name__}")

        nx_acme.close()
        nx_tech.close()


def demo_admin_override():
    """Example 4: Admin access override."""
    print("\n" + "=" * 70)
    print("Example 4: Admin Access Override")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        namespace_config = [
            {"name": "audit", "readonly": False, "admin_only": True, "requires_tenant": False}
        ]

        # Regular user
        nx_user = nexus.connect(
            config={
                "data_dir": tmpdir,
                "tenant_id": "acme",
                "is_admin": False,
                "namespaces": namespace_config,
            }
        )

        # Admin user
        nx_admin = nexus.connect(
            config={
                "data_dir": tmpdir,
                "tenant_id": "admin",
                "is_admin": True,
                "namespaces": namespace_config,
            }
        )

        # User CANNOT write to audit
        try:
            nx_user.write("/audit/access.log", b"user action")
            print("✗ Should have failed (admin-only)")
        except Exception as e:
            print(f"✓ User correctly blocked from /audit: {type(e).__name__}")

        # Admin CAN write to audit
        nx_admin.write("/audit/access.log", b"admin action")
        print("✓ Admin wrote to /audit namespace")

        # Admin can read any tenant's data (bypass tenant isolation)
        nx_user.write("/workspace/acme/agent1/secret.txt", b"acme secret")
        admin_read = nx_admin.read("/workspace/acme/agent1/secret.txt")
        print(f"✓ Admin bypassed tenant isolation: {admin_read.decode()}")

        nx_user.close()
        nx_admin.close()


if __name__ == "__main__":
    print("\nNexus Configuration Demo")
    print("=" * 70)
    print("This demo shows different ways to configure Nexus with custom namespaces")
    print("=" * 70)

    demo_dict_config()
    demo_yaml_config()
    demo_multi_tenant()
    demo_admin_override()

    print("\n" + "=" * 70)
    print("✓ All examples completed successfully!")
    print("=" * 70)
    print("\nKey Takeaways:")
    print("1. Use dict config for programmatic/dynamic configuration")
    print("2. Use YAML config files for declarative configuration")
    print("3. Custom namespaces provide semantic organization + access control")
    print("4. Tenant isolation is automatic when tenant_id is configured")
    print("5. Admin access overrides tenant isolation")

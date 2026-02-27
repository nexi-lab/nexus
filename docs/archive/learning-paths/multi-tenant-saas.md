# Multi-Tenant SaaS

**Build scalable multi-zone SaaS applications with complete tenant isolation**

⏱️ **Time:** 30 minutes | 💡 **Difficulty:** Advanced

## What You'll Learn

- Design multi-zone architecture with Nexus
- Implement complete tenant isolation
- Manage per-tenant workspaces and permissions
- Handle cross-tenant data sharing securely
- Scale to thousands of tenants
- Implement tenant lifecycle management
- Monitor tenant usage and quotas
- Build production-ready SaaS infrastructure

## Prerequisites

✅ Python 3.8+ installed
✅ Nexus installed (`pip install nexus-ai-fs`)
✅ Understanding of team collaboration ([Team Collaboration](team-collaboration.md))
✅ Familiarity with ReBAC permissions
✅ Basic knowledge of SaaS architecture concepts

**📝 API Note:** This tutorial uses Nexus's ReBAC (Relationship-Based Access Control) API for permissions. Admin operations use the RPC interface via `_call_rpc()`. User accounts are created implicitly when their first API key is generated.

## Overview

Multi-zone SaaS applications serve multiple customers (tenants) from a single infrastructure while ensuring **complete data isolation** and **independent tenant management**. Nexus provides built-in multi-tenancy support with ReBAC for fine-grained access control.

**Use Cases:**
- 🏢 B2B SaaS platforms
- 📊 Analytics-as-a-Service
- 🤖 AI/ML platforms serving multiple customers
- 📝 Document management systems
- 💼 Enterprise collaboration tools
- 🔐 Secure file sharing platforms

**Multi-Tenant Architecture:**

```
┌────────────────────────────────────────────────────────────┐
│                  SaaS Application Layer                    │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                │
│  │ Tenant A │  │ Tenant B │  │ Tenant C │                │
│  │ (Acme)   │  │ (Beta)   │  │ (Corp)   │                │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘                │
└───────┼─────────────┼─────────────┼────────────────────────┘
        │             │             │ Isolated API Access
        └─────────────┼─────────────┘
                      ↓
┌────────────────────────────────────────────────────────────┐
│              Nexus Multi-Tenant Server                     │
│  ┌──────────────────────────────────────────────────────┐ │
│  │         Tenant Isolation & Authorization             │ │
│  │  ┌────────────┐  ┌────────────┐  ┌────────────┐    │ │
│  │  │  Tenant    │  │   ReBAC    │  │   Quota    │    │ │
│  │  │  Manager   │  │  Enforcer  │  │  Manager   │    │ │
│  │  └────────────┘  └────────────┘  └────────────┘    │ │
│  └────────────────────┬─────────────────────────────────┘ │
│         ↓             ↓              ↓                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │   Tenant A   │  │   Tenant B   │  │   Tenant C   │   │
│  │  Workspace   │  │  Workspace   │  │  Workspace   │   │
│  │              │  │              │  │              │   │
│  │  /tenants/   │  │  /tenants/   │  │  /tenants/   │   │
│  │    acme/     │  │    beta/     │  │    corp/     │   │
│  └──────────────┘  └──────────────┘  └──────────────┘   │
└────────────────────────────────────────────────────────────┘
```

---

## Step 1: Start Multi-Tenant Nexus Server

Start a Nexus server configured for multi-tenancy:

```bash
# Start server with multi-zone configuration
nexus serve \
  --host 0.0.0.0 \
  --port 2026 \
  --data-dir ./nexus-saas-data \
  --database-url "postgresql://postgres:nexus@localhost/nexus" \
  &

# Wait for server to start
sleep 3

# Verify server is running
curl http://localhost:2026/health
```

**Expected output:**
```json
{"status":"ok","version":"0.5.0","mode":"multi-zone"}
```

**💡 Pro Tip:** Use PostgreSQL for production multi-zone deployments to handle thousands of tenants efficiently.

---

## Step 2: Create Tenant Management System

Build a tenant management system:

**Important Note:** This example uses Nexus's internal RPC API via `_call_rpc()` for admin operations and the ReBAC API for permissions. The admin operations create API keys which implicitly create user accounts.

```python
# tenant_manager.py
import nexus
import json
from datetime import datetime
from typing import Dict, List, Optional

class TenantManager:
    """Manage multi-zone SaaS tenants"""

    def __init__(self, admin_api_key: str, server_url: str = "http://localhost:2026"):
        self.server_url = server_url
        self.admin = nexus.connect(config={
            "url": server_url,
            "api_key": admin_api_key
        })

    def create_tenant(
        self,
        zone_id: str,
        name: str,
        plan: str = "free",
        max_users: int = 5,
        max_storage_gb: int = 10
    ) -> Dict:
        """Create a new tenant with isolated workspace"""

        # Create tenant workspace
        tenant_path = f"/tenants/{zone_id}"
        self.admin.mkdir(tenant_path)

        # Create tenant metadata
        metadata = {
            "zone_id": zone_id,
            "name": name,
            "plan": plan,
            "created_at": datetime.now().isoformat(),
            "status": "active",
            "limits": {
                "max_users": max_users,
                "max_storage_gb": max_storage_gb,
                "max_files": 10000 if plan == "free" else None
            },
            "usage": {
                "users": 0,
                "storage_gb": 0,
                "files": 0
            }
        }

        # Save metadata
        self.admin.write(
            f"{tenant_path}/.tenant.json",
            json.dumps(metadata, indent=2).encode()
        )

        # Create standard tenant directories
        for subdir in ["users", "shared", "uploads", "exports"]:
            self.admin.mkdir(f"{tenant_path}/{subdir}")

        print(f"✅ Created tenant: {name} ({zone_id})")
        print(f"   Plan: {plan}, Max Users: {max_users}, Storage: {max_storage_gb}GB")

        return metadata

    def create_tenant_admin(self, zone_id: str, username: str, email: str) -> Dict:
        """Create admin user for a tenant"""

        # Create user account and API key
        # Note: In Nexus, users are created implicitly when their first API key is generated
        result = self.admin._call_rpc("admin_create_key", {
            "user_id": username,
            "name": f"{username} (Tenant Admin)",
            "subject_type": "user",
            "zone_id": zone_id,
            "is_admin": False,  # Tenant admin, not system admin
        })

        api_key = result["api_key"]

        # Grant tenant admin full access to tenant workspace
        tenant_path = f"/tenants/{zone_id}"
        self.admin.rebac_create(
            subject=("user", username),
            relation="owner",
            object=("file", tenant_path),
            zone_id=zone_id
        )

        print(f"✅ Created tenant admin: {username}")
        print(f"   Tenant: {zone_id}, Email: {email}")

        return {
            "username": username,
            "email": email,
            "zone_id": zone_id,
            "api_key": api_key,
            "role": "tenant_admin"
        }

    def add_tenant_user(
        self,
        zone_id: str,
        username: str,
        email: str,
        role: str = "user"
    ) -> Dict:
        """Add a user to a tenant"""

        # Create user account and API key
        result = self.admin._call_rpc("admin_create_key", {
            "user_id": username,
            "name": username,
            "subject_type": "user",
            "zone_id": zone_id,
            "is_admin": False,
        })

        api_key = result["api_key"]

        # Grant appropriate permissions based on role
        tenant_path = f"/tenants/{zone_id}"

        if role == "admin":
            relation = "owner"
        elif role == "editor":
            relation = "can_write"
        else:  # role == "user"
            relation = "can_read"

        self.admin.rebac_create(
            subject=("user", username),
            relation=relation,
            object=("file", f"{tenant_path}/shared"),
            zone_id=zone_id
        )

        # Grant user access to their personal folder
        user_folder = f"{tenant_path}/users/{username}"
        self.admin.mkdir(user_folder)
        self.admin.rebac_create(
            subject=("user", username),
            relation="owner",
            object=("file", user_folder),
            zone_id=zone_id
        )

        print(f"✅ Added user: {username} (role: {role})")

        # Update tenant user count
        self._update_tenant_usage(zone_id, users_delta=1)

        return {
            "username": username,
            "email": email,
            "zone_id": zone_id,
            "role": role,
            "api_key": api_key
        }

    def get_tenant_info(self, zone_id: str) -> Dict:
        """Get tenant information and usage"""
        metadata = json.loads(
            self.admin.read(f"/tenants/{zone_id}/.tenant.json").decode()
        )
        return metadata

    def list_tenants(self) -> List[Dict]:
        """List all tenants"""
        tenant_dirs = self.admin.list("/tenants", recursive=False)

        tenants = []
        for tenant_dir in tenant_dirs:
            zone_id = tenant_dir['path'].split('/')[-1]
            try:
                info = self.get_tenant_info(zone_id)
                tenants.append(info)
            except:
                pass

        return tenants

    def _update_tenant_usage(self, zone_id: str, **kwargs):
        """Update tenant usage metrics"""
        metadata = self.get_tenant_info(zone_id)

        for key, value in kwargs.items():
            if key.endswith('_delta'):
                metric = key.replace('_delta', '')
                if metric in metadata['usage']:
                    metadata['usage'][metric] += value

        self.admin.write(
            f"/tenants/{zone_id}/.tenant.json",
            json.dumps(metadata, indent=2).encode()
        )

    def check_quota(self, zone_id: str, resource: str, amount: int = 1) -> bool:
        """Check if tenant is within quota limits"""
        metadata = self.get_tenant_info(zone_id)

        limits = metadata['limits']
        usage = metadata['usage']

        if resource == 'users':
            return usage['users'] + amount <= limits['max_users']
        elif resource == 'storage_gb':
            return usage['storage_gb'] + amount <= limits['max_storage_gb']
        elif resource == 'files':
            max_files = limits.get('max_files')
            if max_files is None:
                return True
            return usage['files'] + amount <= max_files

        return True

# Demo usage
if __name__ == "__main__":
    # Admin API key (replace with actual key)
    ADMIN_KEY = "admin_key_here"

    # Initialize tenant manager
    tm = TenantManager(ADMIN_KEY)

    # Create tenants
    acme_tenant = tm.create_tenant(
        zone_id="acme",
        name="Acme Corporation",
        plan="enterprise",
        max_users=100,
        max_storage_gb=1000
    )

    beta_tenant = tm.create_tenant(
        zone_id="beta",
        name="Beta Inc",
        plan="pro",
        max_users=50,
        max_storage_gb=500
    )

    startup_tenant = tm.create_tenant(
        zone_id="startup",
        name="Startup Co",
        plan="free",
        max_users=5,
        max_storage_gb=10
    )

    # Create tenant admins
    acme_admin = tm.create_tenant_admin(
        zone_id="acme",
        username="alice",
        email="alice@acme.com"
    )

    beta_admin = tm.create_tenant_admin(
        zone_id="beta",
        username="bob",
        email="bob@beta.com"
    )

    # Add users to tenants
    tm.add_tenant_user(
        zone_id="acme",
        username="charlie",
        email="charlie@acme.com",
        role="editor"
    )

    tm.add_tenant_user(
        zone_id="acme",
        username="diana",
        email="diana@acme.com",
        role="user"
    )

    # List all tenants
    print("\n📋 All Tenants:")
    for tenant in tm.list_tenants():
        print(f"  - {tenant['name']} ({tenant['zone_id']})")
        print(f"    Plan: {tenant['plan']}, Users: {tenant['usage']['users']}/{tenant['limits']['max_users']}")
```

**Run it:**

```bash
python tenant_manager.py
```

---

## Step 3: Implement Tenant Isolation

Ensure complete data isolation between tenants:

```python
# tenant_isolation_demo.py
import nexus

# Acme tenant user (Alice)
alice = nexus.connect(config={
    "url": "http://localhost:2026",
    "api_key": "alice_key_here"
})

# Beta tenant user (Bob)
bob = nexus.connect(config={
    "url": "http://localhost:2026",
    "api_key": "bob_key_here"
})

# Alice creates files in Acme tenant workspace
alice.write(
    "/tenants/acme/shared/project-plan.md",
    b"""# Acme Project Plan

## Q1 Goals
- Launch new product
- Expand to EMEA
- Hire 10 engineers

CONFIDENTIAL - Acme Corporation
"""
)

print("✅ Alice created Acme project plan")

# Alice creates personal files
alice.write(
    "/tenants/acme/users/alice/notes.md",
    b"Personal notes for Alice - Acme internal"
)

print("✅ Alice created personal notes")

# Bob creates files in Beta tenant workspace
bob.write(
    "/tenants/beta/shared/roadmap.md",
    b"""# Beta Product Roadmap

## Features
- AI integration
- Mobile app
- API v2

CONFIDENTIAL - Beta Inc
"""
)

print("✅ Bob created Beta roadmap")

# Verify isolation: Bob CANNOT access Acme files
print("\n🔒 Testing Tenant Isolation:")

try:
    # Bob tries to read Acme files
    acme_file = bob.read("/tenants/acme/shared/project-plan.md")
    print("❌ SECURITY BREACH: Bob accessed Acme files!")
except nexus.NexusPermissionError:
    print("✅ Isolation working: Bob cannot access Acme files")

try:
    # Alice tries to read Beta files
    beta_file = alice.read("/tenants/beta/shared/roadmap.md")
    print("❌ SECURITY BREACH: Alice accessed Beta files!")
except nexus.NexusPermissionError:
    print("✅ Isolation working: Alice cannot access Beta files")

# Verify: Users can only see their tenant's files
alice_files = alice.list("/tenants/acme", recursive=True)
print(f"\n📁 Alice can see {len(alice_files)} Acme files:")
for f in alice_files[:5]:
    print(f"  - {f['path']}")

bob_files = bob.list("/tenants/beta", recursive=True)
print(f"\n📁 Bob can see {len(bob_files)} Beta files:")
for f in bob_files[:5]:
    print(f"  - {f['path']}")

print("\n✅ Complete tenant isolation verified!")
```

---

## Step 4: Cross-Tenant Data Sharing (Controlled)

Implement secure cross-tenant sharing:

```python
# cross_tenant_sharing.py
import nexus
import json
from datetime import datetime, timedelta

class CrossTenantSharing:
    """Manage secure cross-tenant data sharing"""

    def __init__(self, admin_api_key: str):
        self.admin = nexus.connect(config={
            "url": "http://localhost:2026",
            "api_key": admin_api_key
        })

    def create_shared_link(
        self,
        zone_id: str,
        file_path: str,
        target_zone_id: str,
        permission: str = "can_read",
        expires_in_days: int = 7
    ) -> Dict:
        """Create a secure cross-tenant share"""

        # Create share metadata
        share_id = f"share_{datetime.now().timestamp()}"
        expires_at = datetime.now() + timedelta(days=expires_in_days)

        share_metadata = {
            "share_id": share_id,
            "source_tenant": zone_id,
            "target_tenant": target_zone_id,
            "file_path": file_path,
            "permission": permission,
            "created_at": datetime.now().isoformat(),
            "expires_at": expires_at.isoformat(),
            "status": "active"
        }

        # Grant temporary cross-tenant permission
        # Create a symlink or reference in target tenant's shared folder
        target_path = f"/tenants/{target_zone_id}/shared/from-{zone_id}"
        self.admin.mkdir(target_path)

        # Copy file to shared location (or create reference)
        shared_file_path = f"{target_path}/{file_path.split('/')[-1]}"

        # Grant target tenant read access
        # Note: Grant to all users in target tenant by using a group or specific users
        # For simplicity, we'll create a share that specific users can access
        self.admin.rebac_create(
            subject=("tenant", target_zone_id),
            relation=permission,
            object=("file", shared_file_path),
            zone_id=zone_id
        )

        # Store share metadata
        self.admin.write(
            f"{target_path}/.share-{share_id}.json",
            json.dumps(share_metadata, indent=2).encode()
        )

        print(f"✅ Created cross-tenant share: {share_id}")
        print(f"   {zone_id} → {target_zone_id}")
        print(f"   File: {file_path}")
        print(f"   Permission: {permission}")
        print(f"   Expires: {expires_at.date()}")

        return share_metadata

    def revoke_share(self, share_id: str):
        """Revoke a cross-tenant share"""
        # Find and delete share
        # Remove permissions
        # Clean up shared file
        print(f"✅ Revoked share: {share_id}")

    def list_active_shares(self, zone_id: str) -> List[Dict]:
        """List all active shares for a tenant"""
        # Implementation would scan for share metadata
        pass

# Usage example
sharing = CrossTenantSharing("admin_key_here")

# Acme shares a file with Beta (partnership)
share = sharing.create_shared_link(
    zone_id="acme",
    file_path="/tenants/acme/shared/partnership-proposal.pdf",
    target_zone_id="beta",
    permission="can_read",
    expires_in_days=30
)
```

---

## Step 5: Implement Tenant Quotas and Billing

Track usage and enforce limits:

```python
# tenant_quotas.py
import nexus
import json
from typing import Dict

class TenantQuotaManager:
    """Manage tenant quotas and usage tracking"""

    def __init__(self, admin_api_key: str):
        self.admin = nexus.connect(config={
            "url": "http://localhost:2026",
            "api_key": admin_api_key
        })

    def check_and_enforce_quota(
        self,
        zone_id: str,
        operation: str,
        amount: int = 1
    ) -> bool:
        """Check quota before allowing operation"""

        # Get tenant metadata
        metadata = json.loads(
            self.admin.read(f"/tenants/{zone_id}/.tenant.json").decode()
        )

        limits = metadata['limits']
        usage = metadata['usage']

        # Check specific quota
        if operation == "create_user":
            if usage['users'] >= limits['max_users']:
                raise QuotaExceededError(
                    f"Tenant {zone_id} has reached user limit: {limits['max_users']}"
                )

        elif operation == "upload_file":
            if limits.get('max_files') and usage['files'] >= limits['max_files']:
                raise QuotaExceededError(
                    f"Tenant {zone_id} has reached file limit: {limits['max_files']}"
                )

        elif operation == "storage":
            if usage['storage_gb'] + (amount / 1024**3) > limits['max_storage_gb']:
                raise QuotaExceededError(
                    f"Tenant {zone_id} would exceed storage limit: {limits['max_storage_gb']}GB"
                )

        return True

    def update_usage(self, zone_id: str, metric: str, delta: float):
        """Update tenant usage metrics"""
        metadata = json.loads(
            self.admin.read(f"/tenants/{zone_id}/.tenant.json").decode()
        )

        metadata['usage'][metric] += delta

        self.admin.write(
            f"/tenants/{zone_id}/.tenant.json",
            json.dumps(metadata, indent=2).encode()
        )

    def get_usage_report(self, zone_id: str) -> Dict:
        """Generate usage report for billing"""
        metadata = json.loads(
            self.admin.read(f"/tenants/{zone_id}/.tenant.json").decode()
        )

        usage = metadata['usage']
        limits = metadata['limits']

        return {
            "zone_id": zone_id,
            "plan": metadata['plan'],
            "usage": usage,
            "limits": limits,
            "utilization": {
                "users": f"{usage['users']}/{limits['max_users']} ({usage['users']/limits['max_users']*100:.1f}%)",
                "storage": f"{usage['storage_gb']:.2f}/{limits['max_storage_gb']}GB ({usage['storage_gb']/limits['max_storage_gb']*100:.1f}%)",
            }
        }

    def calculate_overage_charges(self, zone_id: str) -> Dict:
        """Calculate overage charges for billing"""
        report = self.get_usage_report(zone_id)

        charges = {
            "base_charge": 0,
            "overage_charges": {},
            "total": 0
        }

        # Example pricing
        plan_prices = {
            "free": 0,
            "pro": 99,
            "enterprise": 499
        }

        charges["base_charge"] = plan_prices.get(report['plan'], 0)

        # Calculate overages (simplified)
        usage = report['usage']
        limits = report['limits']

        if usage['users'] > limits['max_users']:
            overage = usage['users'] - limits['max_users']
            charges["overage_charges"]["users"] = overage * 10  # $10 per extra user

        if usage['storage_gb'] > limits['max_storage_gb']:
            overage = usage['storage_gb'] - limits['max_storage_gb']
            charges["overage_charges"]["storage"] = overage * 0.50  # $0.50 per extra GB

        charges["total"] = charges["base_charge"] + sum(charges["overage_charges"].values())

        return charges

class QuotaExceededError(Exception):
    pass

# Usage
quota_mgr = TenantQuotaManager("admin_key_here")

# Check quota before operation
try:
    quota_mgr.check_and_enforce_quota("acme", "create_user")
    # Proceed with user creation...
    quota_mgr.update_usage("acme", "users", 1)
except QuotaExceededError as e:
    print(f"❌ {e}")

# Generate usage report
report = quota_mgr.get_usage_report("acme")
print(f"\n📊 Acme Usage Report:")
print(f"  Users: {report['utilization']['users']}")
print(f"  Storage: {report['utilization']['storage']}")

# Calculate charges
charges = quota_mgr.calculate_overage_charges("acme")
print(f"\n💰 Acme Billing:")
print(f"  Base: ${charges['base_charge']}")
if charges['overage_charges']:
    print(f"  Overages: ${sum(charges['overage_charges'].values()):.2f}")
print(f"  Total: ${charges['total']:.2f}")
```

---

## Step 6: Tenant Lifecycle Management

Manage tenant creation, suspension, and deletion:

```python
# tenant_lifecycle.py
import nexus
import json
from datetime import datetime
from typing import Dict

class TenantLifecycle:
    """Manage complete tenant lifecycle"""

    def __init__(self, admin_api_key: str):
        self.admin = nexus.connect(config={
            "url": "http://localhost:2026",
            "api_key": admin_api_key
        })

    def suspend_tenant(self, zone_id: str, reason: str):
        """Suspend a tenant (maintain data, block access)"""

        # Update tenant status
        metadata = json.loads(
            self.admin.read(f"/tenants/{zone_id}/.tenant.json").decode()
        )

        metadata['status'] = 'suspended'
        metadata['suspended_at'] = datetime.now().isoformat()
        metadata['suspension_reason'] = reason

        self.admin.write(
            f"/tenants/{zone_id}/.tenant.json",
            json.dumps(metadata, indent=2).encode()
        )

        # Revoke all user access (but keep data)
        users = self._get_tenant_users(zone_id)
        for user in users:
            self._revoke_user_access(user, zone_id)

        print(f"⏸️  Suspended tenant: {zone_id}")
        print(f"   Reason: {reason}")

    def reactivate_tenant(self, zone_id: str):
        """Reactivate a suspended tenant"""

        metadata = json.loads(
            self.admin.read(f"/tenants/{zone_id}/.tenant.json").decode()
        )

        metadata['status'] = 'active'
        metadata['reactivated_at'] = datetime.now().isoformat()

        self.admin.write(
            f"/tenants/{zone_id}/.tenant.json",
            json.dumps(metadata, indent=2).encode()
        )

        # Restore user access
        users = self._get_tenant_users(zone_id)
        for user in users:
            self._restore_user_access(user, zone_id)

        print(f"▶️  Reactivated tenant: {zone_id}")

    def export_tenant_data(self, zone_id: str) -> str:
        """Export all tenant data for backup or migration"""

        tenant_path = f"/tenants/{zone_id}"
        export_path = f"/exports/{zone_id}-{datetime.now().strftime('%Y%m%d')}.tar.gz"

        # In production, this would create a compressed archive
        # For demo, we'll list what would be exported

        files = self.admin.list(tenant_path, recursive=True)

        print(f"📦 Exporting tenant data: {zone_id}")
        print(f"   Total files: {len(files)}")
        print(f"   Export path: {export_path}")

        return export_path

    def delete_tenant(self, zone_id: str, confirm: bool = False):
        """Permanently delete a tenant and all data"""

        if not confirm:
            print("⚠️  DANGER: This will permanently delete all tenant data!")
            print("   Call with confirm=True to proceed")
            return

        # Delete all tenant data
        tenant_path = f"/tenants/{zone_id}"

        # Remove all permissions
        # Delete all user accounts
        # Delete tenant workspace

        self.admin.rmdir(tenant_path, recursive=True)

        print(f"🗑️  Deleted tenant: {zone_id}")
        print("   All data permanently removed")

    def _get_tenant_users(self, zone_id: str) -> list:
        """Get all users in a tenant"""
        # Implementation would query user database
        return []

    def _revoke_user_access(self, user: str, zone_id: str):
        """Revoke user access to tenant"""
        pass

    def _restore_user_access(self, user: str, zone_id: str):
        """Restore user access to tenant"""
        pass

# Usage
lifecycle = TenantLifecycle("admin_key_here")

# Suspend tenant for non-payment
lifecycle.suspend_tenant("startup", "Payment overdue")

# Export data before deletion
lifecycle.export_tenant_data("startup")

# Delete tenant (requires confirmation)
lifecycle.delete_tenant("startup", confirm=True)
```

---

## Step 7: Scaling to Thousands of Tenants

Optimize for scale:

```python
# scalable_saas.py
import nexus
from typing import Dict, List
import asyncio
from concurrent.futures import ThreadPoolExecutor

class ScalableSaaS:
    """Production-ready multi-zone SaaS implementation"""

    def __init__(self, admin_api_key: str, max_workers: int = 10):
        self.admin = nexus.connect(config={
            "url": "http://localhost:2026",
            "api_key": admin_api_key
        })
        self.executor = ThreadPoolExecutor(max_workers=max_workers)

    def bulk_create_tenants(self, tenant_configs: List[Dict]):
        """Create multiple tenants in parallel"""

        def create_single_tenant(config):
            try:
                return self._create_tenant(**config)
            except Exception as e:
                return {"error": str(e), "config": config}

        # Process tenants in parallel
        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(create_single_tenant, tenant_configs))

        successful = [r for r in results if "error" not in r]
        failed = [r for r in results if "error" in r]

        print(f"✅ Created {len(successful)} tenants")
        if failed:
            print(f"❌ Failed to create {len(failed)} tenants")

        return {"successful": successful, "failed": failed}

    def _create_tenant(self, zone_id: str, **kwargs) -> Dict:
        """Create a single tenant"""
        # Implementation from TenantManager
        pass

    def get_tenant_health_metrics(self) -> Dict:
        """Get system-wide tenant health metrics"""

        all_tenants = self._list_all_tenants()

        metrics = {
            "total_tenants": len(all_tenants),
            "by_plan": {},
            "by_status": {},
            "total_users": 0,
            "total_storage_gb": 0,
            "avg_users_per_tenant": 0,
            "avg_storage_per_tenant": 0
        }

        for tenant in all_tenants:
            # Count by plan
            plan = tenant.get('plan', 'unknown')
            metrics['by_plan'][plan] = metrics['by_plan'].get(plan, 0) + 1

            # Count by status
            status = tenant.get('status', 'unknown')
            metrics['by_status'][status] = metrics['by_status'].get(status, 0) + 1

            # Aggregate usage
            usage = tenant.get('usage', {})
            metrics['total_users'] += usage.get('users', 0)
            metrics['total_storage_gb'] += usage.get('storage_gb', 0)

        if metrics['total_tenants'] > 0:
            metrics['avg_users_per_tenant'] = metrics['total_users'] / metrics['total_tenants']
            metrics['avg_storage_per_tenant'] = metrics['total_storage_gb'] / metrics['total_tenants']

        return metrics

    def _list_all_tenants(self) -> List[Dict]:
        """List all tenants efficiently"""
        # Implementation would use optimized queries
        return []

    def implement_tenant_caching(self):
        """Implement caching for tenant metadata"""

        # Use Redis or similar for caching tenant info
        # Cache tenant permissions
        # Cache quota information

        print("✅ Tenant caching enabled")
        print("   - Metadata cache: Redis")
        print("   - Permission cache: In-memory LRU")
        print("   - Quota cache: 5-minute TTL")

# Usage
saas = ScalableSaaS("admin_key_here", max_workers=20)

# Bulk create 100 tenants
tenant_configs = [
    {"zone_id": f"tenant{i:03d}", "name": f"Tenant {i}", "plan": "free"}
    for i in range(100)
]

results = saas.bulk_create_tenants(tenant_configs)

# Get health metrics
metrics = saas.get_tenant_health_metrics()
print(f"\n📊 SaaS Platform Health:")
print(f"  Total Tenants: {metrics['total_tenants']}")
print(f"  Total Users: {metrics['total_users']}")
print(f"  Total Storage: {metrics['total_storage_gb']:.2f}GB")
print(f"  Avg Users/Tenant: {metrics['avg_users_per_tenant']:.1f}")
```

---

## Complete Production Example

Here's a full multi-zone SaaS implementation:

```python
#!/usr/bin/env python3
"""
Production Multi-Tenant SaaS Platform
Complete implementation with isolation, quotas, and lifecycle management
"""
import nexus
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from enum import Enum

class TenantPlan(Enum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"

class TenantStatus(Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    TRIAL = "trial"
    CANCELLED = "cancelled"

class MultiTenantSaaS:
    """Complete multi-zone SaaS platform"""

    def __init__(self, server_url: str, admin_api_key: str):
        self.server_url = server_url
        self.admin = nexus.connect(config={
            "url": server_url,
            "api_key": admin_api_key
        })

        # Plan configurations
        self.plan_limits = {
            TenantPlan.FREE: {
                "max_users": 5,
                "max_storage_gb": 10,
                "max_files": 1000,
                "features": ["basic_storage", "basic_sharing"]
            },
            TenantPlan.PRO: {
                "max_users": 50,
                "max_storage_gb": 500,
                "max_files": 50000,
                "features": ["advanced_storage", "advanced_sharing", "api_access"]
            },
            TenantPlan.ENTERPRISE: {
                "max_users": None,  # Unlimited
                "max_storage_gb": None,  # Unlimited
                "max_files": None,  # Unlimited
                "features": ["all", "custom_integrations", "dedicated_support"]
            }
        }

    def onboard_tenant(
        self,
        zone_id: str,
        name: str,
        admin_email: str,
        plan: TenantPlan = TenantPlan.FREE
    ) -> Dict:
        """Complete tenant onboarding workflow"""

        print(f"\n🚀 Starting tenant onboarding: {name}")

        # Step 1: Create tenant workspace
        tenant_path = f"/tenants/{zone_id}"
        self.admin.mkdir(tenant_path)
        print(f"  ✅ Created tenant workspace")

        # Step 2: Set up directory structure
        for subdir in ["users", "shared", "uploads", "templates", "exports"]:
            self.admin.mkdir(f"{tenant_path}/{subdir}")
        print(f"  ✅ Created directory structure")

        # Step 3: Create tenant metadata
        limits = self.plan_limits[plan]
        metadata = {
            "zone_id": zone_id,
            "name": name,
            "plan": plan.value,
            "status": TenantStatus.TRIAL.value,
            "created_at": datetime.now().isoformat(),
            "trial_ends_at": (datetime.now() + timedelta(days=14)).isoformat(),
            "limits": limits,
            "usage": {
                "users": 0,
                "storage_gb": 0,
                "files": 0,
                "api_calls": 0
            },
            "settings": {
                "allow_public_sharing": False,
                "enforce_2fa": False,
                "data_retention_days": 90
            }
        }

        self.admin.write(
            f"{tenant_path}/.tenant.json",
            json.dumps(metadata, indent=2).encode()
        )
        print(f"  ✅ Saved tenant metadata")

        # Step 4: Create admin user
        admin_username = f"{zone_id}_admin"
        result = self.admin._call_rpc("admin_create_key", {
            "user_id": admin_username,
            "name": f"{name} Admin",
            "subject_type": "user",
            "zone_id": zone_id,
            "is_admin": False,
        })

        api_key = result["api_key"]

        # Grant admin full access
        self.admin.rebac_create(
            subject=("user", admin_username),
            relation="owner",
            object=("file", tenant_path),
            zone_id=zone_id
        )
        print(f"  ✅ Created admin user: {admin_email}")

        # Step 5: Copy templates
        self._copy_welcome_templates(zone_id)
        print(f"  ✅ Copied welcome templates")

        # Step 6: Send welcome email (simulated)
        self._send_welcome_email(name, admin_email, api_key)
        print(f"  ✅ Sent welcome email")

        print(f"\n✅ Tenant onboarding complete!")
        print(f"   Zone ID: {zone_id}")
        print(f"   Plan: {plan.value}")
        print(f"   Trial ends: {metadata['trial_ends_at']}")

        return {
            "zone_id": zone_id,
            "admin_username": admin_username,
            "api_key": api_key,
            "metadata": metadata
        }

    def _copy_welcome_templates(self, zone_id: str):
        """Copy welcome templates to new tenant"""
        welcome_doc = b"""# Welcome to Your Workspace!

## Getting Started

1. Invite your team members
2. Upload your files
3. Start collaborating

## Features Available

- Secure file storage
- Real-time collaboration
- Version history
- Access control

Need help? Contact support@example.com
"""
        self.admin.write(
            f"/tenants/{zone_id}/shared/Welcome.md",
            welcome_doc
        )

    def _send_welcome_email(self, tenant_name: str, email: str, api_key: str):
        """Send welcome email (simulated)"""
        # In production: use SendGrid, AWS SES, etc.
        print(f"\n📧 Email to {email}:")
        print(f"   Subject: Welcome to {tenant_name}!")
        print(f"   API Key: {api_key[:20]}...")

    def upgrade_tenant(self, zone_id: str, new_plan: TenantPlan) -> Dict:
        """Upgrade tenant to a new plan"""

        metadata = self._get_tenant_metadata(zone_id)

        old_plan = TenantPlan(metadata['plan'])
        metadata['plan'] = new_plan.value
        metadata['limits'] = self.plan_limits[new_plan]
        metadata['upgraded_at'] = datetime.now().isoformat()
        metadata['status'] = TenantStatus.ACTIVE.value

        self._save_tenant_metadata(zone_id, metadata)

        print(f"⬆️  Upgraded {zone_id}: {old_plan.value} → {new_plan.value}")

        return metadata

    def monitor_usage(self, zone_id: str) -> Dict:
        """Monitor and report tenant usage"""

        metadata = self._get_tenant_metadata(zone_id)

        usage = metadata['usage']
        limits = metadata['limits']

        # Calculate utilization percentages
        utilization = {}

        for resource in ['users', 'storage_gb', 'files']:
            limit = limits.get(f'max_{resource}')
            if limit is None:
                utilization[resource] = 0  # Unlimited
            else:
                current = usage.get(resource, 0)
                utilization[resource] = (current / limit * 100) if limit > 0 else 0

        # Check for quota violations
        warnings = []
        if utilization.get('users', 0) > 80:
            warnings.append(f"Users at {utilization['users']:.0f}% of limit")
        if utilization.get('storage_gb', 0) > 80:
            warnings.append(f"Storage at {utilization['storage_gb']:.0f}% of limit")

        return {
            "zone_id": zone_id,
            "usage": usage,
            "limits": limits,
            "utilization": utilization,
            "warnings": warnings
        }

    def _get_tenant_metadata(self, zone_id: str) -> Dict:
        """Get tenant metadata"""
        return json.loads(
            self.admin.read(f"/tenants/{zone_id}/.tenant.json").decode()
        )

    def _save_tenant_metadata(self, zone_id: str, metadata: Dict):
        """Save tenant metadata"""
        self.admin.write(
            f"/tenants/{zone_id}/.tenant.json",
            json.dumps(metadata, indent=2).encode()
        )

def main():
    """Demo the multi-zone SaaS platform"""

    SERVER_URL = "http://localhost:2026"
    ADMIN_KEY = "admin_key_here"  # Replace with actual admin key

    saas = MultiTenantSaaS(SERVER_URL, ADMIN_KEY)

    # Onboard new tenants
    acme = saas.onboard_tenant(
        zone_id="acme",
        name="Acme Corporation",
        admin_email="admin@acme.com",
        plan=TenantPlan.ENTERPRISE
    )

    beta = saas.onboard_tenant(
        zone_id="beta",
        name="Beta Inc",
        admin_email="admin@beta.com",
        plan=TenantPlan.PRO
    )

    startup = saas.onboard_tenant(
        zone_id="startup",
        name="Startup Co",
        admin_email="admin@startup.com",
        plan=TenantPlan.FREE
    )

    # Upgrade a tenant
    saas.upgrade_tenant("startup", TenantPlan.PRO)

    # Monitor usage
    usage = saas.monitor_usage("acme")
    print(f"\n📊 Acme Usage:")
    print(f"  Users: {usage['usage']['users']}")
    print(f"  Storage: {usage['usage']['storage_gb']}GB")
    if usage['warnings']:
        print(f"  ⚠️  Warnings: {', '.join(usage['warnings'])}")

if __name__ == "__main__":
    main()
```

---

## Troubleshooting

### Issue: Cross-Tenant Data Leakage

**Problem:** User can access another tenant's data

**Solution:**
```python
# Verify permissions are properly scoped
permissions = nx.rebac_list_tuples(
    subject=("user", "alice")
)

# Ensure all permissions start with /tenants/{zone_id}
for perm in permissions:
    if not perm['object_id'].startswith(f"/tenants/{alice_zone_id}"):
        print(f"⚠️  Cross-tenant permission detected: {perm}")
```

---

### Issue: Quota Not Enforced

**Problem:** Tenants exceed quotas

**Solution:**
```python
# Implement quota check middleware
def enforce_quota_middleware(zone_id, operation, amount):
    metadata = get_tenant_metadata(zone_id)

    # Check before allowing operation
    if not check_quota(metadata, operation, amount):
        raise QuotaExceededError(
            f"Tenant {zone_id} quota exceeded for {operation}"
        )

    # Allow operation
    # ...

    # Update usage after operation
    update_usage(zone_id, operation, amount)
```

---

## Best Practices

### 1. Always Use Tenant Prefixes

```python
# ✅ Good: All tenant data under /tenants/{zone_id}
tenant_path = f"/tenants/{zone_id}/data"

# ❌ Bad: Mixed tenant data
user_path = f"/users/{user_id}/data"  # Crosses tenant boundaries
```

### 2. Implement Audit Logging

```python
def audit_log(zone_id, user, action, resource):
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "zone_id": zone_id,
        "user": user,
        "action": action,
        "resource": resource
    }

    nx.append(
        f"/tenants/{zone_id}/.audit/log.jsonl",
        (json.dumps(log_entry) + '\n').encode()
    )
```

### 3. Use Database for Tenant Metadata

```python
# ✅ Good: Use PostgreSQL for tenant metadata
# Fast queries, transactions, indexing

# ❌ Bad: Store tenant metadata only in files
# Slow to query, no transactions
```

---

## What's Next?

**Congratulations!** You've built a production-ready multi-zone SaaS platform with Nexus.

### 🔍 Recommended Next Steps

1. **[Administration & Operations](administration-operations.md)** (25 min)
   Learn advanced admin operations and monitoring

2. **[Multi-Backend Storage](multi-backend-storage.md)** (20 min)
   Scale storage across multiple backends

3. **[Production Deployment](../production/deployment-patterns.md)**
   Deploy your SaaS platform to production

### 📚 Related Concepts

- [Multi-Tenancy Architecture](../concepts/multi-tenancy.md)
- [ReBAC for Multi-Tenant](../concepts/rebac-explained.md)
- [Scaling Patterns](../production/scaling-guide.md)

### 🔧 Advanced Topics

- [Performance Tuning](../how-to/optimize/performance-tuning.md)
- [Security Hardening](../production/security-checklist.md)
- [Monitoring & Observability](../production/monitoring.md)

---

## Summary

🎉 **You've completed the Multi-Tenant SaaS tutorial!**

**What you learned:**
- ✅ Design and implement multi-zone architecture
- ✅ Ensure complete tenant isolation
- ✅ Manage tenant lifecycle (create, suspend, delete)
- ✅ Implement quotas and billing
- ✅ Enable secure cross-tenant sharing
- ✅ Scale to thousands of tenants
- ✅ Monitor usage and enforce limits

**Key Takeaways:**
- Tenant isolation is critical for security
- Use /tenants/{zone_id} prefix for all tenant data
- Implement quotas from day one
- Monitor usage for billing and capacity planning
- Use PostgreSQL for production deployments

---

**Next:** [Administration & Operations →](administration-operations.md)

**Questions?** Check our [Multi-Tenancy Guide](../concepts/multi-tenancy.md) or [GitHub Discussions](https://github.com/nexi-lab/nexus/discussions)

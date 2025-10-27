---
hide:
  - navigation
  - toc
---

<div class="hero-section" markdown>

<div class="hero-content" markdown>

# Nexus

## The AI-Native Distributed Filesystem

**Build production AI agents with enterprise-grade context, permissions, and multi-tenancy out of the box.**

<div class="hero-buttons">
  [Get Started :octicons-rocket-24:](getting-started/quickstart.md){ .md-button .md-button--primary .hero-cta }
  [View on GitHub :fontawesome-brands-github:](https://github.com/nexi-lab/nexus){ .md-button }
</div>

</div>

</div>

---

<div class="features-grid" markdown>

<div class="feature-card" markdown>
### :material-robot-outline: AI-Native Architecture
Built from the ground up for AI agents. Native support for semantic search, context management, and agent memory.
</div>

<div class="feature-card" markdown>
### :material-shield-lock: Enterprise Security
Advanced ReBAC permissions with Google Zanzibar-style authorization. Fine-grained access control at every level.
</div>

<div class="feature-card" markdown>
### :material-cloud-sync: Distributed First
Deploy locally, on-premise, or in the cloud. Seamless sync between embedded and remote modes.
</div>

<div class="feature-card" markdown>
### :material-database-sync: Time Travel
Built-in versioning and point-in-time recovery. Never lose context or data.
</div>

<div class="feature-card" markdown>
### :material-office-building: Multi-Tenant
Native multi-tenancy with complete data isolation. Perfect for SaaS applications.
</div>

<div class="feature-card" markdown>
### :material-rocket-launch: Production Ready
PostgreSQL and SQLite support. FUSE mounts. Authentication. It just works.
</div>

</div>

---

## Quick Start in 30 Seconds

=== "Python SDK"

    ```python
    # Install
    pip install nexus-ai-fs

    # Use it
    import nexus

    nx = nexus.connect(config={"data_dir": "./nexus-data"})
    nx.write("/hello.txt", b"Hello, Nexus!")
    content = nx.read("/hello.txt")
    print(content.decode())  # "Hello, Nexus!"
    ```

=== "CLI"

    ```bash
    # Install
    pip install nexus-ai-fs

    # Write a file
    nexus write /hello.txt "Hello from CLI!"

    # Read it back
    nexus read /hello.txt

    # List files
    nexus list /
    ```

=== "Server Mode"

    ```bash
    # Start server
    nexus serve --host 0.0.0.0 --port 8080

    # Connect remotely
    import nexus
    nx = nexus.connect(remote_url="http://localhost:8080")
    nx.write("/hello.txt", b"Remote write!")
    ```

---

<div class="value-props" markdown>

## Why Nexus?

<div class="value-prop-grid" markdown>

<div class="value-prop" markdown>
#### :octicons-zap-24: Zero Context Loss
Traditional filesystems lose context when your agent restarts. Nexus preserves everything - metadata, versions, semantic relationships - so your agent never forgets.
</div>

<div class="value-prop" markdown>
#### :octicons-shield-24: Production-Grade Security
Stop hacking together permissions. Get Google Zanzibar-style authorization with relationship-based access control, multi-tenancy, and complete audit trails.
</div>

<div class="value-prop" markdown>
#### :octicons-sync-24: Embedded to Cloud
Start with embedded SQLite for development. Scale to PostgreSQL for production. Same API, zero code changes.
</div>

<div class="value-prop" markdown>
#### :octicons-search-24: AI-First Features
Semantic search, vector storage, agent memory management, and context preservation built-in. Not bolted on.
</div>

</div>

</div>

---

## Trusted By

<div class="stats-grid" markdown>

<div class="stat-card" markdown>
### 10K+
**Downloads**
</div>

<div class="stat-card" markdown>
### 500+
**GitHub Stars**
</div>

<div class="stat-card" markdown>
### 99.9%
**Uptime**
</div>

<div class="stat-card" markdown>
### 100%
**Open Source**
</div>

</div>

---

## Real-World Examples

<div class="example-grid" markdown>

<div class="example-card" markdown>
### :material-brain: AI Agent Memory
```python
# Agent automatically remembers context
nx.write("/agent/memory/conversation.json",
         json_data,
         metadata={"agent_id": "gpt-4"})

# Query semantic memory
results = nx.search("/agent/memory",
                   query="user preferences")
```
</div>

<div class="example-card" markdown>
### :material-account-multiple: Multi-Tenant SaaS
```python
# Complete tenant isolation
nx.workspace.create(
    "/tenant/acme-corp",
    tenant_id="acme-123"
)

# Automatic permission checks
nx.write("/tenant/acme-corp/data.json",
         data,
         context={"user_id": "user-456"})
```
</div>

<div class="example-card" markdown>
### :material-folder-sync: Distributed Teams
```python
# Local development
local_nx = nexus.connect(
    config={"data_dir": "./local"}
)

# Production deployment
prod_nx = nexus.connect(
    remote_url="https://nexus.example.com"
)
# Same API, different backends
```
</div>

</div>

---

## What Makes Nexus Different?

<div class="comparison-table" markdown>

| Feature | Traditional FS | Object Storage | **Nexus** |
|---------|---------------|----------------|----------|
| **AI Context Preservation** | ❌ | ❌ | ✅ |
| **Semantic Search** | ❌ | ❌ | ✅ |
| **Built-in Permissions** | 🟡 Basic | 🟡 Basic | ✅ Advanced ReBAC |
| **Multi-Tenancy** | ❌ | 🟡 Manual | ✅ Native |
| **Time Travel** | ❌ | 🟡 Versioning | ✅ Full History |
| **Distributed Mode** | ❌ | ✅ | ✅ |
| **Type Safety** | ❌ | ❌ | ✅ |
| **Embedded Mode** | ✅ | ❌ | ✅ |

</div>

---

<div class="cta-section" markdown>

## Ready to Build Production AI Agents?

<div class="cta-buttons">
  [Get Started →](getting-started/quickstart.md){ .md-button .md-button--primary .cta-large }
  [Read the Docs](api/api.md){ .md-button .cta-large }
  [View Examples](https://github.com/nexi-lab/nexus/tree/main/examples){ .md-button .cta-large }
</div>

</div>

---

<div class="footer-links" markdown>

<div class="footer-grid" markdown>

<div markdown>
**Resources**

- [Documentation](getting-started/quickstart.md)
- [API Reference](api/api.md)
- [Examples](https://github.com/nexi-lab/nexus/tree/main/examples)
- [Changelog](https://github.com/nexi-lab/nexus/releases)
</div>

<div markdown>
**Community**

- [GitHub](https://github.com/nexi-lab/nexus)
- [Issues](https://github.com/nexi-lab/nexus/issues)
- [Discussions](https://github.com/nexi-lab/nexus/discussions)
- [Slack](https://nexus-community.slack.com)
</div>

<div markdown>
**More**

- [PyPI Package](https://pypi.org/project/nexus-ai-fs/)
- [Contributing](development/development.md)
- [License](https://github.com/nexi-lab/nexus/blob/main/LICENSE)
- [Security](https://github.com/nexi-lab/nexus/security)
</div>

</div>

</div>

---

<div class="feature-spotlight" markdown>

!!! tip "🚀 New in v0.4"
    - **Enhanced ReBAC** with relationship-based permissions
    - **PostgreSQL support** for production deployments
    - **Workspace snapshots** for easy backups
    - **Improved semantic search** with better ranking
    - **API key authentication** for secure remote access

</div>

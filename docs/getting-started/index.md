# Getting Started with Nexus

Welcome to Nexus! This guide will help you get up and running in minutes.

<div class="features-grid" markdown>

<div class="feature-card" markdown>
### :material-download: Installation
Learn how to install Nexus via pip and set up your environment.

[Install Nexus →](installation.md){ .md-button }
</div>

<div class="feature-card" markdown>
### :material-rocket-launch: Quick Start
Get started with a working example in under 5 minutes.

[Quick Start →](quickstart.md){ .md-button }
</div>

<div class="feature-card" markdown>
### :material-cog: Configuration
Configure Nexus for your specific use case.

[Configure →](configuration.md){ .md-button }
</div>

<div class="feature-card" markdown>
### :material-cloud-upload: Deployment
Deploy in embedded, server, or distributed mode.

[Deploy →](deployment-modes.md){ .md-button }
</div>

</div>

---

## What is Nexus?

**Nexus** is an AI-native distributed filesystem designed from the ground up for production AI agents. It provides:

- **Context Preservation**: Never lose agent context across restarts
- **Enterprise Security**: Google Zanzibar-style ReBAC permissions
- **Multi-Tenancy**: Native tenant isolation for SaaS applications
- **Time Travel**: Built-in versioning and point-in-time recovery
- **Semantic Search**: AI-first search capabilities
- **Distributed First**: Seamless embedded-to-cloud deployment

---

## Quick Example

```python
import nexus

# Connect to Nexus
nx = nexus.connect(config={"data_dir": "./nexus-data"})

# Write a file
nx.write("/hello.txt", b"Hello, Nexus!")

# Read it back
content = nx.read("/hello.txt")
print(content.decode())  # "Hello, Nexus!"

# List files
files = nx.list("/")
```

---

## Choose Your Path

=== "New Users"

    **Start Here:**

    1. [Install Nexus](installation.md)
    2. [Complete the Quick Start](quickstart.md)
    3. [Explore the API Reference](../api/api.md)

=== "Developers"

    **Best Practices:**

    1. [Configuration Guide](configuration.md)
    2. [Deployment Modes](deployment-modes.md)
    3. [Development Guide](../development/development.md)

=== "DevOps/SRE"

    **Production Setup:**

    1. [Server Deployment](../deployment/server-setup.md)
    2. [PostgreSQL Setup](../deployment/postgresql.md)
    3. [Authentication](../authentication.md)

---

!!! tip "Need Help?"
    - Join our [Slack community](https://nexus-community.slack.com)
    - Check out the [examples](https://github.com/nexi-lab/nexus/tree/main/examples)
    - Open an [issue on GitHub](https://github.com/nexi-lab/nexus/issues)

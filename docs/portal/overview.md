# Control Panel Guide

## Overview

The Nexus Control Panel is a web-based interface for managing your Nexus server, providing human oversight and control over AI agents' access to resources.

## Key Features

### User Management
- Create and manage user accounts
- Generate and rotate API keys
- Set key expiration and access levels
- Track user activity and sessions

### Permissions Management
- Configure ReBAC (Relationship-Based Access Control) policies
- Grant and revoke access to files, workspaces, and resources
- Define permission inheritance hierarchies
- Manage multi-tenant isolation

### Integration Management
- Configure connectors (S3, GCS, Gmail, Google Drive, X/Twitter)
- Set up OAuth flows for third-party services
- Manage MCP server integrations
- Monitor connector health and performance

### Audit & Versioning
- View operation logs and access history
- Track file changes and versions
- Monitor agent activities
- Export audit trails for compliance

## Access Control Levels

The Control Panel supports different permission levels:

- **Owner**: Full control over resources and permissions
- **Editor**: Read and write access to resources
- **Viewer**: Read-only access to resources

## Getting Started

1. **Access the Control Panel**: Navigate to `http://localhost:8080/portal` (or your server URL)
2. **Authentication**: Log in with your admin API key
3. **Initial Setup**: Configure your first tenant and workspace
4. **Grant Permissions**: Set up access control for your agents

## Configuration

The Control Panel is automatically enabled when running Nexus in server mode. See the [Server Setup Guide](../api/rpc-api.md) for deployment instructions.

## Best Practices

- **Regular Audits**: Review access logs periodically
- **Least Privilege**: Grant minimum necessary permissions
- **API Key Rotation**: Rotate keys every 90 days
- **Tenant Isolation**: Use separate tenants for different projects
- **Monitor Integration Health**: Check connector status regularly

## Related Documentation

- [Permissions & ReBAC](../PERMISSIONS.md)
- [Multi-Tenant Architecture](../MULTI_TENANT.md)
- [API Reference](../api/)
- [Authentication](../authentication.md)

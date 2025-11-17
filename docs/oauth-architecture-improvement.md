# OAuth Architecture Improvement Proposal

## Current Problem

The current `nexus oauth setup-gdrive` command requires users to provide OAuth client credentials:

```bash
# ❌ Current (problematic)
nexus oauth setup-gdrive \
    --client-id "123.apps.googleusercontent.com" \
    --client-secret "GOCSPX-..." \
    --user-email "alice@gmail.com"
```

**Issues:**
1. **Security**: Users handle OAuth client secrets
2. **UX**: Users must create Google Cloud projects
3. **Scalability**: Each user needs separate credentials
4. **Maintenance**: Admin can't rotate credentials centrally

## Proposed Solution

### Two-Tier OAuth Configuration

#### Tier 1: Server-Level OAuth App (Admin)

**One-time setup by admin:**

```bash
# Option A: Environment variables (current workaround)
export NEXUS_OAUTH_GOOGLE_CLIENT_ID="123.apps.googleusercontent.com"
export NEXUS_OAUTH_GOOGLE_CLIENT_SECRET="GOCSPX-..."

# Option B: Server config file (better)
# nexus.yaml
oauth:
  providers:
    google:
      client_id: "123.apps.googleusercontent.com"
      client_secret: "GOCSPX-..."
      scopes:
        - "https://www.googleapis.com/auth/drive"
        - "https://www.googleapis.com/auth/drive.file"

# Option C: Admin CLI command (best)
nexus admin oauth configure google \
    --client-id "123.apps.googleusercontent.com" \
    --client-secret "GOCSPX-..."
```

#### Tier 2: User Authorization (User)

**Users just authorize:**

```bash
# ✅ Proposed (better UX)
nexus oauth authorize gdrive
# OR
nexus oauth login google

# What happens:
# 1. Server provides OAuth credentials (from config)
# 2. Opens browser with OAuth flow
# 3. User signs in and grants permission
# 4. Tokens stored for that user
# 5. Done! (user never sees credentials)
```

## Implementation Plan

### Phase 1: Server-Level OAuth Config

Add OAuth provider configuration to server:

```python
# src/nexus/config.py

class OAuthProviderConfig(BaseModel):
    """OAuth provider configuration."""
    client_id: str
    client_secret: str
    scopes: list[str] = []
    redirect_uri: str | None = None

class OAuthConfig(BaseModel):
    """OAuth configuration for all providers."""
    providers: dict[str, OAuthProviderConfig] = {}

class NexusConfig(BaseModel):
    # ... existing fields ...

    oauth: OAuthConfig = Field(
        default_factory=OAuthConfig,
        description="OAuth provider configurations"
    )
```

**Config file example:**

```yaml
# nexus.yaml

oauth:
  providers:
    google:
      client_id: "123456789-abc.apps.googleusercontent.com"
      client_secret: "GOCSPX-abc123..."
      scopes:
        - "https://www.googleapis.com/auth/drive"
        - "https://www.googleapis.com/auth/drive.file"
      redirect_uri: "http://localhost:8080/oauth/callback"  # Optional

    microsoft:
      client_id: "abcdef-1234-5678"
      client_secret: "secret~..."
      scopes:
        - "Files.ReadWrite.All"
        - "offline_access"
```

### Phase 2: Simplified User Commands

**New commands (better UX):**

```bash
# User authorization (no credentials needed!)
nexus oauth authorize google
nexus oauth authorize microsoft

# Aliases for better UX
nexus oauth login google
nexus gdrive login

# Legacy support (still works, but not recommended)
nexus oauth setup-gdrive \
    --client-id "..." \
    --client-secret "..." \
    --user-email "alice@gmail.com"
```

**Implementation:**

```python
# src/nexus/cli/commands/oauth.py

@oauth.command("authorize")
@click.argument("provider", type=click.Choice(["google", "microsoft"]))
@click.option("--user-email", help="User email (optional, auto-detected from OAuth)")
@click.option("--db-path", default=None, help="Database path")
def authorize(provider: str, user_email: str | None, db_path: str | None):
    """Authorize Nexus to access your cloud storage.

    This command uses OAuth credentials configured by the server admin.
    You only need to sign in and grant permission.

    Examples:
        nexus oauth authorize google
        nexus oauth authorize microsoft
    """
    # 1. Load OAuth config from server
    from nexus.config import load_config

    config = load_config()

    if provider not in config.oauth.providers:
        console.print(f"[red]Error:[/red] OAuth provider '{provider}' not configured")
        console.print("[yellow]Ask your admin to configure OAuth credentials[/yellow]")
        console.print(f"[dim]Admin command: nexus admin oauth configure {provider}[/dim]")
        sys.exit(1)

    provider_config = config.oauth.providers[provider]

    # 2. Create OAuth provider with server credentials
    if provider == "google":
        oauth_provider = GoogleOAuthProvider(
            client_id=provider_config.client_id,
            client_secret=provider_config.client_secret,
            redirect_uri=provider_config.redirect_uri or "urn:ietf:wg:oauth:2.0:oob",
            scopes=provider_config.scopes
        )
    # ... similar for other providers

    # 3. Start OAuth flow (same as before)
    auth_url = oauth_provider.get_authorization_url()
    console.print(f"\n[bold green]Authorize Nexus to access your {provider.title()} account[/bold green]")
    console.print(f"\n[bold yellow]Step 1:[/bold yellow] Visit this URL:\n{auth_url}\n")

    auth_code = click.prompt("\nEnter authorization code")

    # 4. Exchange code for tokens
    credential = await oauth_provider.exchange_code(auth_code)

    # Auto-detect user email from OAuth response if not provided
    if not user_email:
        user_email = credential.metadata.get("email") or click.prompt("Enter your email")

    # 5. Store tokens
    manager = TokenManager(db_path=db_path)
    await manager.store_credential(
        provider=provider,
        user_email=user_email,
        credential=credential
    )

    console.print(f"\n[green]✓[/green] Successfully authorized {provider} for {user_email}")
```

### Phase 3: Admin Configuration Commands

**Admin commands:**

```bash
# Configure OAuth provider
nexus admin oauth configure google \
    --client-id "123.apps.googleusercontent.com" \
    --client-secret "GOCSPX-..."

# View configured providers
nexus admin oauth list-providers

# Remove provider configuration
nexus admin oauth remove-provider google

# Rotate credentials (update all at once)
nexus admin oauth rotate google \
    --client-id "new-id" \
    --client-secret "new-secret"
```

## Migration Path

### Current Workaround (No Code Changes)

Use environment variables as server-level config:

```bash
# Admin sets server-wide OAuth credentials
export NEXUS_OAUTH_GOOGLE_CLIENT_ID="123.apps.googleusercontent.com"
export NEXUS_OAUTH_GOOGLE_CLIENT_SECRET="GOCSPX-..."

# Update CLI to read from environment if not provided
# Users can then just run:
nexus oauth setup-gdrive --user-email "alice@gmail.com"
# (client-id/secret read from environment)
```

### Full Implementation (Code Changes)

1. **Add OAuth config to NexusConfig** (Phase 1)
2. **Add `nexus oauth authorize` command** (Phase 2)
3. **Add admin configuration commands** (Phase 3)
4. **Deprecate user-provided credentials** (but keep for backward compatibility)

## Deployment Scenarios

### Scenario 1: Self-Hosted (Single User)

```bash
# User is also admin - can configure directly
nexus serve --auth-type database --init

# Configure OAuth (admin task)
export NEXUS_OAUTH_GOOGLE_CLIENT_ID="..."
export NEXUS_OAUTH_GOOGLE_CLIENT_SECRET="..."

# Authorize (user task)
nexus oauth authorize google
```

### Scenario 2: Team Server (Multiple Users)

```bash
# Admin configures server
# nexus.yaml
oauth:
  providers:
    google:
      client_id: "team-app.apps.googleusercontent.com"
      client_secret: "GOCSPX-..."

# Admin starts server
nexus serve --auth-type database --config nexus.yaml

# Users authorize (no credentials needed!)
# Alice:
nexus oauth authorize google
# Bob:
nexus oauth authorize google
# Charlie:
nexus oauth authorize google

# Each user goes through OAuth with SAME app
# Each gets their own tokens for their own Drive
```

### Scenario 3: SaaS Deployment

```bash
# Admin configures via environment (secure)
export NEXUS_OAUTH_GOOGLE_CLIENT_ID="saas-app.apps.googleusercontent.com"
export NEXUS_OAUTH_GOOGLE_CLIENT_SECRET="<from-secrets-manager>"

# Users use web UI or CLI
# Web UI: Click "Connect Google Drive" → OAuth popup → Done
# CLI: nexus oauth authorize google → Browser opens → Done

# All users use same OAuth app
# Centralized credential rotation
# Better security & compliance
```

## Benefits

### For Users

✅ **Simple**: Just click "authorize" - no credential management
✅ **Secure**: Never handle OAuth secrets
✅ **Fast**: One-click authorization
✅ **Consistent**: Same OAuth app for all users

### For Admins

✅ **Centralized**: Configure OAuth once for all users
✅ **Rotatable**: Update credentials in one place
✅ **Auditable**: Track which OAuth app is being used
✅ **Scalable**: Support unlimited users with one OAuth app

### For Security

✅ **Least privilege**: Users only see what they need
✅ **Separation of duties**: Admin configures, users authorize
✅ **Rotation**: Easy to rotate credentials centrally
✅ **Compliance**: Better audit trail

## Comparison

### Current (❌ Problematic)

```bash
# Each user needs to:
# 1. Create Google Cloud project
# 2. Enable Drive API
# 3. Create OAuth credentials
# 4. Download client_secret.json
# 5. Extract client_id and client_secret
# 6. Run: nexus oauth setup-gdrive --client-id ... --client-secret ... --user-email ...

# Problems:
# - Too complex for users
# - Each user = different OAuth app = management nightmare
# - Security risk (users handle secrets)
```

### Proposed (✅ Better)

```bash
# Admin (one-time):
nexus admin oauth configure google --client-id ... --client-secret ...

# Users:
nexus oauth authorize google
# → Opens browser → Sign in → Grant permission → Done!

# Benefits:
# - Simple for users
# - All users use same OAuth app
# - Centralized credential management
# - Secure (users never see secrets)
```

## Implementation Priority

1. **Quick Win (Phase 0)**: Environment variable fallback
   - Read `NEXUS_OAUTH_GOOGLE_CLIENT_ID` and `NEXUS_OAUTH_GOOGLE_CLIENT_SECRET`
   - Allow `--client-id` and `--client-secret` to be optional
   - Document workaround

2. **Short Term (Phase 1)**: OAuth config in nexus.yaml
   - Add `oauth` section to NexusConfig
   - Load from config file
   - Still works with current commands

3. **Medium Term (Phase 2)**: New user commands
   - Add `nexus oauth authorize` command
   - Auto-detect user email from OAuth response
   - Keep legacy `setup-gdrive` for compatibility

4. **Long Term (Phase 3)**: Admin commands
   - Add `nexus admin oauth configure` command
   - Add web UI for OAuth management
   - Deprecate user-provided credentials

## Related

- Issue #137: OAuth Token Management
- Issue #136: Google Drive Backend
- MindsDB's approach: Handler-centric (each integration configures OAuth)
- Our approach: Centralized (server configures, users authorize)

# CLI Reference

nexus-fs provides a `nexus-fs` command with two subcommands and an
auth group.

```bash
nexus-fs --version        # show version
nexus-fs doctor           # run diagnostics
nexus-fs playground       # interactive file browser (TUI)
nexus-fs auth <command>   # manage credentials
```

| Command | Description |
|---------|-------------|
| [doctor](doctor.md) | Check environment, backends, and connectivity |
| [playground](playground.md) | Browse files interactively in the terminal |
| `auth list` | List configured auth for all services |
| `auth connect <service>` | Start OAuth or configure credentials |
| `auth test <service>` | Validate stored credentials |
| `auth disconnect <service>` | Remove stored credentials |
| `auth doctor` | Auth-specific diagnostics |

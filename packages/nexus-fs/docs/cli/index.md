# CLI Reference

```bash
nexus-fs --version
nexus-fs mount list
nexus-fs mount test local:///absolute/path
nexus-fs ls /local/data/
nexus-fs cat /local/data/README.md
nexus-fs doctor
```

Use `nexus-fs --help` for file commands and [doctor](doctor.md) for diagnostics.

For a remote native host, use `nexus connect`, `nexus identity whoami` and
`nexus admin create-key`, `list-keys` or `revoke-key`. These commands use the
host's native identity and key records.

The [Nexus TUI](https://github.com/nexi-lab/nexus/tree/develop/packages/nexus-tui)
provides Files, Search and Console on one authenticated connection.

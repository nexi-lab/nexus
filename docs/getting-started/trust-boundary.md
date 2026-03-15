# Trust Boundary

Nexus = filesystem/context plane.

The local quickstart, the shared daemon, and a production auth deployment are
not the same trust model.

## Modes

| Mode | Boundary | Identity model | Use it for |
| --- | --- | --- | --- |
| Local SDK with `profile="minimal"` | One local process | Implicit local user/process | Prototyping, notebooks, tests |
| Shared daemon with one `--api-key` | One shared service | One shared key | Internal tools, simple shared setups |
| Shared daemon with real auth | One shared service | Per-user or per-agent identity | Multi-user systems, audit, policy |

## Rules

- Do not treat the local quickstart as a multi-user secure deployment.
- Do not use one shared key when you need per-user trust or audit trails.
- Use `profile="remote"` for clients talking to a daemon.

## Next

- [Quickstart](quickstart.md)
- [Shared daemon path](../paths/daemon-and-remote.md)

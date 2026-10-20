# User guide

Nexus provides native file operations, authenticated Search and explicit administration of credentials and relationships.

## Connect to the data owner

Use the HTTP and gRPC endpoints of the native host that owns your files. HTTP serves Search, identity and administration. File operations use typed gRPC. Both carry your existing native credential.

For an isolated local host, configure the actual endpoints and zone:

```powershell
$env:NEXUS_URL = 'http://127.0.0.1:2327'
$env:NEXUS_GRPC_HOST = '127.0.0.1:2326'
$env:NEXUS_GRPC_PORT = '2326'
$env:NEXUS_API_KEY = '<native credential>'
$env:NEXUS_ZONE_ID = 'sharedzone'
```

For a TLS gRPC host, also provide its configured certificate material:

```powershell
$env:NEXUS_GRPC_TLS = 'true'
$env:NEXUS_TLS_CA = '<CA certificate path>'
$env:NEXUS_TLS_CERT = '<client certificate path>'
$env:NEXUS_TLS_KEY = '<client private key path>'
```

Replace `/docs` below with a directory mounted by your host. Use its actual zone. `nexus --profile <name>` selects a connection from `~/.nexus/config.yaml`; explicit connection options take precedence.

```sh
nexus identity whoami --json
nexus status --json
nexus ready --timeout 10 --json
nexus doctor remote --json
nexus profile list
nexus config show
```

The identity response comes from `/v2/auth/whoami`, including the authenticated subject, administrator flag and zone grants. Human account login in nexus-frontend and native file credentials have independent identity boundaries.

`status` reports current native HTTP status and the authenticated principal. `ready` waits for both within one deadline; authorization failures exit immediately. `doctor remote` also checks the configured gRPC endpoint. Connection profiles store connection settings; runtime reports come from the selected host.

## Read and write files

```sh
nexus mkdir /docs/example
nexus write /docs/example/report.txt "hello native"
nexus cat /docs/example/report.txt
nexus stat /docs/example/report.txt --json
nexus ls /docs/example --json
nexus rm /docs/example/report.txt
nexus rmdir /docs/example
```

The native host enforces permissions for every operation. Directory listing and discovery require directory read access. File reads and Search results require access to the individual file.

The Python SDK uses the same native transport:

```python
import nexus

nx = nexus.connect(
    config={
        "profile": "remote",
        "url": "http://127.0.0.1:2327",
        "api_key": "<native credential>",
    }
)
try:
    content = nx.sys_read("/docs/report.txt")
finally:
    nx.close()
```

Remote SDK connections call the owning host directly and need its configured endpoints and native credential. Writes create or replace a complete file; `--show-metadata` prints the native write receipt. `cp` and `move` use the owning host's native operations.

`write-batch` uses the native BatchWrite RPC. Each file commits independently. `rename-batch` and `rm-batch` report success or failure for each input path. `read-bulk --atomic` returns a complete result or an error.

## Search and discovery

Index an existing directory through the owning Rust Search host, then query it:

```sh
nexus search index /docs
nexus search query "native filesystem" --mode keyword --json
nexus search query "find the project plan" --mode hybrid --json
nexus search stats --zone-id sharedzone
nexus glob "**/*.txt" /docs
nexus grep "native" /docs
```

Hybrid and semantic queries require the host's configured embedding model. Indexing and statistics require native administration rights. A reader can query authorized content while statistics remain forbidden.

For an explicit Search endpoint, pass `--remote-url <HTTP origin>` on the Search command. Keep that endpoint tied to the data owner and use the same native caller credential.

Administrators can manage durable Search path descriptions and ranking weights:

```sh
nexus path-context set /docs/project "Project design documents" --zone-id sharedzone --weight 2
nexus path-context list --zone-id sharedzone --json
nexus path-context delete /docs/project --zone-id sharedzone
```

## Manage native credentials

These commands require an existing native administrator credential:

```sh
nexus admin create-key alice --subject-type user --name "Alice reader" --zone sharedzone:r --json
nexus admin list-keys --subject-id alice --json
nexus admin revoke-key <key_hash> --json
```

Creation returns the plaintext `key` once, its `key_hash`, `key_id` and public record. Listing returns public records. Revocation takes the returned hash and reports the native `existed` receipt.

`create-key` accepts `user` and `service` subjects, repeated `--zone` grants, an absolute `--expires-at-ms` cutoff, `--allow-existing` for rotation, and `--admin` for a zoneless administrator key. The native host validates the request and the caller's authority. Agent certificate identities follow their runtime's existing enrollment path.

## Manage native ReBAC tuples

An administrator grants and lists complete tuples for an explicit zone:

```sh
nexus rebac create user alice viewer file /docs/report.txt --zone-id sharedzone --json
nexus rebac list --zone-id sharedzone --json
nexus rebac delete user alice viewer file /docs/report.txt --zone-id sharedzone --json
```

Deletion identifies the zone, object, relation and subject. Include `--subject-relation` for a userset tuple:

```sh
nexus rebac create group engineering viewer file /docs/report.txt --subject-relation member --zone-id sharedzone
nexus rebac delete group engineering viewer file /docs/report.txt --subject-relation member --zone-id sharedzone --json
```

These operations use the same native tuple store as file and Search authorization. The CLI forwards the full tuple. A revoke receipt reports whether it existed; test access through the native file or Search operation.

## Inspect native agent processes

The runtime registry holds current process state in the daemon. Its PID identifies the external OS process; a micro-agent's PID also includes its local ID. Runtime registration and identity enrollment have independent responsibilities.

```sh
nexus agent list --zone-id sharedzone --json
nexus agent info <pid> --json
nexus agent heartbeat <pid> --json
nexus agent transition <pid> ready --json
```

The daemon validates every state transition. External runtimes can register their actual process and connection metadata:

```sh
nexus agent register "Local worker" --owner-id alice --zone-id sharedzone --connection-id <connection> --host-pid <os-pid> --json
nexus agent signal <pid> SIGUSR1 --label steering=continue --json
nexus agent delete <pid> --yes --json
```

These commands use the native gRPC endpoint and the existing credential. The Python SDK exposes the same bindings through `nexus.remote.agent_client.AgentClient`, which owns and closes its channel with a `with` statement.

## Use the terminal interface

The TUI has Files, Search and a native HTTP Console. It verifies native status and whoami before opening the workspace. The Console uses the same credential.

From a checkout:

```sh
cd packages/nexus-api-client
npm ci
cd ../nexus-tui
bun install --frozen-lockfile
bun run start --url <native-http-origin> --search-url <owning-search-origin> --zone-id <zone>
```

The Python entry point is `nexus tui`. The terminal supports file editing, Unicode paths, binary preview, Search/discovery, explicit delete confirmation and native HTTP requests. Press `q` to quit and restore the terminal.

Build and verify an independently installable artifact with `bun run pack` and `bun run test:package`. Publishing a new package is a separate release step.

## Verify a local deployment

Use the [local acceptance guide](../testing/search-local-acceptance.md) for the feature inventory, actual validation receipts and assignable manual cases.

`scripts/test_search_local.py` exercises an isolated Rust daemon, signed Search plugin, node TLS, native credentials, full-tuple grants/revocation and actual CLI/terminal consumers. Its keyword mode needs no model-provider credential. The Docker publication workflow also tests the built SDK image, native host and local embedding model with the shared HERB corpus in `scripts/fixtures/herb.json`.

For individual command options, run `nexus <command> --help`. Use `--json` for structured command results and errors.

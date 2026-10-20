# Nexus TUI

A terminal client for the native Rust HTTP API. Files, Search and Console use one authenticated transport worker. Connection readiness requires both HTTP liveness and the credential identity returned by `/v2/auth/whoami`.

## Run

Install the shared API client, then the terminal package:

```sh
cd packages/nexus-api-client
npm ci
cd ../nexus-tui
bun install --frozen-lockfile
NEXUS_URL=http://127.0.0.1:2028 NEXUS_API_KEY='<native-key>' bun run start
```

`--url`, `--api-key`, `--search-url` and `--zone-id` provide explicit connection settings. Search can have a separate owning native host; its original bearer is preserved. The status bar displays the verified native principal. Human account login remains separate.

## Panels

- **1 Files**: browse directories, preview file bytes, edit UTF-8 text, create directories, write files, rename and delete. `g` opens a directory path; `j/k` select; Enter opens; `e` edits; `n` writes a path; `m` creates a directory; `v` renames; `d` asks for delete confirmation. The server applies native file permissions to every operation.
- **2 Search**: `/` opens a query; `m` cycles keyword, semantic and hybrid modes. `g:pattern` performs glob and `r:pattern` performs grep under the discovery root selected with `g`. The owning Rust host applies zone and ReBAC scope.
- **3 Console**: `i` opens `METHOD /v2/path [JSON body]`. The same transport sends the original credential. HTTP errors remain visible. For raw file writes, `PUT /v2/files?path=<encoded-path>` sends the text body as bytes. Console previews are limited to 64 KiB.

Example native administration requests:

```text
GET /v2/auth/whoami
GET /v2/auth/keys
POST /v2/auth/keys {"name":"reader","subject_type":"user","subject_id":"alice","zones":["root:r"],"admin":false,"allow_existing":false}
GET /v2/rebac/tuples?zone=root
POST /v2/rebac/tuples {"zone":"root","object_type":"file","object_id":"/notes.txt","relation":"viewer","subject_type":"user","subject_id":"alice"}
```

Revoke a key using `DELETE /v2/auth/keys/<encoded-key_hash>`. Revoke a relationship using `DELETE /v2/rebac/tuples` with the complete returned tuple as the JSON body, including any `subject_relation`. Certificate agents use the native agent lifecycle. Key and relationship management require the native privileges enforced by the server.

Editor saves require a valid native write receipt. Failed or late responses preserve a newer draft. Escape asks before discarding changes. Text editing is limited to 1 MiB; file previews show up to 64 KiB. Terminal resizing hides the interface below 60×24 while keeping its state alive; hidden panels do not process hotkeys. UI drafts, request state and Console results stay in memory.

## Validation

```sh
bun test
bun run lint
```

The standard suite drives the full terminal tree with real keystrokes, a transport worker and controlled HTTP faults. It covers Unicode input, binary transfers, editor acknowledgements, late saves, discard/delete confirmation, Search errors, native Console traffic and terminal resizing. Linux CI also starts the actual CLI inside tmux and checks its rendered output and exit status.

`tests/e2e/native-search.test.tsx` uses an isolated Rust daemon prepared by the repository's `scripts/test_search_local.py`. It verifies binary VFS CRUD, native identity, Search/discovery/indexing scope, and immediate key/relation revocation through the real worker and terminal renderer. The **Native TUI acceptance** CI job runs this journey against the pinned daemon/plugin. No Python server is used.

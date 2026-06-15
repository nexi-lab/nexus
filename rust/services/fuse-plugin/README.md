# nexus-fuse-plugin

In-process FUSE service plugin for `nexusd-cluster`. Loaded as a
signed cdylib via `--plugin-dir`; spawns a `fuser`-backed FUSE event
loop in a background thread and routes POSIX filesystem ops to Nexus
kernel syscalls through the plugin-abi KernelHandle (v2 ABI: `read /
write / stat / readdir / mkdir / unlink / rmdir / rename`).

This is the operator-facing layer that turns Nexus's VFS into
something `ls`, `cat`, `mv`, `vim`, `tar`, and every other POSIX
filesystem tool can talk to without a single client-side rewrite.

## Platform matrix

| Platform | FUSE userspace      | Status                                      |
|----------|---------------------|---------------------------------------------|
| Linux    | `libfuse3` (`fuse3`) | First-cut.  Production-ready.              |
| macOS    | `macFUSE`           | Same `fuser` body via cfg gate.            |
| Windows  | `WinFsp` (`winfsp` crate) | First-cut.  Cfg-gated under `target_os = "windows"`. |

`fuser`'s `Filesystem` trait and `spawn_mount2` are identical on
Linux + macOS, so the same source body (`src/fs.rs`) services both.
Windows takes a different shape: WinFsp's `FileSystemContext` trait
+ `FileSystemHost` mount lifecycle live in `src/fs_winfsp.rs`,
sharing the same path-translation + inode-tracking + kernel-callback
helpers (`crate::kernel_callbacks`, `crate::path_index`) with the
fuser path.

## Operator install

The FUSE userspace is an out-of-band dependency — install it before
launching `nexusd-cluster`:

```bash
# Debian / Ubuntu
sudo apt install fuse3 libfuse3-3
sudo usermod -a -G fuse "$USER"   # /dev/fuse access without root

# macOS (Homebrew)
brew install --cask macfuse
# Then approve the macFUSE kernel extension in System Settings →
# Privacy & Security and reboot.  macFUSE installs are interactive
# by design; there's no headless equivalent today.
```

```powershell
# Windows (Chocolatey or winget, as Administrator)
choco install winfsp -y
# OR
winget install --id WinFsp.WinFsp --silent --accept-package-agreements
#
# WinFsp installs winfsp-x64.dll under
# `C:\Program Files (x86)\WinFsp\bin\` and registers the .sys driver,
# but does NOT add that directory to system PATH.  The plugin DLL has
# `winfsp-x64.dll` in its static import table, so Windows
# `LoadLibraryExW` walks exe-dir → system32 → CWD → PATH at plugin
# load time and fails with error 126 ("module not found") when none
# of those resolves it.  Prepend the WinFsp bin dir to PATH for the
# shell that launches the daemon — Start-Process / spawn inherits it:
#
$env:PATH = "C:\Program Files (x86)\WinFsp\bin;$env:PATH"
nexusd-cluster --plugin-dir $env:USERPROFILE\.nexus\plugins ...
```

Drop the signed dylib + `.sig` from the latest `fuse-v*` release into
`$NEXUS_PLUGIN_DIR`:

```bash
mkdir -p ~/.nexus/plugins
gh release download fuse-v0.1.0 \
    --pattern 'nexus-fuse-plugin-linux-x86_64.tar.gz' \
    --dir /tmp/
tar -xzf /tmp/nexus-fuse-plugin-linux-x86_64.tar.gz -C ~/.nexus/plugins/
ls ~/.nexus/plugins/   # libnexus_fuse_plugin.so + libnexus_fuse_plugin.so.sig
```

`PluginLoader::load` Ed25519-verifies the `.sig` against the
embedded trust root before `dlopen` — an unsigned dylib is refused
at startup with no escape hatch.

## Configuration

The plugin reads two env vars at `create` time:

| Var                       | Required | Default | Meaning                                                                 |
|---------------------------|----------|---------|-------------------------------------------------------------------------|
| `NEXUS_FUSE_MOUNT_POINT`  | Yes      | —       | Mount target.  Linux / macOS: absolute path (`/mnt/cc-tasks`).  Windows: drive letter (`Z:`) or directory path; the runner reserves the letter for the lifetime of the cluster process. |
| `NEXUS_FUSE_VFS_ROOT`     | No       | `/`     | VFS-path prefix that maps to the FUSE mount root.  Joined with child names to produce kernel-side paths. |

Without `NEXUS_FUSE_MOUNT_POINT` the plugin loads but performs no
mount — useful for the kernel's `--plugin-dir` scanning to validate
the ABI without committing to a mount target.

Example launch:

```bash
export NEXUS_FUSE_MOUNT_POINT=/mnt/nexus
export NEXUS_FUSE_VFS_ROOT=/
nexusd-cluster \
    --plugin-dir ~/.nexus/plugins \
    --bind-addr 0.0.0.0:2126 \
    --data-dir ~/.nexus/data
```

After the daemon comes up, `mountpoint -q /mnt/nexus` returns 0 and
plain POSIX ops work end-to-end:

```bash
mkdir /mnt/nexus/foo
echo hello > /mnt/nexus/foo/bar.txt
ls /mnt/nexus/foo
cat /mnt/nexus/foo/bar.txt
mv /mnt/nexus/foo/bar.txt /mnt/nexus/foo/baz.txt
rm /mnt/nexus/foo/baz.txt
rmdir /mnt/nexus/foo
```

## Admin RPCs

The plugin exposes two methods via `nexus_service_dispatch`:

- `status` — returns `mounted` or `unmounted` so an operator can poll
  whether the background thread's `spawn_mount2` succeeded without
  scraping logs.
- `unmount` — graceful unmount + worker-thread join.  The plugin
  stays loaded but the mount is released; `mountpoint -q` returns
  non-zero afterwards.

Both are admin-level (no auth surface).  Plain-text UTF-8 payloads
on both legs.

## What's covered, what's not

**Covered (KernelHandle v2):** `lookup`, `getattr`, `open`, `release`,
`flush`, `read`, `write`, `readdir`, `mkdir`, `unlink`, `rmdir`,
`rename`, `create`.

**Surfaces ENOSYS:** `setattr`.  chmod / chown / utimes / truncate
have no kernel equivalent today — ReBAC owns permissions and
timestamps are kernel-managed.  Returning `ENOSYS` keeps the
contract honest until a kernel-side setattr surface lands.

**First-cut performance shape:** `read` pulls the **entire** file
via `sys_read` and slices by offset/size in the FUSE op.  `write`
honours `O_TRUNC` semantics only (offset=0 writes).  Offset-aware
`sys_read` / `sys_write` extensions are tracked as a follow-up
nexus-vfs PR.

## Regression coverage

`tests/e2e/docker/test_cc_tasks_share_e2e.py` mounts the signed dylib
on both founder + joiner inside privileged Linux containers and
exercises every v3 op via plain POSIX commands, with a gRPC
`vfs_stat` / `vfs_read` cross-check at every step.  The FUSE
workflows compose with LocalConnector + federation so the full Mac↔Win
`cc tasks list` chain — FUSE → kernel → DT_MOUNT routing → federation
fan-out → peer LocalConnector → host fs — has byte-exact regression
guard.  Runs in CI on every change to the plugin source, the
plugin-abi pin, the Dockerfile, or the test itself — see
`.github/workflows/cc-tasks-share-e2e.yml`.

Architectural decisions live in
`docs/superpowers/specs/2026-06-13-sealed-keystore-dogfood-design.md`
(signing) and the plugin source's module docstring (FUSE op
translation layer).

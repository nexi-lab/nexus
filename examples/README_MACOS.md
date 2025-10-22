# Running Nexus Demos on macOS

macFUSE on macOS requires kernel extension approval and restart, which can be cumbersome. Here are your alternatives:

## Option 1: CLI-Only Demo (Recommended for macOS) ✅

**No FUSE required!** Works perfectly on macOS without any special permissions.

```bash
bash examples/nexus_cli_demo_no_fuse.sh
```

This demo shows all key Nexus features using pure CLI commands:
- File operations (`nexus write`, `nexus cat`, `nexus ls`)
- Searching (`nexus grep`)
- Permissions (`nexus chmod`, ACLs with `setfacl`/`getfacl`)
- Export/import
- Everything works without mounting!

## Option 2: Use Remote Server

Connect to a Nexus server instead of using local FUSE:

```bash
python3 examples/remote_server_demo.py
```

This connects to `nexus.sudorouter.ai` (or your own server) over HTTP/HTTPS. No FUSE needed!

## Option 3: Docker Container

Run FUSE inside Docker (Linux environment):

```bash
# Create Dockerfile
cat > Dockerfile.nexus-fuse <<'EOF'
FROM ubuntu:22.04

RUN apt-get update && apt-get install -y \
    python3.11 \
    python3-pip \
    fuse3 \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install nexus-ai-fs[fuse]

WORKDIR /workspace
CMD ["/bin/bash"]
EOF

# Build and run
docker build -t nexus-fuse -f Dockerfile.nexus-fuse .
docker run -it --privileged --device /dev/fuse nexus-fuse

# Inside container:
bash examples/fuse_cli_demo.sh
```

## Option 4: E2B Sandbox (Linux in the Cloud)

Run the demo in an E2B Linux sandbox - works great for CI/CD!

**Note:** Currently, standard E2B sandboxes don't support FUSE due to kernel restrictions. We're working on a privileged template.

```bash
# For now, use the CLI demo which works everywhere:
bash examples/nexus_cli_demo_no_fuse.sh
```

## Option 5: Install macFUSE (If You Really Want FUSE)

Only if you absolutely need local FUSE mounting:

1. Download macFUSE from: https://osxfuse.github.io/
2. Install and restart your Mac
3. Approve the kernel extension in System Settings > Privacy & Security
4. Run the FUSE demo:

```bash
bash examples/fuse_cli_demo.sh
```

## Comparison

| Method | Pros | Cons | Setup Time |
|--------|------|------|------------|
| **CLI-Only** | ✓ No setup<br>✓ All features<br>✓ Fast | △ Different syntax | 0 min |
| **Remote Server** | ✓ No FUSE<br>✓ Multi-user | △ Needs server | 5 min |
| **Docker** | ✓ Full FUSE<br>✓ Isolated | △ Needs Docker | 10 min |
| **E2B** | ✓ Cloud-based<br>✓ No local deps | △ FUSE limited | 15 min |
| **macFUSE** | ✓ Native experience | ✗ Requires restart<br>✗ Kernel ext | 20 min |

## Recommended Workflow for macOS Users

1. **Start with CLI demo** - Get familiar with Nexus features
   ```bash
   bash examples/nexus_cli_demo_no_fuse.sh
   ```

2. **Try remote mode** - Test client-server architecture
   ```bash
   python3 examples/remote_server_demo.py
   ```

3. **Use Docker for FUSE** - When you need Unix tools integration
   ```bash
   docker run -it --privileged --device /dev/fuse nexus-fuse
   ```

4. **Install macFUSE last** - Only if you need native macOS mounting

## Quick Start (No FUSE)

The fastest way to try Nexus on macOS:

```bash
# Install
pip install nexus-ai-fs

# Initialize
nexus init /tmp/my-workspace

# Try it out
nexus write /workspace/hello.txt "Hello Nexus!"
nexus cat /workspace/hello.txt
nexus grep "Hello" --file-pattern "**/*.txt"
nexus ls /workspace --long
```

That's it! No FUSE, no macFUSE, just works. 🎉

## Questions?

- **Why doesn't FUSE work in E2B?** - E2B sandboxes run in userspace containers without kernel FUSE support
- **Do I need FUSE?** - No! The CLI provides all features. FUSE is just for Unix tools integration (grep, cat, vim, etc.)
- **What's the difference?** - FUSE lets you use `cat /mnt/file.txt` instead of `nexus cat /file.txt`

## More Examples

All examples work on macOS:
- `examples/embedded_demo.py` - Embedded SDK (no server needed)
- `examples/postgres_demo.py` - PostgreSQL backend
- `examples/permissions_demo.py` - ACL and permissions
- `examples/skills_demo.py` - Work queues and task management

Run any Python example with:
```bash
python3 examples/<example_name>.py
```

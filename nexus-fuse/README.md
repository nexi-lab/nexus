# Nexus FUSE

## Linux FUSE Passthrough For Large Reads

Passthrough is opt-in and is intended for raw, read-only large-file workloads on Linux kernels with FUSE passthrough support.

Example:

```bash
nexus-fuse mount /mnt/nexus \
  --url "$NEXUS_URL" \
  --api-key-file "$NEXUS_API_KEY_FILE" \
  --passthrough \
  --passthrough-pattern "/data/**" \
  --passthrough-threshold-bytes 131072
```

Fallback behavior:

- Without `--passthrough`, reads use the normal userspace path.
- On unsupported platforms, passthrough is disabled unless `--passthrough-require` is set.
- Files below the threshold, directories, denied patterns, and write opens use normal userspace reads.

Benchmark:

```bash
dd if=/mnt/nexus/data/one-gib.bin of=/dev/null bs=8M status=progress
```

The Criterion bench target is opt-in because it must read from a real mounted
passthrough file. Without the environment variable below, the target exits after
printing setup instructions instead of recording meaningless local numbers.

```bash
NEXUS_FUSE_PASSTHROUGH_BENCH_FILE=/mnt/nexus/data/one-gib.bin \
  cargo bench --bench passthrough_read -- --sample-size 10
```

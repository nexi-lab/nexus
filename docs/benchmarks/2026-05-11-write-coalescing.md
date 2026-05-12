# Write Coalescing Benchmark - Issue #4059

Commands:

```bash
cargo test -p kernel --bench write_coalescing burst_write_count_acceptance
cargo bench -p kernel --bench write_coalescing -- write_coalescing_100_write_burst
```

Workload:

- path: `/workspace/burst.txt`
- writes: 100
- payload: small full-file overwrite payloads
- strict policy: write-through
- latency policy: default 1 second window, 4 MiB byte budget
- completion: explicit final flush

Acceptance:

- strict backend writes: 100
- buffered backend writes: no more than 10
- reduction: at least 10x

Result:

- strict backend writes: 100
- buffered backend writes: 1
- reduction: 100x

# Operation Dispatch Registry

The operation dispatch registry selects an implementation for a filesystem
operation after VFS routing has identified the path and backend. It does not
replace mount routing, virtual path resolvers, intercept hooks, permission
checks, or observers.

Each handler is keyed by:

- operation name, such as `cat`, `grep`, `raw_read`, or `fingerprint`
- file type, such as `json` or `parquet`
- backend kind, such as `s3`, `slack`, or `github`

Resolution probes four keys in order:

1. `(op, filetype, backend)`
2. `(op, *, backend)`
3. `(op, filetype, *)`
4. `(op, *, *)`

Backends use `(op, *, backend)` for API pushdown, for example Slack grep.
Parsers use `(op, filetype, *)` for content rendering, for example JSON or
parquet cat. Defaults use `(op, *, *)`.

Boot registration is explicit:

1. default operations
2. parser operations
3. backend operations

Duplicate registration is rejected unless the caller uses the replace API.
This keeps boot order deterministic and makes override intent visible in
tests.

The Rust registry keeps built-in operation/filetype/backend keys in fixed
arrays with a precomputed resolved table for the hot path. Custom
`Other(...)` keys are stored separately and merged with the same specificity
rules only when present. Registration is mutable during boot; steady-state
resolution is lock-free. The `ops_registry_bench` Criterion benchmark tracks
the direct default path against registry lookup cost for hot-path changes.

To add an override:

1. Add or reuse a handler function.
2. Register it with the most specific key it needs.
3. Add a unit test for resolution order and a behavior test using a fake
   backend or parser dependency.
4. Run focused tests plus the operation registry benchmark when the handler
   affects hot paths.

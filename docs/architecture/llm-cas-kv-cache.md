# Message-Boundary CDC for LLM KV Cache Optimization

> Task #1589 sub-design. Explains how CAS + content-defined chunking at
> message boundaries enables shared prefix deduplication across LLM
> conversations, and how this maps to provider-side KV cache warming.

## Problem

LLM conversations are append-only: each turn adds a message to an
existing conversation. Two conversations sharing the first N messages
store those N messages redundantly in CAS (each conversation is a
separate blob with a different hash).

Provider-side KV caches (OpenAI, Anthropic, etc.) cache the key-value
activations of prompt prefixes. When two requests share a prefix, the
provider skips recomputation for the shared portion. But this only
works if the provider sees the same prefix bytes.

## Solution: MessageBoundaryStrategy

Instead of the default `CDCEngine` (Rabin fingerprint, 16MB threshold),
LLM conversations use a `MessageBoundaryStrategy` that chunks at
**message boundaries** in the conversation JSON.

```
ChunkingStrategy (Protocol)
  ├── CDCEngine             ← default: Rabin fingerprint, 16MB threshold
  └── MessageBoundaryStrategy  ← LLM: chunk per message, always-chunk mode
```

### How It Works

Conversation A: `[sys_prompt, user_1, assistant_1, user_2]`
Conversation B: `[sys_prompt, user_1, assistant_1, user_3]` (diverges at msg 4)

MessageBoundaryStrategy chunks each message independently:

```
A chunks: [hash(sys_prompt), hash(user_1), hash(assistant_1), hash(user_2)]
B chunks: [hash(sys_prompt), hash(user_1), hash(assistant_1), hash(user_3)]
                 identical         identical         identical      different
```

CAS dedup: chunks 1-3 are stored once. Only chunk 4 differs. The
manifest (chunk list) is different per conversation, but the underlying
chunk blobs are shared.

### Why Not Default CDC?

Default `CDCEngine` uses Rabin fingerprint with a **16MB threshold**
(`CDC_THRESHOLD_BYTES`). Conversations are < 1M tokens ~ 4MB text. This
is well below the threshold, so `CDCEngine.should_chunk()` returns False
and the entire conversation is stored as a single blob.

`MessageBoundaryStrategy` uses **always-chunk mode**: every conversation
is chunked regardless of size. The chunk boundary is the message
separator in the JSON array, not a content-defined fingerprint.

### CAS Storage Layout

```
cas/
├── ab/cd/abcd1234...          # chunk: sys_prompt (shared by A and B)
├── ab/cd/abcd1234...meta      # {"ref_count": 2, "is_chunk": true}
├── ef/gh/efgh5678...          # chunk: user_1 (shared)
├── ij/kl/ijkl9012...          # chunk: assistant_1 (shared)
├── mn/op/mnop3456...          # chunk: user_2 (A only)
├── qr/st/qrst7890...          # chunk: user_3 (B only)
├── AA/BB/AABB...              # manifest A (links to chunks 1-3 + user_2)
└── CC/DD/CCDD...              # manifest B (links to chunks 1-3 + user_3)
```

## Provider-Side KV Cache Warming

### Current State (v1)

CAS dedup reduces **storage** cost. Provider-side KV cache warming is
**not** implemented in v1. The provider sees full request JSON each time.

### Future Optimization

When sending a request to the LLM provider, detect shared prefix via
CAS chunk hashes:

1. Hash the first N messages of the new request
2. Check if these chunk hashes exist in CAS (bloom filter, ~0 cost)
3. If shared prefix detected, use provider-specific cache hints:
   - OpenAI: send same `seed` parameter for deterministic prefix routing
   - Anthropic: prompt caching API (`cache_control` breakpoints)
   - Custom: SudoRouter could accept chunk hashes directly for KV cache lookup

This turns CAS chunk hashes into a **cache key** for provider-side KV
caches. Two requests with identical chunk prefix hashes route to the
same cached KV state.

### Cost Reduction Model

```
Without prefix sharing:
  Request A: compute KV for [sys, u1, a1, u2]  → 4 message KV compute
  Request B: compute KV for [sys, u1, a1, u3]  → 4 message KV compute
  Total: 8 message KV computations

With prefix sharing:
  Request A: compute KV for [sys, u1, a1, u2]  → 4 message KV compute
  Request B: reuse KV for [sys, u1, a1] + compute [u3]  → 1 message KV compute
  Total: 5 message KV computations (37.5% reduction)
```

For agents with long system prompts and multi-turn conversations, the
system prompt is computed once and reused across all turns and sessions.

## Integration Point

`MessageBoundaryStrategy` implements `ChunkingStrategy` (Protocol) and
is injected into `CASOpenAIBackend` via CAS Feature DI:

```python
cdc = MessageBoundaryStrategy(backend=backend)
backend = CASOpenAIBackend(
    base_url="...", api_key="...",
    # CASBackend Feature DI:
    cdc_engine=cdc,
)
```

The CASBackend base class routes `write_content()` through
`cdc_engine.should_chunk()` → `write_chunked()` automatically. No
changes to the kernel dispatch or syscall path.

## References

- `src/nexus/backends/engines/cdc.py` — ChunkingStrategy protocol + CDCEngine
- `src/nexus/backends/base/cas_backend.py` — CAS Feature DI (cdc_engine param)
- `src/nexus/backends/compute/openai_compatible.py` — LLM backend
- Task #1589: LLM backend driver design
- Task #1681: Promote CDC to CASBackend base class (prerequisite, done)

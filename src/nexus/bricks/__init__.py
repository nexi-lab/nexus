"""Feature bricks — optional, removable, independently-testable modules.

Each sub-package is a self-contained brick that:
  - Implements exactly one Protocol (or a small set of related Protocols)
  - Has zero imports from other bricks
  - Declares dependencies in its constructor (DI, not config)
  - Can fail independently without crashing the system

Bricks are wired by ``factory.py`` (the Composition Root) and loaded
on demand via config gates.  See NEXUS-LEGO-ARCHITECTURE.md §3 for
the brick rules and lifecycle.

Current bricks:
  - context_manifest — Deterministic context pre-execution (Stripe Minions pattern)
  - delegation       — Agent identity delegation (COPY/CLEAN/SHARED modes)
  - discovery        — BM25-based MCP tool discovery
  - governance       — Anti-fraud & anti-collusion governance graphs
  - pay              — NexusPay credits + X402 payments
  - reputation       — Agent reputation & dispute resolution (Bayesian Beta)
  - sandbox          — Sandboxed code execution (Docker / E2B / Monty)
  - search           — Hot search daemon (semantic, BM25, Zoekt, hybrid fusion)
  - snapshot         — Transactional filesystem snapshots (begin/commit/rollback)
  - workflows        — Event-driven workflow engine
"""

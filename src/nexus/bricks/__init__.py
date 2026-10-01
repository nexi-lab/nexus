"""Feature bricks — optional, removable, independently-testable modules.

A brick is the *optional* tier. It is defined by a contract, not by a base
class — there is no ``Brick`` class to inherit, which is why the word can look
like loose vocabulary until you read what it buys:

  - Implements exactly one Protocol (or a small set of related Protocols)
  - Has zero imports from other bricks
  - Declares dependencies in its constructor (DI, not config)
  - Can fail independently without crashing the system

**A brick is not a ``nexus.services`` service.** Both are packages of domain
logic and the names sound interchangeable, so the distinction is worth stating
where it can be seen:

  - ``nexus.services`` — kernel-coupled. Sync, lifecycle, workspace. Always
    started; the system does not run without them.
  - ``nexus.bricks`` — feature-gated. Loaded on demand, removable, allowed to
    fail alone.

The separation is enforced, not aspirational: ``.pre-commit-hooks/
check_brick_imports.py`` fails the build on a brick importing ``nexus.core``,
``nexus.services``, or another brick. It runs in pre-commit and in CI
("Brick Import Boundary Check"). That checker is the authority on what a
brick may reach for — read it before proposing that the two tiers be merged
or the term retired.

Bricks are wired by ``factory.py`` (the Composition Root) and loaded on demand
via config gates. The long-form rationale — four-tier model, lifecycle, how to
add one — is ``docs/archive/design/NEXUS-LEGO-ARCHITECTURE.md`` §3. Read its
principles, not its tables: §3.3's rules are what the checker above enforces,
while its brick catalog and readiness columns are counts from an older tree.
The tree and the checker are authoritative over both.

The set of bricks is the set of sub-packages here. A list kept by hand in
this docstring would go stale the first time someone adds one, so there
isn't one.
"""

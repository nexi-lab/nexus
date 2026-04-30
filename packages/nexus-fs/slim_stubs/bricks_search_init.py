"""Package marker for nexus.bricks.search in the slim (nexus-fs) wheel.

The full bricks/search/__init__.py eagerly imports chunking, search_service,
and other full-runtime modules that are excluded from the slim wheel.
This stub ships instead — it makes nexus.bricks.search importable so that
nexus.bricks.search.primitives (glob_helpers, trigram_fast) can be reached
without triggering the full-runtime import chain.
"""

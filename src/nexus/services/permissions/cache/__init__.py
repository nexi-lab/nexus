"""Cache subsystem for ReBAC permissions.

Contains multi-layer caching infrastructure including:
- Tiger Cache: Pre-materialized permissions as Roaring Bitmaps
- Result cache, boundary cache, visibility cache, etc.
"""

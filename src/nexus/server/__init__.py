"""Nexus HTTP server with S3-compatible API.

This module provides an HTTP server that exposes Nexus filesystem operations
through an S3-compatible API. This allows standard S3 clients and tools
(like rclone) to interact with Nexus without modifications.
"""

from nexus.server.api import APIRequestHandler, NexusHTTPServer
from nexus.server.auth import Credentials, SigV4Validator, create_simple_credentials_store

__all__ = [
    "NexusHTTPServer",
    "APIRequestHandler",
    "SigV4Validator",
    "Credentials",
    "create_simple_credentials_store",
]

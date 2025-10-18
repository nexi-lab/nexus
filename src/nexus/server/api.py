"""HTTP API server for Nexus filesystem using S3-compatible protocol.

This module implements an HTTP server that exposes Nexus filesystem operations
using S3-style API conventions. This allows tools like rclone to interact with
Nexus using familiar S3 protocols without requiring modifications to rclone.

Supported Operations:
- ListObjectsV2: List files in Nexus
- GetObject: Read file content
- PutObject: Write file content
- DeleteObject: Delete a file
- HeadObject: Get file metadata

Authentication:
- AWS Signature Version 4 (SigV4)
- Compatible with standard S3 clients and tools

Usage with rclone:
    rclone config create nexus s3 \\
        provider=Other \\
        endpoint=http://localhost:8080 \\
        access_key_id=YOUR_KEY \\
        secret_access_key=YOUR_SECRET \\
        force_path_style=true
"""

from __future__ import annotations

import hashlib
import logging
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from nexus import NexusFilesystem
from nexus.core.exceptions import NexusError, NexusFileNotFoundError
from nexus.server.auth import SigV4Validator

logger = logging.getLogger(__name__)


class APIRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for S3-style API.

    Implements S3 API conventions for Nexus filesystem operations.
    """

    # Class-level attributes set by server
    nexus_fs: NexusFilesystem
    auth_validator: SigV4Validator
    bucket_name: str = "nexus"

    def log_message(self, format: str, *args: Any) -> None:
        """Override to use Python logging instead of stderr."""
        logger.info(f"{self.address_string()} - {format % args}")

    def do_GET(self) -> None:
        """Handle GET requests (ListObjectsV2, GetObject)."""
        try:
            # Validate authentication
            valid, error = self._validate_auth()
            if not valid:
                self._send_error_response(403, "AccessDenied", error or "Access denied")
                return

            # Parse URL
            parsed = urlparse(self.path)
            path_parts = parsed.path.strip("/").split("/", 1)

            # Check if this is a bucket operation (list) or object operation (get)
            if len(path_parts) == 1 and path_parts[0] == self.bucket_name:
                # List operation
                query_params = parse_qs(parsed.query)
                self._handle_list_objects(query_params)
            elif len(path_parts) == 2 and path_parts[0] == self.bucket_name:
                # Get object operation
                object_key = unquote(path_parts[1])
                self._handle_get_object(object_key)
            else:
                self._send_error_response(404, "NoSuchBucket", "Invalid path")

        except Exception as e:
            logger.exception("Error handling GET request")
            self._send_error_response(500, "InternalError", str(e))

    def do_HEAD(self) -> None:
        """Handle HEAD requests (HeadBucket, HeadObject)."""
        try:
            # Validate authentication
            valid, error = self._validate_auth()
            if not valid:
                self._send_error_response(403, "AccessDenied", error or "Access denied")
                return

            # Parse URL
            parsed = urlparse(self.path)
            path_parts = parsed.path.strip("/").split("/", 1)

            # Check if this is HeadBucket (HEAD to bucket root)
            if len(path_parts) == 1 and path_parts[0] == self.bucket_name:
                # HeadBucket - return 200 (bucket exists)
                self.send_response(200)
                self.end_headers()
            elif len(path_parts) == 2 and path_parts[0] == self.bucket_name:
                object_key = unquote(path_parts[1])
                self._handle_head_object(object_key)
            else:
                self._send_error_response(404, "NotFound", "Not found")

        except Exception as e:
            logger.exception("Error handling HEAD request")
            self._send_error_response(500, "InternalError", str(e))

    def do_PUT(self) -> None:
        """Handle PUT requests (CreateBucket, PutObject)."""
        try:
            # Read request body
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else b""

            # Validate authentication with payload
            valid, error = self._validate_auth(payload=body)
            if not valid:
                self._send_error_response(403, "AccessDenied", error or "Access denied")
                return

            # Parse URL
            parsed = urlparse(self.path)
            path_parts = parsed.path.strip("/").split("/", 1)

            # Check if this is a CreateBucket request (PUT to bucket root)
            if len(path_parts) == 1 and path_parts[0] == self.bucket_name:
                # CreateBucket - just return success (bucket always exists)
                self._handle_create_bucket()
            elif len(path_parts) == 2 and path_parts[0] == self.bucket_name:
                object_key = unquote(path_parts[1])
                self._handle_put_object(object_key, body)
            else:
                self._send_error_response(404, "NoSuchBucket", "Invalid path")

        except Exception as e:
            logger.exception("Error handling PUT request")
            self._send_error_response(500, "InternalError", str(e))

    def do_DELETE(self) -> None:
        """Handle DELETE requests (DeleteObject)."""
        try:
            # Validate authentication
            valid, error = self._validate_auth()
            if not valid:
                self._send_error_response(403, "AccessDenied", error or "Access denied")
                return

            # Parse URL
            parsed = urlparse(self.path)
            path_parts = parsed.path.strip("/").split("/", 1)

            if len(path_parts) == 2 and path_parts[0] == self.bucket_name:
                object_key = unquote(path_parts[1])
                self._handle_delete_object(object_key)
            else:
                self._send_error_response(404, "NoSuchBucket", "Invalid path")

        except Exception as e:
            logger.exception("Error handling DELETE request")
            self._send_error_response(500, "InternalError", str(e))

    def _validate_auth(self, payload: bytes = b"") -> tuple[bool, str | None]:
        """Validate request authentication using SigV4.

        Args:
            payload: Request body bytes

        Returns:
            Tuple of (is_valid, error_message)
        """
        # Build full URL
        host = self.headers.get("Host", "")
        scheme = "http"  # TODO: Support HTTPS
        full_url = f"{scheme}://{host}{self.path}"

        # Convert headers to dict
        headers = {k: v for k, v in self.headers.items()}

        # Validate signature
        return self.auth_validator.validate_request(
            method=self.command,
            url=full_url,
            headers=headers,
            payload=payload,
        )

    def _handle_list_objects(self, query_params: dict[str, list[str]]) -> None:
        """Handle list operation (ListObjectsV2 format).

        Args:
            query_params: Parsed query parameters
        """
        # Parse parameters
        prefix = query_params.get("prefix", [""])[0]
        max_keys = int(query_params.get("max-keys", ["1000"])[0])
        continuation_token = query_params.get("continuation-token", [None])[0]
        start_after = query_params.get("start-after", [None])[0]

        # Ensure prefix starts with /
        if prefix and not prefix.startswith("/"):
            prefix = "/" + prefix

        try:
            # List files from Nexus
            files_list = self.nexus_fs.list(prefix or "/", recursive=True, details=True)

            # Filter files
            filtered_files = []
            for file_info in files_list:
                # Remove leading / for S3 compatibility
                s3_key = file_info["path"].lstrip("/")

                # Apply filters
                if start_after and s3_key <= start_after:
                    continue
                if continuation_token and s3_key <= continuation_token:
                    continue

                filtered_files.append((s3_key, file_info))

            # Sort by key
            filtered_files.sort(key=lambda x: x[0])

            # Apply max_keys limit
            is_truncated = len(filtered_files) > max_keys
            filtered_files = filtered_files[:max_keys]

            # Build response XML
            root = ET.Element("ListBucketResult", xmlns="http://s3.amazonaws.com/doc/2006-03-01/")

            ET.SubElement(root, "Name").text = self.bucket_name
            ET.SubElement(root, "Prefix").text = prefix.lstrip("/")
            ET.SubElement(root, "KeyCount").text = str(len(filtered_files))
            ET.SubElement(root, "MaxKeys").text = str(max_keys)
            ET.SubElement(root, "IsTruncated").text = str(is_truncated).lower()

            # Add file entries
            for s3_key, file_info in filtered_files:
                contents = ET.SubElement(root, "Contents")
                ET.SubElement(contents, "Key").text = s3_key
                ET.SubElement(contents, "LastModified").text = file_info["modified_at"].strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z"
                )
                ET.SubElement(contents, "ETag").text = f'"{file_info["etag"]}"'
                ET.SubElement(contents, "Size").text = str(file_info["size"])
                ET.SubElement(contents, "StorageClass").text = "STANDARD"

            # Set next continuation token if truncated
            if is_truncated:
                next_token = filtered_files[-1][0]
                ET.SubElement(root, "NextContinuationToken").text = next_token

            # Convert to XML
            xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)

            # Send response
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(xml_bytes)))
            self.end_headers()
            self.wfile.write(xml_bytes)

        except NexusError as e:
            logger.error(f"Nexus error in list operation: {e}")
            self._send_error_response(500, "InternalError", str(e))

    def _handle_get_object(self, object_key: str) -> None:
        """Handle get object operation.

        Args:
            object_key: Object key (without leading /)
        """
        # Convert to Nexus path
        nexus_path = "/" + object_key

        try:
            # Read file
            content = self.nexus_fs.read(nexus_path)

            # Get metadata if available
            file_meta = None
            try:
                from nexus.core.nexus_fs import NexusFS

                if isinstance(self.nexus_fs, NexusFS):
                    file_meta = self.nexus_fs.metadata.get(nexus_path)
            except Exception:
                pass

            # Compute ETag
            etag = hashlib.md5(content).hexdigest()
            if file_meta and file_meta.etag:
                etag = file_meta.etag

            # Send response
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("ETag", f'"{etag}"')

            if file_meta and file_meta.modified_at:
                last_modified = file_meta.modified_at.strftime("%a, %d %b %Y %H:%M:%S GMT")
                self.send_header("Last-Modified", last_modified)

            self.end_headers()
            self.wfile.write(content)

        except NexusFileNotFoundError:
            self._send_error_response(404, "NoSuchKey", "The specified key does not exist")
        except NexusError as e:
            logger.error(f"Nexus error in get operation: {e}")
            self._send_error_response(500, "InternalError", str(e))

    def _handle_head_object(self, object_key: str) -> None:
        """Handle head object operation.

        Args:
            object_key: Object key (without leading /)
        """
        # Convert to Nexus path
        nexus_path = "/" + object_key

        try:
            # Check existence
            if not self.nexus_fs.exists(nexus_path):
                self.send_response(404)
                self.end_headers()
                return

            # Get metadata
            file_meta = None
            try:
                from nexus.core.nexus_fs import NexusFS

                if isinstance(self.nexus_fs, NexusFS):
                    file_meta = self.nexus_fs.metadata.get(nexus_path)
            except Exception:
                pass

            if not file_meta:
                self.send_response(404)
                self.end_headers()
                return

            # Send headers only
            self.send_response(200)
            self.send_header("Content-Type", file_meta.mime_type or "application/octet-stream")
            self.send_header("Content-Length", str(file_meta.size))

            if file_meta.etag:
                self.send_header("ETag", f'"{file_meta.etag}"')

            if file_meta.modified_at:
                last_modified = file_meta.modified_at.strftime("%a, %d %b %Y %H:%M:%S GMT")
                self.send_header("Last-Modified", last_modified)

            self.end_headers()

        except NexusError as e:
            logger.error(f"Nexus error in head operation: {e}")
            self.send_response(500)
            self.end_headers()

    def _handle_create_bucket(self) -> None:
        """Handle create bucket operation.

        In Nexus, the bucket always exists (it's a virtual construct),
        so this is a no-op that returns success.
        """
        # Return 200 OK (bucket already exists / created)
        self.send_response(200)
        self.end_headers()

    def _handle_put_object(self, object_key: str, content: bytes) -> None:
        """Handle put object operation.

        Args:
            object_key: Object key (without leading /)
            content: File content
        """
        # Convert to Nexus path
        nexus_path = "/" + object_key

        try:
            # Write to Nexus
            self.nexus_fs.write(nexus_path, content)

            # Compute ETag
            etag = hashlib.md5(content).hexdigest()

            # Send response
            self.send_response(200)
            self.send_header("ETag", f'"{etag}"')
            self.end_headers()

        except NexusError as e:
            logger.error(f"Nexus error in put operation: {e}")
            self._send_error_response(500, "InternalError", str(e))

    def _handle_delete_object(self, object_key: str) -> None:
        """Handle delete object operation.

        Args:
            object_key: Object key (without leading /)
        """
        # Convert to Nexus path
        nexus_path = "/" + object_key

        try:
            # Delete from Nexus
            if self.nexus_fs.exists(nexus_path):
                self.nexus_fs.delete(nexus_path)

            # Return 204
            self.send_response(204)
            self.end_headers()

        except NexusError as e:
            logger.error(f"Nexus error in delete operation: {e}")
            self._send_error_response(500, "InternalError", str(e))

    def _send_error_response(self, status_code: int, error_code: str, message: str) -> None:
        """Send error response in XML format.

        Args:
            status_code: HTTP status code
            error_code: Error code string
            message: Error message
        """
        # Build error XML
        root = ET.Element("Error")
        ET.SubElement(root, "Code").text = error_code
        ET.SubElement(root, "Message").text = message
        ET.SubElement(root, "RequestId").text = "nexus-1"

        xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)

        # Send response
        self.send_response(status_code)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(xml_bytes)))
        self.end_headers()
        self.wfile.write(xml_bytes)


class NexusHTTPServer:
    """HTTP server for Nexus with S3-compatible API.

    Provides S3-compatible endpoints for Nexus filesystem operations.
    """

    def __init__(
        self,
        nexus_fs: NexusFilesystem,
        auth_validator: SigV4Validator,
        host: str = "0.0.0.0",
        port: int = 8080,
        bucket_name: str = "nexus",
    ):
        """Initialize server.

        Args:
            nexus_fs: Nexus filesystem instance
            auth_validator: SigV4 authentication validator
            host: Server host
            port: Server port
            bucket_name: Virtual bucket name
        """
        self.nexus_fs = nexus_fs
        self.auth_validator = auth_validator
        self.host = host
        self.port = port
        self.bucket_name = bucket_name

        # Create HTTP server
        self.server = HTTPServer((host, port), APIRequestHandler)

        # Configure handler
        APIRequestHandler.nexus_fs = nexus_fs
        APIRequestHandler.auth_validator = auth_validator
        APIRequestHandler.bucket_name = bucket_name

    def serve_forever(self) -> None:
        """Start server and handle requests."""
        logger.info(f"Starting Nexus HTTP server on {self.host}:{self.port}")
        logger.info(f"Virtual bucket: {self.bucket_name}")
        logger.info(f"Endpoint: http://{self.host}:{self.port}")
        try:
            self.server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Server stopped by user")
            self.shutdown()

    def shutdown(self) -> None:
        """Shutdown server gracefully."""
        logger.info("Shutting down server...")
        self.server.shutdown()
        self.server.server_close()
        if hasattr(self.nexus_fs, "close"):
            self.nexus_fs.close()
        logger.info("Server stopped")

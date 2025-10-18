"""Document type detection utilities."""

import gzip
import io
import logging
import zipfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def detect_mime_type(content: bytes, file_path: str | None = None) -> str | None:
    """Detect MIME type of file content.

    Uses python-magic if available, falls back to extension-based detection.

    Args:
        content: File content as bytes
        file_path: Optional file path for extension-based fallback

    Returns:
        MIME type string, or None if detection fails
    """
    # Try python-magic first
    try:
        import magic

        mime = magic.Magic(mime=True)
        mime_type = mime.from_buffer(content)
        logger.debug(f"Detected MIME type using magic: {mime_type}")
        return str(mime_type)
    except ImportError:
        logger.debug("python-magic not available, using extension-based detection")
    except Exception as e:
        logger.warning(f"Failed to detect MIME type with magic: {e}")

    # Fallback to extension-based detection
    if file_path:
        return _detect_mime_from_extension(file_path)

    return None


def _detect_mime_from_extension(file_path: str) -> str | None:
    """Detect MIME type from file extension.

    Args:
        file_path: Path to the file

    Returns:
        MIME type string, or None if unknown
    """
    import mimetypes

    mime_type, _ = mimetypes.guess_type(file_path)
    if mime_type:
        logger.debug(f"Detected MIME type from extension: {mime_type}")
    return mime_type


def detect_encoding(content: bytes) -> str:
    """Detect text encoding of file content.

    Uses chardet if available, falls back to UTF-8.

    Args:
        content: File content as bytes

    Returns:
        Encoding name (e.g., 'utf-8', 'ascii', 'latin-1')
    """
    # Try chardet first
    try:
        import chardet

        result = chardet.detect(content)
        encoding = result.get("encoding")
        confidence = result.get("confidence", 0)

        if encoding and confidence > 0.7:
            logger.debug(f"Detected encoding: {encoding} (confidence: {confidence:.2f})")
            return str(encoding)
        else:
            logger.debug(f"Low confidence encoding detection: {encoding} ({confidence:.2f})")
    except ImportError:
        logger.debug("chardet not available, using UTF-8 default")
    except Exception as e:
        logger.warning(f"Failed to detect encoding: {e}")

    # Default to UTF-8
    return "utf-8"


def is_compressed(file_path: str) -> bool:
    """Check if a file is compressed based on extension.

    Args:
        file_path: Path to the file

    Returns:
        True if the file appears to be compressed
    """
    ext = Path(file_path).suffix.lower()
    return ext in {".gz", ".gzip", ".zip", ".bz2", ".xz"}


def decompress_content(content: bytes, file_path: str) -> tuple[bytes, str | None]:
    """Decompress file content if it's compressed.

    Args:
        content: File content as bytes
        file_path: Path to the file (for extension detection)

    Returns:
        Tuple of (decompressed_content, inner_filename)
        If not compressed, returns original content and None

    Raises:
        ValueError: If compression format is unsupported or decompression fails
    """
    ext = Path(file_path).suffix.lower()

    # Handle gzip
    if ext in {".gz", ".gzip"}:
        try:
            decompressed = gzip.decompress(content)
            # Try to extract original filename (strip .gz extension)
            inner_name = Path(file_path).stem
            logger.debug(f"Decompressed gzip file, original size: {len(decompressed)} bytes")
            return decompressed, inner_name
        except Exception as e:
            raise ValueError(f"Failed to decompress gzip file: {e}") from e

    # Handle zip
    elif ext == ".zip":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                # Get list of files in archive
                names = zf.namelist()

                if not names:
                    raise ValueError("ZIP archive is empty")

                # If single file, extract it
                if len(names) == 1:
                    inner_name = names[0]
                    decompressed = zf.read(inner_name)
                    logger.debug(
                        f"Extracted single file from ZIP: {inner_name} ({len(decompressed)} bytes)"
                    )
                    return decompressed, inner_name
                else:
                    # Multiple files - not supported for now
                    raise ValueError(
                        f"ZIP archive contains {len(names)} files. "
                        "Multi-file archives are not supported."
                    )
        except zipfile.BadZipFile as e:
            raise ValueError(f"Invalid ZIP file: {e}") from e
        except Exception as e:
            raise ValueError(f"Failed to decompress ZIP file: {e}") from e

    # Handle bz2
    elif ext == ".bz2":
        try:
            import bz2

            decompressed = bz2.decompress(content)
            inner_name = Path(file_path).stem
            logger.debug(f"Decompressed bz2 file, original size: {len(decompressed)} bytes")
            return decompressed, inner_name
        except Exception as e:
            raise ValueError(f"Failed to decompress bz2 file: {e}") from e

    # Handle xz/lzma
    elif ext == ".xz":
        try:
            import lzma

            decompressed = lzma.decompress(content)
            inner_name = Path(file_path).stem
            logger.debug(f"Decompressed xz file, original size: {len(decompressed)} bytes")
            return decompressed, inner_name
        except Exception as e:
            raise ValueError(f"Failed to decompress xz file: {e}") from e

    # Not compressed
    return content, None


def prepare_content_for_parsing(
    content: bytes, file_path: str
) -> tuple[bytes, str, dict[str, Any]]:
    """Prepare file content for parsing.

    Handles decompression, MIME type detection, and encoding detection.

    Args:
        content: Raw file content
        file_path: Path to the file

    Returns:
        Tuple of (processed_content, effective_path, metadata)
        - processed_content: Decompressed content if needed
        - effective_path: Path to parse (inner file if compressed)
        - metadata: Dict with mime_type, encoding, and compression info
    """
    metadata: dict[str, Any] = {}

    # Check for compression
    if is_compressed(file_path):
        try:
            content, inner_name = decompress_content(content, file_path)
            metadata["compressed"] = True
            metadata["original_path"] = file_path

            # Update effective path to inner file
            if inner_name:
                file_path = inner_name
                metadata["inner_filename"] = inner_name

        except ValueError as e:
            logger.warning(f"Failed to decompress {file_path}: {e}")
            metadata["compression_error"] = str(e)

    # Detect MIME type
    mime_type = detect_mime_type(content, file_path)
    if mime_type:
        metadata["mime_type"] = mime_type

    # Detect encoding for text files
    if mime_type and mime_type.startswith("text/"):
        encoding = detect_encoding(content)
        metadata["encoding"] = encoding

    return content, file_path, metadata

"""Byte output for CLI pipelines."""

import errno
import os
import stat
import sys
from collections.abc import Iterable


def _emit(content: bytes | None) -> bool:
    output = sys.stdout.buffer
    try:
        if content is None:
            output.flush()
        else:
            output.write(content)
    except OSError as exc:
        # Windows reports a closed pipe as EINVAL. Restrict that case to an
        # actual pipe so invalid output files still report their error.
        if exc.errno != errno.EPIPE and not (
            os.name == "nt"
            and exc.errno == errno.EINVAL
            and stat.S_ISFIFO(os.fstat(output.fileno()).st_mode)
        ):
            raise
        # Discard the remaining buffer at interpreter shutdown too.
        with open(os.devnull, "wb") as sink:
            os.dup2(sink.fileno(), output.fileno())
        return False
    return True


def write_stdout(chunks: Iterable[bytes]) -> None:
    """Write exact bytes, stopping when the downstream reader closes."""
    for chunk in chunks:
        if not _emit(chunk):
            return
    _emit(None)

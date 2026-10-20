"""A file read error must remain visible when writing a pipeline."""

import errno

import pytest

from nexus.cli.io import write_stdout


def test_input_error_is_not_mistaken_for_a_closed_output_pipe():
    failure = OSError(errno.EINVAL, "native read rejected its range")

    def chunks():
        yield b"valid bytes"
        raise failure

    with pytest.raises(OSError) as caught:
        write_stdout(chunks())
    assert caught.value is failure

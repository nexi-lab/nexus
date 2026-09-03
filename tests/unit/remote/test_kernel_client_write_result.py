"""``_SysWriteResult.is_new`` derived from ``gen`` over the typed Write RPC (Issue #4739).

The kernel's ``WriteResponse`` proto exposes only ``content_id``/``size``/``gen``.
The kernel sets ``gen = old_gen + 1`` and ``old_gen = 0`` when no entry existed,
so ``gen == 1`` is its own ``is_new`` condition.  The post-write permission
hooks key the creator's ``direct_owner`` grant on ``is_new_file``.
"""

from __future__ import annotations

import pytest

from nexus.remote.kernel_client import _SysWriteResult


@pytest.mark.parametrize(
    ("gen", "expected"),
    [
        (0, False),  # error / miss shape
        (1, True),  # first write to the path
        (2, False),  # overwrite
        (7, False),
    ],
)
def test_is_new_follows_generation(gen: int, expected: bool) -> None:
    result = _SysWriteResult(content_id="cid", size=3, gen=gen)
    assert result.is_new is expected
    assert result.gen == gen
    assert result.hit is True
    assert result.post_hook_needed is True


def test_default_result_is_not_new() -> None:
    assert _SysWriteResult().is_new is False

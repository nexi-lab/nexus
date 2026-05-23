"""distribute.py: append idempotent contract appendix to a subissue body."""

from scripts.surface_coverage.distribute import (
    APPENDIX_BEGIN,
    APPENDIX_END,
    apply_appendix,
    build_appendix,
)


def test_apply_to_clean_body():
    body = "# Original body\n\nSome content.\n"
    new = apply_appendix(body, build_appendix(issue_number=4123, owned_op_ids=["fs.read"]))
    assert "Original body" in new
    assert APPENDIX_BEGIN in new
    assert APPENDIX_END in new
    assert "fs.read" in new
    assert "Surface coverage contract" in new


def test_apply_is_idempotent():
    body = "# Original\n"
    a = apply_appendix(body, build_appendix(issue_number=4123, owned_op_ids=["fs.read"]))
    b = apply_appendix(a, build_appendix(issue_number=4123, owned_op_ids=["fs.read"]))
    assert a == b


def test_apply_replaces_existing_appendix():
    body = "# Original\n"
    once = apply_appendix(body, build_appendix(issue_number=4123, owned_op_ids=["fs.read"]))
    twice = apply_appendix(
        once, build_appendix(issue_number=4123, owned_op_ids=["fs.read", "fs.write"])
    )
    assert "fs.write" in twice
    # only one appendix block
    assert twice.count(APPENDIX_BEGIN) == 1
    assert twice.count(APPENDIX_END) == 1


def test_apply_preserves_original_below_appendix():
    """Appendix is appended at the very end; surrounding body content untouched."""
    body = "Line 1\nLine 2\n"
    new = apply_appendix(body, build_appendix(issue_number=4123, owned_op_ids=[]))
    assert new.startswith("Line 1\nLine 2\n")

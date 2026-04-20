"""Unit test for the entrypoint's AWS_PROFILE INI-header matcher.

The matcher must handle:
  * ``[name]`` form (credentials file)
  * ``[profile name]`` form (config file)
  * Profile names with regex metacharacters (``prod.us``, ``a+b``)
  * Whitespace variation inside ``[profile   name]``
  * Trailing whitespace on the header line
  * Partial-match false positives (must NOT match)
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[3] / "dockerfiles" / "docker-entrypoint.sh"


def _extract_matcher_function() -> str:
    """Pull just the ``_aws_profile_file_has_name`` function from the entrypoint."""
    lines = ENTRYPOINT.read_text().splitlines()
    buf: list[str] = []
    inside = False
    for line in lines:
        if line.startswith("_aws_profile_file_has_name() {"):
            inside = True
        if inside:
            buf.append(line)
            if line == "}":
                break
    if not buf:
        raise RuntimeError(
            "Could not locate _aws_profile_file_has_name function in docker-entrypoint.sh"
        )
    return "\n".join(buf) + "\n"


def _has_profile(tmp_file: Path, wanted: str, fn_file: Path) -> bool:
    """Invoke bash against the extracted helper and return the match result."""
    script = f"""
    source {fn_file}
    if _aws_profile_file_has_name {tmp_file!s} {wanted!r}; then
        echo FOUND
    else
        echo MISSING
    fi
    """
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip().endswith("FOUND")


@pytest.fixture(scope="module")
def fn_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("aws-matcher") / "matcher.sh"
    path.write_text(_extract_matcher_function())
    return path


@pytest.fixture
def bash_available() -> None:
    if shutil.which("bash") is None:
        pytest.skip("bash not available")


def _write(tmp_path: Path, content: str) -> Path:
    f = tmp_path / "creds"
    f.write_text(content)
    return f


def test_matches_bracketed_name(tmp_path: Path, bash_available: None, fn_file: Path) -> None:
    f = _write(tmp_path, "[prod]\naws_access_key_id=x\n")
    assert _has_profile(f, "prod", fn_file) is True


def test_matches_profile_name_form(tmp_path: Path, bash_available: None, fn_file: Path) -> None:
    f = _write(tmp_path, "[profile prod]\nregion=us-east-1\n")
    assert _has_profile(f, "prod", fn_file) is True


def test_profile_name_with_dot_must_match_exactly(
    tmp_path: Path, bash_available: None, fn_file: Path
) -> None:
    """``prod.us`` should match ``[prod.us]`` — but NOT ``[prodXus]`` etc.

    Regex-based search with raw interpolation would match anything-dot-anything;
    an exact match must not.
    """
    f = _write(tmp_path, "[prodXus]\naws_access_key_id=x\n")
    assert _has_profile(f, "prod.us", fn_file) is False


def test_profile_name_with_regex_metachars_matches_only_exact(
    tmp_path: Path, bash_available: None, fn_file: Path
) -> None:
    f = _write(tmp_path, "[a+b]\naws_access_key_id=x\n")
    assert _has_profile(f, "a+b", fn_file) is True
    # Regex interpretation would have matched [aaab] etc. — must not.
    f2 = _write(tmp_path, "[aaab]\n")
    assert _has_profile(f2, "a+b", fn_file) is False


def test_whitespace_around_profile_keyword_tolerated(
    tmp_path: Path, bash_available: None, fn_file: Path
) -> None:
    f = _write(tmp_path, "[profile   prod]\nregion=us-east-1\n")
    assert _has_profile(f, "prod", fn_file) is True


def test_trailing_whitespace_on_header_tolerated(
    tmp_path: Path, bash_available: None, fn_file: Path
) -> None:
    f = _write(tmp_path, "[prod]   \naws_access_key_id=x\n")
    assert _has_profile(f, "prod", fn_file) is True


def test_partial_name_is_not_a_match(tmp_path: Path, bash_available: None, fn_file: Path) -> None:
    f = _write(tmp_path, "[production]\n")
    assert _has_profile(f, "prod", fn_file) is False


def test_missing_profile_returns_false(tmp_path: Path, bash_available: None, fn_file: Path) -> None:
    f = _write(tmp_path, "[other]\n")
    assert _has_profile(f, "prod", fn_file) is False


def test_empty_file_returns_false(tmp_path: Path, bash_available: None, fn_file: Path) -> None:
    f = _write(tmp_path, "")
    assert _has_profile(f, "prod", fn_file) is False


def test_header_on_final_line_without_trailing_newline_matches(
    tmp_path: Path, bash_available: None, fn_file: Path
) -> None:
    """Hand-edited AWS config files often lack a trailing newline on the
    last line. A bare ``while read`` loop drops that line silently, which
    would look like a missing profile and trip the fail-closed guard.
    """
    # Note: no "\n" at end — deliberate.
    f = _write(tmp_path, "[prod]")
    assert _has_profile(f, "prod", fn_file) is True


def test_profile_keyword_form_on_final_line_without_newline_matches(
    tmp_path: Path, bash_available: None, fn_file: Path
) -> None:
    f = _write(tmp_path, "[profile prod]")
    assert _has_profile(f, "prod", fn_file) is True

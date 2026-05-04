from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "permissions_demo_enhanced.sh"


def test_successful_write_checks_do_not_fail_on_generic_log_error_lines() -> None:
    text = SCRIPT.read_text()

    assert "grep -qiE" not in text
    assert "error|denied|forbidden" not in text


def test_tuple_id_capture_filters_log_noise_to_uuid_only() -> None:
    text = SCRIPT.read_text()

    tuple_block = text[text.index("TUPLE_ID=$(") : text.index('print_test "Delete permission')]
    assert "grep -Eo" in tuple_block
    assert "[0-9a-fA-F]{8}" in tuple_block
    assert "tail -1" in tuple_block

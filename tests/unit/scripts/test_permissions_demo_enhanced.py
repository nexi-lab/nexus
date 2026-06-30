from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "permissions_demo_enhanced.sh"


def test_successful_write_checks_use_exit_status_only() -> None:
    text = SCRIPT.read_text()

    assert 'if [ "$WRITE_RC" -ne 0 ]; then' in text
    assert 'if [ "$DEEP_RC" -ne 0 ]; then' in text
    assert 'echo "$WRITE_OUT" | grep -qiE "error|denied|forbidden"' not in text
    assert 'echo "$DEEP_OUT" | grep -qiE "error|denied|forbidden"' not in text


def test_expected_denials_require_status_and_denial_evidence() -> None:
    text = SCRIPT.read_text()

    assert (
        '[ "$BOB_PERM_RC" -ne 0 ] && echo "$BOB_PERM_OUT" | is_permission_denial_output'
    ) in text
    assert (
        '[ "$CHARLIE_DEEP_RC" -ne 0 ] && echo "$CHARLIE_DEEP_OUT" | is_permission_denial_output'
    ) in text
    assert (
        '[ "$WRITE_ATTEMPT_RC" -ne 0 ] && echo "$WRITE_ATTEMPT" | is_permission_denial_output'
    ) in text
    assert ('[ "$CROSS_RC" -ne 0 ] && echo "$CROSS_OUT" | is_permission_denial_output') in text


def test_expected_denial_failures_without_denial_evidence_are_errors() -> None:
    text = SCRIPT.read_text()

    assert "rebac create failed without permission-denial evidence" in text
    assert "deep write failed without permission-denial evidence" in text
    assert "shared write failed without permission-denial evidence" in text
    assert "cross-tenant read failed without permission-denial evidence" in text


def test_tuple_id_capture_filters_log_noise_to_uuid_only() -> None:
    text = SCRIPT.read_text()

    tuple_block = text[text.index("TUPLE_ID=$(") : text.index('print_test "Delete permission')]
    assert "grep -Eo" in tuple_block
    assert "[0-9a-fA-F]{8}" in tuple_block
    assert "tail -1" in tuple_block


def test_owner_role_requires_read_write_and_execute() -> None:
    text = SCRIPT.read_text()
    owner_block = text[
        text.index('print_subsection "1.1 Understanding Permission Roles"') : text.index(
            'print_test "Verify bob (editor)'
        )
    ]

    assert "OWNER:  read ✓  write ✓  execute ✓" in owner_block
    assert "owners need editor/viewer role for read" not in owner_block
    assert "ALICE_READ=$(nexus rebac check user alice read file" in owner_block
    assert (
        'if echo "$ALICE_READ" | grep -q "GRANTED" '
        '&& echo "$ALICE_WRITE" | grep -q "GRANTED" '
        '&& echo "$ALICE_EXEC" | grep -q "GRANTED"; then'
    ) in owner_block


def test_group_explanation_targets_and_requires_the_granted_parent() -> None:
    text = SCRIPT.read_text()
    explain_block = text[
        text.index('log_step "rebac check user bob write file $DEMO_BASE') : text.index(
            'print_subsection "2.4 PROVE group composition'
        )
    ]
    assert (
        "EXPLAIN_OUT=$(nexus rebac explain user bob write file $DEMO_BASE 2>&1) "
        "&& EXPLAIN_RC=0 || EXPLAIN_RC=$?"
    ) in explain_block
    assert ('[ "$EXPLAIN_RC" -eq 0 ] && echo "$EXPLAIN_OUT" | grep -q "GRANTED"') in explain_block
    assert 'print_error "Group explanation failed' in explain_block
    assert "$DEMO_BASE/team-file.txt" not in explain_block

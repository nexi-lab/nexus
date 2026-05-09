"""Verify op completion emits a kind=OP ActivityEvent with correct meta."""


def test_emit_op_completed_emits_op_event(monkeypatch):
    captured = []
    from nexus.contracts.protocols.activity import set_emitter

    class Capture:
        def emit(self, **kw):
            captured.append(kw)

    set_emitter(Capture())
    try:
        # emit_op_completed lives in nexus_fs_content (alongside the post-hooks)
        from nexus.core.nexus_fs_content import emit_op_completed

        emit_op_completed(
            agent_id="alice",
            op="read",
            path="/s3/bucket/foo.txt",
            bytes_count=1234,
            latency_ms=42,
        )
    finally:
        from nexus.contracts.protocols.activity import NoopEmitter

        set_emitter(NoopEmitter())

    assert len(captured) == 1
    c = captured[0]
    from nexus.contracts.protocols.activity import EventKind, Result

    assert c["kind"] == EventKind.OP
    assert c["result"] == Result.OK
    assert c["actor_agent"] == "alice"
    assert c["latency_ms"] == 42
    assert c["meta"] == {"op": "read", "path": "/s3/bucket/foo.txt", "bytes": 1234}


def test_emit_op_completed_with_no_agent_still_emits():
    captured = []
    from nexus.contracts.protocols.activity import set_emitter

    class Capture:
        def emit(self, **kw):
            captured.append(kw)

    set_emitter(Capture())
    try:
        from nexus.core.nexus_fs_content import emit_op_completed

        emit_op_completed(
            agent_id=None,
            op="write",
            path="/local/x.txt",
            bytes_count=10,
            latency_ms=1,
        )
    finally:
        from nexus.contracts.protocols.activity import NoopEmitter

        set_emitter(NoopEmitter())

    assert len(captured) == 1
    assert captured[0]["actor_agent"] is None


def test_emit_op_completed_op_list():
    captured = []
    from nexus.contracts.protocols.activity import NoopEmitter, set_emitter

    class Capture:
        def emit(self, **kw):
            captured.append(kw)

    set_emitter(Capture())
    try:
        from nexus.core.nexus_fs_content import emit_op_completed

        emit_op_completed(agent_id="alice", op="list", path="/local/", bytes_count=0, latency_ms=2)
    finally:
        set_emitter(NoopEmitter())

    assert len(captured) == 1
    assert captured[0]["meta"] == {"op": "list", "path": "/local/", "bytes": 0}


def test_emit_op_completed_op_delete():
    captured = []
    from nexus.contracts.protocols.activity import NoopEmitter, set_emitter

    class Capture:
        def emit(self, **kw):
            captured.append(kw)

    set_emitter(Capture())
    try:
        from nexus.core.nexus_fs_content import emit_op_completed

        emit_op_completed(
            agent_id="alice", op="delete", path="/local/x.txt", bytes_count=0, latency_ms=3
        )
    finally:
        set_emitter(NoopEmitter())

    assert len(captured) == 1
    assert captured[0]["meta"] == {"op": "delete", "path": "/local/x.txt", "bytes": 0}

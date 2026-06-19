from __future__ import annotations

from nexus.server import protocol
from nexus.server.protocol import parse_method_params


def test_revoke_share_params_accept_user_alias_and_json_resource() -> None:
    params = parse_method_params(
        "revoke_share",
        {
            "resource": ["file", "/workspace/report.csv"],
            "target_user": "bob",
            "permission": "viewer",
            "zone_id": "zone-a",
        },
    )

    assert params.resource == ("file", "/workspace/report.csv")
    assert params.target is None
    assert params.target_user == "bob"
    assert params.permission == "viewer"
    assert params.zone_id == "zone-a"
    assert params.__class__ is protocol.RevokeShareParams


def test_revoke_share_by_id_params_accept_share_id_alias() -> None:
    params = parse_method_params("revoke_share_by_id", {"share_id": "tuple-1"})

    assert params.tuple_id is None
    assert params.share_id == "tuple-1"
    assert params.__class__ is protocol.RevokeShareByIdParams

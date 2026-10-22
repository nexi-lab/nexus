from contracts.tools.export_zone_outbox_fixtures import DESTINATION, fixtures


def test_rust_outbox_sql_fixtures_match_the_canonical_storage_models() -> None:
    for name, content in fixtures().items():
        assert (DESTINATION / name).read_text(encoding="utf8") == content, name

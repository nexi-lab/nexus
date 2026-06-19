import sqlalchemy as sa


def test_document_chunks_has_heading_prefix_column(sqlite_engine_after_upgrade):
    insp = sa.inspect(sqlite_engine_after_upgrade)
    cols = {c["name"] for c in insp.get_columns("document_chunks")}
    assert "heading_prefix" in cols

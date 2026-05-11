from nexus.cache.policy import index_ttl_for_backend, negative_ttl_for_backend


def test_index_ttl_default_when_no_override():
    assert index_ttl_for_backend("path_s3") == 600
    assert index_ttl_for_backend("unknown") == 60


def test_index_ttl_override_takes_precedence():
    overrides = {"path_s3": 30, "github_connector": 1200}
    assert index_ttl_for_backend("path_s3", overrides) == 30
    assert index_ttl_for_backend("github_connector", overrides) == 1200


def test_index_ttl_empty_override_dict_falls_through():
    assert index_ttl_for_backend("path_s3", {}) == 600


def test_index_ttl_none_override_falls_through():
    assert index_ttl_for_backend("path_s3", None) == 600


def test_negative_ttl_respects_override():
    overrides = {"path_s3": 30}
    assert negative_ttl_for_backend("path_s3", overrides) == 5


def test_negative_ttl_capped_below_positive():
    overrides = {"local": 2}
    assert negative_ttl_for_backend("local", overrides) == 2


def test_negative_ttl_unknown_backend():
    assert negative_ttl_for_backend("unknown") == 5

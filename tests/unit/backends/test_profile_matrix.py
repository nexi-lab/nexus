from __future__ import annotations

import pytest
from testkit.profiles import ProfileCase, all_profile_params


@pytest.mark.parametrize("case", all_profile_params())
def test_backend_profile_matrix_tracks_profile_defaults(case: ProfileCase) -> None:
    assert case.expected_bricks == case.profile.default_bricks()
    assert case.expected_drivers == case.profile.default_drivers()

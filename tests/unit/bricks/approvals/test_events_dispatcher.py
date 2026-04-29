"""In-process future dispatcher tests."""

import asyncio

import pytest

from nexus.bricks.approvals.events import Dispatcher
from nexus.bricks.approvals.models import Decision


@pytest.mark.asyncio
async def test_resolve_wakes_all_waiters_for_request_id():
    d = Dispatcher()
    f1 = d.register("req_a")
    f2 = d.register("req_a")
    d.resolve("req_a", Decision.APPROVED)
    assert (await asyncio.wait_for(f1, 0.5)) is Decision.APPROVED
    assert (await asyncio.wait_for(f2, 0.5)) is Decision.APPROVED


@pytest.mark.asyncio
async def test_resolve_for_unknown_id_is_noop():
    d = Dispatcher()
    d.resolve("nope", Decision.DENIED)  # should not raise


@pytest.mark.asyncio
async def test_cancel_unregisters_one_future():
    d = Dispatcher()
    f1 = d.register("req_b")
    f2 = d.register("req_b")
    d.cancel(f1)
    d.resolve("req_b", Decision.DENIED)
    # f1 was cancelled out of the registry but the asyncio.Future itself
    # is not auto-cancelled — caller is responsible for f1.cancel().
    assert (await asyncio.wait_for(f2, 0.5)) is Decision.DENIED


@pytest.mark.asyncio
async def test_in_flight_request_ids_returns_known_keys():
    d = Dispatcher()
    d.register("req_a")
    d.register("req_b")
    assert set(d.in_flight_request_ids()) == {"req_a", "req_b"}


@pytest.mark.asyncio
async def test_waiter_count_returns_zero_for_unknown_id():
    d = Dispatcher()
    assert d.waiter_count("nope") == 0


@pytest.mark.asyncio
async def test_waiter_count_returns_n_after_n_register_calls():
    d = Dispatcher()
    for _ in range(3):
        d.register("req_c")
    assert d.waiter_count("req_c") == 3
    # Other ids remain at zero.
    assert d.waiter_count("req_d") == 0
    # Resolve drops the parked futures back to zero.
    d.resolve("req_c", Decision.APPROVED)
    assert d.waiter_count("req_c") == 0

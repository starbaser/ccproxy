"""Tests for ccproxy.transport.dispatch.

Pins the public API behavior of the LRU+idle cache, singleton lifecycle,
eviction semantics, and profile validation documented in dispatch.py.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
import pytest

from ccproxy.transport import (
    IDLE_TIMEOUT_SECONDS,
    MAX_SESSIONS,
    VALID_PROFILES,
    UnknownFingerprintProfileError,
    aclose_all,
    get_client,
    reset_cache,
)
from ccproxy.transport.dispatch import _Cache

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_cache():
    """Reset the singleton and close all clients around every test."""
    reset_cache()
    yield
    reset_cache()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_max_sessions(self) -> None:
        assert MAX_SESSIONS == 16

    def test_idle_timeout_seconds(self) -> None:
        assert IDLE_TIMEOUT_SECONDS == 60.0

    def test_valid_profiles_is_frozenset(self) -> None:
        assert isinstance(VALID_PROFILES, frozenset)

    def test_valid_profiles_nonempty(self) -> None:
        assert len(VALID_PROFILES) == 53

    def test_known_chrome_profile(self) -> None:
        assert "chrome131" in VALID_PROFILES

    def test_known_firefox_profile(self) -> None:
        assert "firefox133" in VALID_PROFILES

    def test_known_safari_profile(self) -> None:
        assert "safari260" in VALID_PROFILES


# ---------------------------------------------------------------------------
# UnknownFingerprintProfileError
# ---------------------------------------------------------------------------


class TestUnknownFingerprintProfileError:
    def test_is_value_error_subclass(self) -> None:
        assert issubclass(UnknownFingerprintProfileError, ValueError)

    async def test_bad_profile_raises_via_public_api(self) -> None:
        with pytest.raises(UnknownFingerprintProfileError, match="not-a-real-profile"):
            await get_client(host="example.com", profile="not-a-real-profile")

    async def test_error_message_contains_bad_name(self) -> None:
        bad = "totally_bogus_browser42"
        with pytest.raises(UnknownFingerprintProfileError, match=bad):
            await get_client(host="example.com", profile=bad)

    async def test_error_message_references_valid_profiles(self) -> None:
        with pytest.raises(UnknownFingerprintProfileError, match="chrome131"):
            await get_client(host="example.com", profile="notvalid")

    async def test_bad_profile_raises_via_cache_directly(self) -> None:
        cache = _Cache(max_sessions=4, idle_timeout=60.0)
        with pytest.raises(UnknownFingerprintProfileError, match="bogus"):
            await cache.get(host="example.com", profile="bogus")


# ---------------------------------------------------------------------------
# Identity on identical key
# ---------------------------------------------------------------------------


class TestCacheIdentity:
    async def test_same_key_returns_same_client(self) -> None:
        a = await get_client(host="example.com", profile="chrome131")
        b = await get_client(host="example.com", profile="chrome131")
        assert a is b

    async def test_different_host_returns_distinct_client(self) -> None:
        a = await get_client(host="alpha.example.com", profile="chrome131")
        b = await get_client(host="beta.example.com", profile="chrome131")
        assert a is not b

    async def test_different_profile_returns_distinct_client(self) -> None:
        a = await get_client(host="example.com", profile="chrome131")
        b = await get_client(host="example.com", profile="firefox133")
        assert a is not b

    async def test_returned_object_is_httpx_async_client(self) -> None:
        client = await get_client(host="example.com", profile="chrome131")
        assert isinstance(client, httpx.AsyncClient)

    async def test_client_is_open_on_return(self) -> None:
        client = await get_client(host="example.com", profile="chrome131")
        assert not client.is_closed


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    async def test_singleton_identity_across_calls(self) -> None:
        a = await get_client(host="example.com", profile="chrome131")
        b = await get_client(host="example.com", profile="chrome131")
        assert a is b

    async def test_reset_cache_breaks_singleton(self) -> None:
        before = await get_client(host="example.com", profile="chrome131")
        reset_cache()
        after = await get_client(host="example.com", profile="chrome131")
        assert before is not after

    async def test_reset_cache_does_not_close_existing_client(self) -> None:
        client = await get_client(host="example.com", profile="chrome131")
        reset_cache()
        assert not client.is_closed

    async def test_reset_yields_fresh_client_open(self) -> None:
        reset_cache()
        client = await get_client(host="example.com", profile="chrome131")
        assert not client.is_closed


# ---------------------------------------------------------------------------
# LRU eviction
# ---------------------------------------------------------------------------


class TestLruEviction:
    async def test_lru_evicts_oldest_entry(self) -> None:
        cache = _Cache(max_sessions=2, idle_timeout=60.0)
        first = await cache.get(host="first.com", profile="chrome131")
        await cache.get(host="second.com", profile="chrome131")
        assert cache.size() == 2

        await cache.get(host="third.com", profile="chrome131")

        assert cache.size() == 2
        assert first.is_closed

    async def test_lru_eviction_does_not_close_newer_entries(self) -> None:
        cache = _Cache(max_sessions=2, idle_timeout=60.0)
        await cache.get(host="first.com", profile="chrome131")
        second = await cache.get(host="second.com", profile="chrome131")
        third = await cache.get(host="third.com", profile="chrome131")

        assert not second.is_closed
        assert not third.is_closed

    async def test_lru_evicts_correct_count(self) -> None:
        cache = _Cache(max_sessions=2, idle_timeout=60.0)
        for i in range(4):
            await cache.get(host=f"host{i}.com", profile="chrome131")

        assert cache.size() == 2

    async def test_touch_on_get_promotes_entry(self) -> None:
        cache = _Cache(max_sessions=2, idle_timeout=60.0)
        first = await cache.get(host="first.com", profile="chrome131")
        second = await cache.get(host="second.com", profile="chrome131")

        # Touch first — it moves to most-recently-used
        first_again = await cache.get(host="first.com", profile="chrome131")
        assert first is first_again

        # Adding a third entry should evict second (now LRU), not first
        await cache.get(host="third.com", profile="chrome131")

        assert not first.is_closed
        assert second.is_closed

    async def test_touch_preserves_client_identity(self) -> None:
        cache = _Cache(max_sessions=4, idle_timeout=60.0)
        a = await cache.get(host="a.com", profile="chrome131")
        b = await cache.get(host="a.com", profile="chrome131")
        assert a is b


# ---------------------------------------------------------------------------
# Idle eviction
# ---------------------------------------------------------------------------


class TestIdleEviction:
    async def test_idle_entry_closed_on_next_access(self) -> None:
        # idle_timeout=0.0: strictly > 0.0, so anything with elapsed > 0 is stale
        cache = _Cache(max_sessions=16, idle_timeout=0.0)
        stale = await cache.get(host="stale.com", profile="chrome131")

        # A non-zero sleep ensures monotonic time has advanced past 0.0
        await asyncio.sleep(0.01)

        # Any subsequent get triggers idle eviction sweep
        fresh = await cache.get(host="fresh.com", profile="chrome131")

        assert stale.is_closed
        assert not fresh.is_closed

    async def test_idle_eviction_removes_entry_from_cache(self) -> None:
        cache = _Cache(max_sessions=16, idle_timeout=0.0)
        await cache.get(host="stale.com", profile="chrome131")

        await asyncio.sleep(0.01)
        await cache.get(host="fresh.com", profile="chrome131")

        assert cache.size() == 1

    async def test_no_idle_eviction_within_timeout(self) -> None:
        cache = _Cache(max_sessions=16, idle_timeout=60.0)
        a = await cache.get(host="a.com", profile="chrome131")
        b = await cache.get(host="b.com", profile="chrome131")

        assert cache.size() == 2
        assert not a.is_closed
        assert not b.is_closed


# ---------------------------------------------------------------------------
# aclose_all
# ---------------------------------------------------------------------------


class TestAcloseAll:
    async def test_aclose_all_closes_every_client(self) -> None:
        cache = _Cache(max_sessions=16, idle_timeout=60.0)
        clients = [await cache.get(host=f"host{i}.com", profile="chrome131") for i in range(3)]
        await cache.aclose_all()

        assert all(c.is_closed for c in clients)

    async def test_aclose_all_empties_cache(self) -> None:
        cache = _Cache(max_sessions=16, idle_timeout=60.0)
        for i in range(3):
            await cache.get(host=f"host{i}.com", profile="chrome131")
        await cache.aclose_all()

        assert cache.size() == 0

    async def test_aclose_all_is_idempotent(self) -> None:
        cache = _Cache(max_sessions=16, idle_timeout=60.0)
        await cache.get(host="a.com", profile="chrome131")
        await cache.aclose_all()
        await cache.aclose_all()  # must not raise

    async def test_aclose_all_via_public_api(self) -> None:
        clients = [await get_client(host=f"host{i}.com", profile="chrome131") for i in range(3)]
        await aclose_all()

        assert all(c.is_closed for c in clients)

    async def test_aclose_all_empty_cache_is_idempotent(self) -> None:
        await aclose_all()  # nothing cached yet — must not raise
        await aclose_all()


# ---------------------------------------------------------------------------
# Cache size seam
# ---------------------------------------------------------------------------


class TestCacheSize:
    async def test_size_zero_initially(self) -> None:
        cache = _Cache(max_sessions=4, idle_timeout=60.0)
        assert cache.size() == 0

    async def test_size_increments_on_new_entry(self) -> None:
        cache = _Cache(max_sessions=4, idle_timeout=60.0)
        await cache.get(host="a.com", profile="chrome131")
        assert cache.size() == 1
        await cache.get(host="b.com", profile="chrome131")
        assert cache.size() == 2

    async def test_size_stable_on_repeat_get(self) -> None:
        cache = _Cache(max_sessions=4, idle_timeout=60.0)
        await cache.get(host="a.com", profile="chrome131")
        await cache.get(host="a.com", profile="chrome131")
        assert cache.size() == 1


# ---------------------------------------------------------------------------
# Parametrized: distinct-key pairs produce distinct clients
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DistinctKeyTestCase:
    name: str
    """Descriptive name for the test scenario."""

    host_a: str
    """Host for the first get_client call."""

    profile_a: str
    """Profile for the first get_client call."""

    host_b: str
    """Host for the second get_client call."""

    profile_b: str
    """Profile for the second get_client call."""


DISTINCT_KEY_CASES: list[DistinctKeyTestCase] = [
    DistinctKeyTestCase(
        name="different_host_same_profile",
        host_a="alpha.com",
        profile_a="chrome131",
        host_b="beta.com",
        profile_b="chrome131",
    ),
    DistinctKeyTestCase(
        name="same_host_different_profile",
        host_a="example.com",
        profile_a="chrome131",
        host_b="example.com",
        profile_b="firefox133",
    ),
    DistinctKeyTestCase(
        name="different_host_different_profile",
        host_a="one.com",
        profile_a="chrome131",
        host_b="two.com",
        profile_b="safari260",
    ),
]


@pytest.mark.parametrize(
    "case",
    [pytest.param(c, id=c.name) for c in DISTINCT_KEY_CASES],
)
async def test_distinct_key_yields_distinct_client(case: DistinctKeyTestCase) -> None:
    cache = _Cache(max_sessions=16, idle_timeout=60.0)
    a = await cache.get(host=case.host_a, profile=case.profile_a)
    b = await cache.get(host=case.host_b, profile=case.profile_b)
    assert a is not b

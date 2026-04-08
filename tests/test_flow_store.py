from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

import ccproxy.inspector.flow_store as fs
from ccproxy.inspector.flow_store import (
    FLOW_ID_HEADER,
    AuthMeta,
    FlowRecord,
    InspectorMeta,
    OtelMeta,
    _STORE_TTL,
    clear_flow_store,
    create_flow_record,
    get_flow_record,
)


class TestFlowRecordDataclass:
    def test_default_values(self):
        record = FlowRecord("inbound")
        assert record.auth is None
        assert record.otel is None
        assert record.original_headers == {}

    def test_original_headers_independent(self):
        r1 = FlowRecord("inbound")
        r2 = FlowRecord("outbound")
        r1.original_headers["key"] = "value"
        assert "key" not in r2.original_headers

    def test_auth_meta_defaults(self):
        auth = AuthMeta(provider="anthropic", credential="tok", key_field="Authorization")
        assert auth.injected is False
        assert auth.original_key == ""

    def test_otel_meta_defaults(self):
        otel = OtelMeta()
        assert otel.span is None
        assert otel.ended is False


class TestInspectorMeta:
    def test_record_key_value(self):
        assert InspectorMeta.RECORD == "ccproxy.record"

    def test_direction_key_value(self):
        assert InspectorMeta.DIRECTION == "ccproxy.direction"

    def test_flow_id_header_constant(self):
        assert FLOW_ID_HEADER == "x-ccproxy-flow-id"


class TestCreateFlowRecord:
    def test_returns_uuid_and_record(self):
        flow_id, record = create_flow_record("inbound")
        uuid.UUID(flow_id)
        assert isinstance(record, FlowRecord)

    def test_unique_ids(self):
        id1, _ = create_flow_record("inbound")
        id2, _ = create_flow_record("inbound")
        assert id1 != id2

    def test_inbound_direction(self):
        _, record = create_flow_record("inbound")
        assert record.direction == "inbound"

    def test_outbound_direction(self):
        _, record = create_flow_record("outbound")
        assert record.direction == "outbound"


class TestGetFlowRecord:
    def test_found(self):
        flow_id, record = create_flow_record("inbound")
        retrieved = get_flow_record(flow_id)
        assert retrieved is record

    def test_not_found(self):
        assert get_flow_record("nonexistent-id") is None

    def test_empty_string_key(self):
        assert get_flow_record("") is None

    def test_expired_record(self, monkeypatch: pytest.MonkeyPatch):
        import time as stdlib_time

        base = stdlib_time.time()

        call_count = 0

        def fake_time():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return base
            return base + _STORE_TTL + 1.0

        monkeypatch.setattr(fs.time, "time", fake_time)
        flow_id, _ = create_flow_record("inbound")
        assert get_flow_record(flow_id) is None

    def test_boundary_exactly_at_ttl(self, monkeypatch: pytest.MonkeyPatch):
        import time as stdlib_time

        base = stdlib_time.time()

        call_count = 0

        def fake_time():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return base
            return base + _STORE_TTL

        monkeypatch.setattr(fs.time, "time", fake_time)
        flow_id, record = create_flow_record("inbound")
        retrieved = get_flow_record(flow_id)
        assert retrieved is record

    def test_boundary_just_past_ttl(self, monkeypatch: pytest.MonkeyPatch):
        import time as stdlib_time

        base = stdlib_time.time()

        call_count = 0

        def fake_time():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return base
            return base + _STORE_TTL + 0.001

        monkeypatch.setattr(fs.time, "time", fake_time)
        flow_id, _ = create_flow_record("inbound")
        assert get_flow_record(flow_id) is None

    def test_expired_record_deleted(self, monkeypatch: pytest.MonkeyPatch):
        import time as stdlib_time

        base = stdlib_time.time()

        call_count = 0

        def fake_time():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return base
            return base + _STORE_TTL + 1.0

        monkeypatch.setattr(fs.time, "time", fake_time)
        flow_id, _ = create_flow_record("inbound")
        get_flow_record(flow_id)
        assert flow_id not in fs._flow_store


class TestCleanupExpired:
    def test_cleanup_removes_only_expired(self, monkeypatch: pytest.MonkeyPatch):
        import time as stdlib_time

        t = stdlib_time.time()
        timestamps: list[float] = []

        def fake_time():
            return timestamps[-1] if timestamps else t

        monkeypatch.setattr(fs.time, "time", fake_time)

        timestamps.append(t)
        id1, _ = create_flow_record("inbound")
        timestamps.append(t)
        id2, _ = create_flow_record("inbound")
        timestamps.append(t)
        id3, _ = create_flow_record("inbound")

        # Advance time past TTL for id1 and id2 (stored at t),
        # then create id4 at future time (triggers cleanup).
        future = t + _STORE_TTL + 1.0
        timestamps.append(future)
        id4, record4 = create_flow_record("inbound")

        assert id1 not in fs._flow_store
        assert id2 not in fs._flow_store
        assert id3 not in fs._flow_store
        assert id4 in fs._flow_store

    def test_cleanup_on_empty_store(self):
        clear_flow_store()
        id_, _ = create_flow_record("inbound")
        assert get_flow_record(id_) is not None


class TestClearFlowStore:
    def test_clears_all(self):
        ids = [create_flow_record("inbound")[0] for _ in range(5)]
        clear_flow_store()
        for fid in ids:
            assert get_flow_record(fid) is None

    def test_clear_empty(self):
        clear_flow_store()
        clear_flow_store()


class TestConcurrency:
    def test_concurrent_create(self):
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(create_flow_record, "inbound") for _ in range(10)]
            results = [f.result() for f in futures]
        ids = [flow_id for flow_id, _ in results]
        assert len(set(ids)) == 10
        for fid in ids:
            uuid.UUID(fid)

    def test_concurrent_get_during_clear(self):
        ids = [create_flow_record("inbound")[0] for _ in range(20)]

        def get_all():
            for fid in ids:
                get_flow_record(fid)

        with ThreadPoolExecutor(max_workers=4) as pool:
            f1 = pool.submit(get_all)
            f2 = pool.submit(clear_flow_store)
            f3 = pool.submit(get_all)
            f1.result()
            f2.result()
            f3.result()

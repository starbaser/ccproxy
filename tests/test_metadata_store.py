"""Tests for metadata_store TTL store."""

from __future__ import annotations

import time
from unittest.mock import patch

from ccproxy.metadata_store import get_request_metadata, store_request_metadata


class TestMetadataStore:
    def test_store_and_retrieve(self):
        store_request_metadata("call-1", {"key": "value"})
        result = get_request_metadata("call-1")
        assert result == {"key": "value"}

    def test_missing_key_returns_empty_dict(self):
        result = get_request_metadata("nonexistent")
        assert result == {}

    def test_overwrite_same_call_id(self):
        store_request_metadata("call-2", {"a": 1})
        store_request_metadata("call-2", {"b": 2})
        result = get_request_metadata("call-2")
        assert result == {"b": 2}

    def test_expired_entries_cleaned_up(self):
        store_request_metadata("old-call", {"data": "old"})
        # Mock time to be > TTL seconds in the future
        future_time = time.time() + 120
        with patch("ccproxy.metadata_store.time") as mock_time:
            mock_time.time.return_value = future_time
            # Store a new entry to trigger cleanup
            store_request_metadata("new-call", {"data": "new"})

        # old-call should be gone (expired)
        result = get_request_metadata("old-call")
        assert result == {}

    def test_multiple_entries_independent(self):
        store_request_metadata("c1", {"x": 1})
        store_request_metadata("c2", {"y": 2})
        assert get_request_metadata("c1") == {"x": 1}
        assert get_request_metadata("c2") == {"y": 2}

    def test_empty_metadata(self):
        store_request_metadata("empty-call", {})
        result = get_request_metadata("empty-call")
        assert result == {}

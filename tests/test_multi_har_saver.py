"""Tests for ccproxy.inspector.multi_har_saver.MultiHARSaver."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from mitmproxy import http
from mitmproxy.test import tflow

from ccproxy.inspector.flow_store import ClientRequest, FlowRecord, InspectorMeta
from ccproxy.inspector.multi_har_saver import MultiHARSaver


def _make_flow_with_snapshot(
    *,
    method: str = "POST",
    forwarded_url: str = "https://api.upstream.example/v1/messages",
    client_body: bytes = b'{"model": "claude-opus"}',
    content_type: str = "application/json",
) -> http.HTTPFlow:
    """Build an HTTPFlow with a response and a ClientRequest snapshot attached."""
    flow = tflow.tflow(resp=True)
    flow.request.method = method
    flow.request.url = forwarded_url
    flow.request.content = b'{"model": "claude-haiku"}'  # mutated (forwarded) body

    record = FlowRecord(direction="inbound")
    record.client_request = ClientRequest(
        method=method,
        scheme="https",
        host="api.anthropic.com",
        port=443,
        path="/v1/messages",
        headers={"content-type": content_type, "user-agent": "claude-code/1.0"},
        body=client_body,
        content_type=content_type,
    )
    flow.metadata[InspectorMeta.RECORD] = record
    return flow


def _run_dump(flow: http.HTTPFlow | None, flow_id: str) -> str:
    """Invoke MultiHARSaver.ccproxy_dump with a patched view returning `flow`."""
    saver = MultiHARSaver()
    view = MagicMock()
    view.get_by_id.return_value = flow
    master = MagicMock()
    master.addons.get.return_value = view
    with patch("ccproxy.inspector.multi_har_saver.ctx") as mock_ctx:
        mock_ctx.master = master
        return saver.ccproxy_dump(flow_id)


def _run_dump_multi(flows_by_id: dict[str, http.HTTPFlow | None], flow_ids_csv: str) -> str:
    """Invoke ccproxy_dump with multiple flows identified by comma-separated ids."""
    saver = MultiHARSaver()
    view = MagicMock()
    view.get_by_id.side_effect = lambda fid: flows_by_id.get(fid)
    master = MagicMock()
    master.addons.get.return_value = view
    with patch("ccproxy.inspector.multi_har_saver.ctx") as mock_ctx:
        mock_ctx.master = master
        return saver.ccproxy_dump(flow_ids_csv)


class TestFlowLookup:
    """ccproxy.dump looks up the flow via view.get_by_id."""

    def test_flow_not_found_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="no flow with id missing-id"):
            _run_dump(None, "missing-id")

    def test_non_http_flow_raises_value_error(self) -> None:
        not_a_flow = MagicMock(spec=[])
        with pytest.raises(ValueError, match="no flow with id weird-id"):
            _run_dump(not_a_flow, "weird-id")


class TestReturnType:
    """Mitmproxy command return-type registry requires str — not dict."""

    def test_returns_json_string_not_dict(self) -> None:
        flow = _make_flow_with_snapshot()
        result = _run_dump(flow, flow.id)
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert isinstance(parsed, dict)


class TestHarShape:
    """Top-level HAR structure: one page, two entries, ccproxy creator."""

    def test_log_version_12(self) -> None:
        flow = _make_flow_with_snapshot()
        har = json.loads(_run_dump(flow, flow.id))
        assert har["log"]["version"] == "1.2"

    def test_creator_rebranded_to_ccproxy(self) -> None:
        flow = _make_flow_with_snapshot()
        har = json.loads(_run_dump(flow, flow.id))
        assert har["log"]["creator"]["name"] == "ccproxy"

    def test_single_page(self) -> None:
        flow = _make_flow_with_snapshot()
        har = json.loads(_run_dump(flow, flow.id))
        assert len(har["log"]["pages"]) == 1

    def test_two_entries(self) -> None:
        flow = _make_flow_with_snapshot()
        har = json.loads(_run_dump(flow, flow.id))
        assert len(har["log"]["entries"]) == 2


class TestPageGrouping:
    """Page id is the flow id; both entries reference it via pageref."""

    def test_page_id_is_flow_id(self) -> None:
        flow = _make_flow_with_snapshot()
        har = json.loads(_run_dump(flow, flow.id))
        assert har["log"]["pages"][0]["id"] == flow.id

    def test_page_title_contains_flow_id(self) -> None:
        flow = _make_flow_with_snapshot()
        har = json.loads(_run_dump(flow, flow.id))
        assert flow.id in har["log"]["pages"][0]["title"]

    def test_entries_share_pageref(self) -> None:
        flow = _make_flow_with_snapshot()
        har = json.loads(_run_dump(flow, flow.id))
        entries = har["log"]["entries"]
        assert entries[0]["pageref"] == flow.id
        assert entries[1]["pageref"] == flow.id


class TestEntryZero:
    """entries[0] = [fwdreq, fwdres] — the real flow, authoritative."""

    def test_entry_0_request_is_forwarded_url(self) -> None:
        flow = _make_flow_with_snapshot(
            forwarded_url="https://api.upstream.example/v1/messages",
        )
        har = json.loads(_run_dump(flow, flow.id))
        assert "upstream.example" in har["log"]["entries"][0]["request"]["url"]

    def test_entry_0_response_has_real_status(self) -> None:
        flow = _make_flow_with_snapshot()
        assert flow.response is not None
        expected_status = flow.response.status_code
        har = json.loads(_run_dump(flow, flow.id))
        assert har["log"]["entries"][0]["response"]["status"] == expected_status


class TestEntryOne:
    """entries[1] = [clireq, fwdres] — clone with request rebuilt from snapshot."""

    def test_entry_1_request_url_from_snapshot(self) -> None:
        flow = _make_flow_with_snapshot()
        har = json.loads(_run_dump(flow, flow.id))
        url = har["log"]["entries"][1]["request"]["url"]
        # ClientRequest snapshot sets scheme/host/port/path =
        # https/api.anthropic.com/443/v1/messages
        assert "anthropic.com" in url
        assert "/v1/messages" in url

    def test_entry_1_request_headers_from_snapshot(self) -> None:
        flow = _make_flow_with_snapshot()
        har = json.loads(_run_dump(flow, flow.id))
        header_pairs = {h["name"].lower(): h["value"] for h in har["log"]["entries"][1]["request"]["headers"]}
        assert header_pairs.get("user-agent") == "claude-code/1.0"
        assert header_pairs.get("content-type") == "application/json"

    def test_entry_1_post_data_for_post(self) -> None:
        flow = _make_flow_with_snapshot(
            method="POST",
            client_body=b'{"model": "claude-opus"}',
            content_type="application/json",
        )
        har = json.loads(_run_dump(flow, flow.id))
        post_data = har["log"]["entries"][1]["request"]["postData"]
        assert "claude-opus" in post_data["text"]
        assert post_data["mimeType"] == "application/json"

    def test_entry_1_response_is_same_real_response(self) -> None:
        """Duplicate of entries[0].response — HAR pair must be complete."""
        flow = _make_flow_with_snapshot()
        assert flow.response is not None
        har = json.loads(_run_dump(flow, flow.id))
        entries = har["log"]["entries"]
        assert entries[0]["response"]["status"] == entries[1]["response"]["status"]
        assert entries[0]["response"]["status"] == flow.response.status_code


class TestSnapshotMissingFallback:
    """If flow.metadata has no ClientRequest, entries[1] falls back to the mutated request."""

    def test_no_record_does_not_crash(self) -> None:
        flow = tflow.tflow(resp=True)  # no metadata.record
        har = json.loads(_run_dump(flow, flow.id))
        assert len(har["log"]["entries"]) == 2

    def test_no_record_entry_1_mirrors_entry_0_request(self) -> None:
        flow = tflow.tflow(resp=True)
        har = json.loads(_run_dump(flow, flow.id))
        entries = har["log"]["entries"]
        assert entries[0]["request"]["url"] == entries[1]["request"]["url"]

    def test_record_without_client_request_falls_back(self) -> None:
        flow = tflow.tflow(resp=True)
        record = FlowRecord(direction="inbound")
        record.client_request = None
        flow.metadata[InspectorMeta.RECORD] = record
        har = json.loads(_run_dump(flow, flow.id))
        assert len(har["log"]["entries"]) == 2


class TestMultiFlowDump:
    """ccproxy.dump with comma-separated flow ids → N-page HAR."""

    def test_two_flows_produces_two_pages_four_entries(self) -> None:
        f1 = _make_flow_with_snapshot(forwarded_url="https://api.one.example/v1")
        f2 = _make_flow_with_snapshot(forwarded_url="https://api.two.example/v1")
        har = json.loads(_run_dump_multi({f1.id: f1, f2.id: f2}, f"{f1.id},{f2.id}"))
        assert len(har["log"]["pages"]) == 2
        assert len(har["log"]["entries"]) == 4

    def test_three_flows_produces_three_pages_six_entries(self) -> None:
        flows = [_make_flow_with_snapshot() for _ in range(3)]
        by_id = {f.id: f for f in flows}
        csv = ",".join(f.id for f in flows)
        har = json.loads(_run_dump_multi(by_id, csv))
        assert len(har["log"]["pages"]) == 3
        assert len(har["log"]["entries"]) == 6

    def test_pageref_pairing_correct(self) -> None:
        f1 = _make_flow_with_snapshot()
        f2 = _make_flow_with_snapshot()
        har = json.loads(_run_dump_multi({f1.id: f1, f2.id: f2}, f"{f1.id},{f2.id}"))
        entries = har["log"]["entries"]
        assert entries[0]["pageref"] == f1.id
        assert entries[1]["pageref"] == f1.id
        assert entries[2]["pageref"] == f2.id
        assert entries[3]["pageref"] == f2.id

    def test_page_ids_match_flow_ids(self) -> None:
        f1 = _make_flow_with_snapshot()
        f2 = _make_flow_with_snapshot()
        har = json.loads(_run_dump_multi({f1.id: f1, f2.id: f2}, f"{f1.id},{f2.id}"))
        page_ids = [p["id"] for p in har["log"]["pages"]]
        assert page_ids == [f1.id, f2.id]

    def test_flow_order_preserved(self) -> None:
        f1 = _make_flow_with_snapshot(forwarded_url="https://first.example/v1")
        f2 = _make_flow_with_snapshot(forwarded_url="https://second.example/v1")
        har = json.loads(_run_dump_multi({f1.id: f1, f2.id: f2}, f"{f1.id},{f2.id}"))
        assert "first.example" in har["log"]["entries"][0]["request"]["url"]
        assert "second.example" in har["log"]["entries"][2]["request"]["url"]

    def test_whitespace_in_comma_separated_trimmed(self) -> None:
        f1 = _make_flow_with_snapshot()
        f2 = _make_flow_with_snapshot()
        har = json.loads(
            _run_dump_multi(
                {f1.id: f1, f2.id: f2},
                f" {f1.id} , {f2.id} ",
            )
        )
        assert len(har["log"]["pages"]) == 2

    def test_empty_string_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="no flow ids provided"):
            _run_dump_multi({}, "")

    def test_one_missing_id_in_list_raises_value_error(self) -> None:
        f1 = _make_flow_with_snapshot()
        with pytest.raises(ValueError, match="no flow with id missing"):
            _run_dump_multi({f1.id: f1}, f"{f1.id},missing")

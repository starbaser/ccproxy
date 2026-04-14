"""ccproxy multi-page HAR saver addon.

Registers `ccproxy.dump`: a mitmproxy command that returns a page-grouped
HAR 1.2 JSON string for a single flow id. Delegates all HAR entry
construction to `mitmproxy.addons.savehar.SaveHar.make_har()` — ccproxy
does not reimplement the HAR spec.

Layout (one page per flow, two complete entries by documented index):

    entries[0]  [fwdreq, fwdres]  real flow (authoritative)
    entries[1]  [clireq, fwdres]  clone with .request rebuilt from the
                                  `ClientRequest` snapshot, response duplicated
                                  so the HAR pair stays complete

Both entries share ``pageref == flow.id``; the page id is ``flow.id`` too.
Future work will aggregate multiple flows per conversation turn into one HAR
with multiple pages — this contract scales there unchanged.
"""

from __future__ import annotations

import json
import logging
from typing import cast

from mitmproxy import command, ctx, http
from mitmproxy.addons.savehar import SaveHar

from ccproxy.inspector.flow_store import InspectorMeta

logger = logging.getLogger(__name__)


class MultiHARSaver:
    """Addon exposing `ccproxy.dump` — single-page HAR export for a flow."""

    def __init__(self) -> None:
        self._savehar = SaveHar()  # standalone — we only use make_har()

    @command.command("ccproxy.dump")  # type: ignore[untyped-decorator]
    def ccproxy_dump(self, flow_id: str) -> str:
        """Return a JSON-serialized single-page HAR for the given flow.

        mitmproxy's command return-type registry does not include `dict` —
        only `str` — so we serialize here and let the CLI pass the JSON
        through unchanged.
        """
        flow = self._find_http_flow(flow_id)
        if flow is None:
            raise ValueError(f"no flow with id {flow_id}")

        # Clone the real flow (keeping its real response) and swap the clone's
        # .request for a synthetic http.Request rebuilt from the ClientRequest
        # snapshot. Both entries are complete, valid HAR pairs.
        client_clone = self._build_client_clone(flow)

        har = self._savehar.make_har([flow, client_clone])
        # entries[0] = [fwdreq, fwdres]  (real flow — authoritative)
        # entries[1] = [clireq, fwdres]  (clone — client-request perspective)

        # Stamp pageref: one page per flow (future: per conversation turn).
        page_id = flow.id
        for entry in har["log"]["entries"]:
            entry["pageref"] = page_id

        started_iso = har["log"]["entries"][0]["startedDateTime"]
        har["log"]["pages"] = [
            {
                "id": page_id,
                "title": f"ccproxy flow {page_id}",
                "startedDateTime": started_iso,
                "pageTimings": {"onContentLoad": -1, "onLoad": -1},
            },
        ]

        har["log"]["creator"] = {"name": "ccproxy", "version": "dev", "comment": ""}

        return json.dumps(har, indent=2)

    @staticmethod
    def _find_http_flow(flow_id: str) -> http.HTTPFlow | None:
        view = ctx.master.addons.get("view")  # type: ignore[no-untyped-call]
        if view is None:
            return None
        found = view.get_by_id(flow_id)
        return found if isinstance(found, http.HTTPFlow) else None

    @staticmethod
    def _build_client_clone(flow: http.HTTPFlow) -> http.HTTPFlow:
        """Clone the flow and rebuild .request from the ClientRequest snapshot.

        The clone keeps the real flow's response (duplicate of entries[0]'s
        response, required because a HAR entry must be a complete pair).

        Fallback: if the snapshot is missing, the clone keeps the mutated
        request — entries[1] renders identically to entries[0], but the HAR
        stays valid.
        """
        clone = cast("http.HTTPFlow", flow.copy())  # type: ignore[no-untyped-call]

        record = flow.metadata.get(InspectorMeta.RECORD)
        snapshot = record.client_request if record is not None else None
        if snapshot is None:
            logger.debug("Flow %s has no ClientRequest snapshot; falling back", flow.id)
            return clone

        url = f"{snapshot.scheme}://{snapshot.host}:{snapshot.port}{snapshot.path}"
        synthetic = http.Request.make(
            method=snapshot.method,
            url=url,
            content=snapshot.body,
            headers=snapshot.headers,
        )
        synthetic.timestamp_start = flow.request.timestamp_start
        synthetic.timestamp_end = flow.request.timestamp_end
        clone.request = synthetic
        return clone

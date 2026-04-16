"""ccproxy multi-page HAR saver addon.

Registers ``ccproxy.dump``: a mitmproxy command that returns a page-grouped
HAR 1.2 JSON string for one or more flow ids (comma-separated). Delegates
all HAR entry construction to ``mitmproxy.addons.savehar.SaveHar.make_har()``
— ccproxy does not reimplement the HAR spec.

Layout (one page per flow, two complete entries per page by index):

    entries[2i]    [fwdreq, provider_response]  what was sent to / received from provider
    entries[2i+1]  [clireq, client_response]    what client sent / what client received

Both entries in a page share ``pageref == flow.id``.
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
    """Addon exposing ``ccproxy.dump`` — multi-page HAR export."""

    def __init__(self) -> None:
        self._savehar = SaveHar()  # standalone — we only use make_har()

    @command.command("ccproxy.dump")  # type: ignore[untyped-decorator]
    def ccproxy_dump(self, flow_ids: str) -> str:
        """Return a JSON-serialized multi-page HAR for one or more flows.

        ``flow_ids`` is a comma-separated list of mitmproxy flow ids.
        Each flow becomes one page with 2 entries:
        ``[fwdreq, provider_response]`` followed by ``[clireq, client_response]``.
        """
        ids = [fid.strip() for fid in flow_ids.split(",") if fid.strip()]
        if not ids:
            raise ValueError("no flow ids provided")

        real_flows: list[http.HTTPFlow] = []
        for fid in ids:
            flow = self._find_http_flow(fid)
            if flow is None:
                raise ValueError(f"no flow with id {fid}")
            real_flows.append(flow)

        # Interleave: [provider_0, client_0, provider_1, client_1, ...]
        # provider clone: fwdreq + provider_response (raw)
        # client clone:   clireq + client_response (post-transform)
        interleaved: list[http.HTTPFlow] = []
        for real in real_flows:
            interleaved.append(self._build_provider_clone(real))
            interleaved.append(self._build_client_clone(real))

        har = self._savehar.make_har(interleaved)
        entries = har["log"]["entries"]

        pages = []
        for i, flow in enumerate(real_flows):
            page_id = flow.id
            entries[2 * i]["pageref"] = page_id
            entries[2 * i + 1]["pageref"] = page_id
            started_iso = entries[2 * i]["startedDateTime"]
            pages.append(
                {
                    "id": page_id,
                    "title": f"ccproxy flow {page_id}",
                    "startedDateTime": started_iso,
                    "pageTimings": {"onContentLoad": -1, "onLoad": -1},
                },
            )

        har["log"]["pages"] = pages
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
    def _build_provider_clone(flow: http.HTTPFlow) -> http.HTTPFlow:
        """Clone the flow with response replaced by the raw provider response.

        Fallback: if provider_response is absent, the clone keeps the
        post-transform response (identical to client clone).
        """
        clone = cast("http.HTTPFlow", flow.copy())  # type: ignore[no-untyped-call]

        record = flow.metadata.get(InspectorMeta.RECORD)
        snapshot = record.provider_response if record is not None else None
        if snapshot is None:
            return clone

        synthetic = http.Response.make(
            status_code=snapshot.status_code or 200,
            content=snapshot.body,
            headers=snapshot.headers,
        )
        if flow.response:
            synthetic.timestamp_start = flow.response.timestamp_start
            synthetic.timestamp_end = flow.response.timestamp_end
        clone.response = synthetic
        return clone

    @staticmethod
    def _build_client_clone(flow: http.HTTPFlow) -> http.HTTPFlow:
        """Clone the flow and rebuild .request from the client request snapshot.

        The clone keeps the real flow's response (the post-transform
        client-facing response).

        Fallback: if the snapshot is missing, the clone keeps the mutated
        request — entries[1] renders identically to entries[0].
        """
        clone = cast("http.HTTPFlow", flow.copy())  # type: ignore[no-untyped-call]

        record = flow.metadata.get(InspectorMeta.RECORD)
        snapshot = record.client_request if record is not None else None
        if snapshot is None:
            logger.debug("Flow %s has no client request snapshot; falling back", flow.id)
            return clone

        synthetic = http.Request.make(
            method=snapshot.method or "GET",
            url=snapshot.url or "",
            content=snapshot.body,
            headers=snapshot.headers,
        )
        synthetic.timestamp_start = flow.request.timestamp_start
        synthetic.timestamp_end = flow.request.timestamp_end
        clone.request = synthetic
        return clone

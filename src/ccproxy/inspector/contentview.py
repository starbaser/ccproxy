"""Custom mitmproxy content view: client request (pre-pipeline).

Shows the original request as sent by the client, before ccproxy's addon
pipeline (OAuth substitution, header injection, lightllm transform) mutates it.
The default mitmproxy views show the forwarded request (post-pipeline).
"""

from __future__ import annotations

import json

from mitmproxy.contentviews._api import Contentview, Metadata, SyntaxHighlight

from ccproxy.inspector.flow_store import InspectorMeta


class ClientRequestContentview(Contentview):

    @property
    def name(self) -> str:
        return "Client-Request"

    @property
    def syntax_highlight(self) -> SyntaxHighlight:
        return "yaml"

    def prettify(self, data: bytes, metadata: Metadata) -> str:
        flow = metadata.flow
        if flow is None:
            return "(no flow context)"
        record = flow.metadata.get(InspectorMeta.RECORD)
        if record is None or record.client_request is None:
            return "(no client request snapshot)"

        cr = record.client_request
        lines = [
            f"{cr.method} {cr.scheme}://{cr.host}:{cr.port}{cr.path}",
            "",
            "--- Headers ---",
        ]
        for k, v in cr.headers.items():
            lines.append(f"  {k}: {v}")
        lines.append("")
        lines.append("--- Body ---")
        if not cr.body:
            lines.append("(empty)")
        else:
            try:
                lines.append(json.dumps(json.loads(cr.body), indent=2))
            except Exception:
                lines.append(cr.body.decode("utf-8", errors="replace"))
        return "\n".join(lines)

    def render_priority(self, data: bytes, metadata: Metadata) -> float:
        return -1

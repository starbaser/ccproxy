"""Custom mitmproxy content view: client request (pre-pipeline).

Shows the original request as sent by the client, before ccproxy's addon
pipeline (OAuth substitution, header injection, lightllm transform) mutates it.
The default mitmproxy views show the forwarded request (post-pipeline).
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from mitmproxy import flow as flow_mod
from mitmproxy.contentviews import base

from ccproxy.inspector.flow_store import InspectorMeta


class ClientRequestContentview(base.View):
    name: ClassVar[str] = "Client-Request"

    def __call__(
        self,
        data: bytes,
        *,
        flow: flow_mod.Flow | None = None,
        **metadata: Any,
    ) -> base.TViewResult:
        text = self._render(flow)
        return "Client Request", base.format_text(text)

    def render_priority(
        self,
        data: bytes,
        *,
        content_type: str | None = None,
        flow: flow_mod.Flow | None = None,
        http_message: Any = None,
        **unknown_metadata: Any,
    ) -> float:
        return -1

    @staticmethod
    def _render(flow: flow_mod.Flow | None) -> str:
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
            except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
                lines.append(cr.body.decode("utf-8", errors="replace"))
        return "\n".join(lines)

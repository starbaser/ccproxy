"""Compliance seeder addon.

Registers ``ccproxy.seed``: a mitmproxy command that saves the specified
flows verbatim to the provider's seed silo on disk. No extraction, no
filtering, no redaction — the raw ``HTTPFlow`` is persisted as-is.
Invoked by ``ccproxy flows seed --provider X``.
"""

from __future__ import annotations

import json
import logging

from mitmproxy import command, ctx, http

from ccproxy.compliance.store import get_store

logger = logging.getLogger(__name__)


class ComplianceSeeder:
    """Addon exposing ``ccproxy.seed`` — save raw flows as provider seeds."""

    @command.command("ccproxy.seed")  # type: ignore[untyped-decorator]
    def ccproxy_seed(self, flow_ids: str, provider: str) -> str:
        """Save the listed flows verbatim into the provider's seed silo.

        ``flow_ids`` is a comma-separated list of mitmproxy flow ids.
        ``provider`` is the target provider name (e.g. ``anthropic``).
        Returns a JSON summary of the save operation.
        """
        ids = [fid.strip() for fid in flow_ids.split(",") if fid.strip()]
        if not ids:
            raise ValueError("no flow ids provided")

        store = get_store()
        saved = 0
        missing: list[str] = []

        for fid in ids:
            flow = self._find_http_flow(fid)
            if flow is None:
                logger.warning("ccproxy.seed: no flow with id %s, skipping", fid)
                missing.append(fid)
                continue
            store.add(provider, flow)
            saved += 1

        summary: dict[str, object] = {
            "status": "ok" if saved else "empty",
            "provider": provider,
            "flows_saved": saved,
            "missing": missing,
        }

        logger.info(
            "Seeded %d flow(s) under provider %s (%d missing)",
            saved,
            provider,
            len(missing),
        )
        return json.dumps(summary)

    @staticmethod
    def _find_http_flow(flow_id: str) -> http.HTTPFlow | None:
        view = ctx.master.addons.get("view")  # type: ignore[no-untyped-call]
        if view is None:
            return None
        found = view.get_by_id(flow_id)
        return found if isinstance(found, http.HTTPFlow) else None

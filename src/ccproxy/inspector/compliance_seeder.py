"""Compliance profile seeder addon.

Registers ``ccproxy.seed``: a mitmproxy command that builds a
ComplianceProfile from user-selected flows and persists it to the
ProfileStore.  Invoked by ``ccproxy flows seed --provider X``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mitmproxy import command, ctx, http

from ccproxy.compliance.extractor import extract_envelope
from ccproxy.compliance.models import ObservationAccumulator
from ccproxy.compliance.store import get_store
from ccproxy.inspector.flow_store import InspectorMeta

logger = logging.getLogger(__name__)


class ComplianceSeeder:
    """Addon exposing ``ccproxy.seed`` — build profiles from curated flows."""

    @command.command("ccproxy.seed")  # type: ignore[untyped-decorator]
    def ccproxy_seed(self, flow_ids: str, provider: str) -> str:
        """Build a ComplianceProfile from selected flows and persist it.

        ``flow_ids`` is a comma-separated list of mitmproxy flow ids.
        ``provider`` is the target provider name (e.g. 'anthropic').
        Returns a JSON summary of the seeded profile.
        """
        ids = [fid.strip() for fid in flow_ids.split(",") if fid.strip()]
        if not ids:
            raise ValueError("no flow ids provided")

        extra_headers, extra_fields = _load_classifier_config()

        user_agent = "seed"
        snapshots_used = 0
        acc = ObservationAccumulator(provider=provider, user_agent=user_agent)

        for fid in ids:
            flow = self._find_http_flow(fid)
            if flow is None:
                logger.warning("ccproxy.seed: no flow with id %s, skipping", fid)
                continue

            record = flow.metadata.get(InspectorMeta.RECORD)
            if record is None or record.client_request is None:
                logger.warning("ccproxy.seed: flow %s has no client request snapshot, skipping", fid)
                continue

            snapshot = record.client_request

            if snapshots_used == 0:
                ua = snapshot.headers.get("user-agent") or snapshot.headers.get("User-Agent")
                if ua:
                    user_agent = ua
                    acc.user_agent = user_agent

            envelope = extract_envelope(
                snapshot,
                additional_header_exclusions=extra_headers,
                additional_body_content_fields=extra_fields,
            )
            acc.submit(envelope)
            snapshots_used += 1

        if snapshots_used == 0:
            raise ValueError("no valid flows with client request snapshots")

        profile = acc.finalize()
        key = f"{provider}/seed"

        store = get_store()
        store.set_profile(key, profile)

        env = profile.envelope
        summary: dict[str, Any] = {
            "status": "ok",
            "key": key,
            "flows_used": snapshots_used,
            "user_agent": profile.user_agent,
            "headers": len(env.headers),
            "body_fields": len(env.body_fields),
            "system": env.system is not None,
            "body_wrapper": env.body_wrapper,
        }

        logger.info(
            "Seeded compliance profile %s: %d flows, %d headers, %d body fields, system=%s",
            key,
            snapshots_used,
            len(env.headers),
            len(env.body_fields),
            env.system is not None,
        )

        return json.dumps(summary)

    @staticmethod
    def _find_http_flow(flow_id: str) -> http.HTTPFlow | None:
        view = ctx.master.addons.get("view")  # type: ignore[no-untyped-call]
        if view is None:
            return None
        found = view.get_by_id(flow_id)
        return found if isinstance(found, http.HTTPFlow) else None


def _load_classifier_config() -> tuple[frozenset[str], frozenset[str]]:
    """Load additional classifier exclusions from config."""
    try:
        from ccproxy.config import get_config

        cfg = get_config()
        extra_headers = frozenset(h.lower() for h in cfg.compliance.additional_header_exclusions)
        extra_fields = frozenset(cfg.compliance.additional_body_content_fields)
        return extra_headers, extra_fields
    except Exception:
        return frozenset(), frozenset()

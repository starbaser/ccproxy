"""Apply a compliance profile onto a pipeline Context.

Two-phase pipeline: prepare_envelope() builds a materialized Envelope
from the profile, then wrap() fills it with the incoming request.
Subclass ComplianceStamper to override either phase.
"""

from __future__ import annotations

import importlib
import json
import logging
import re
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ccproxy.compliance.models import ComplianceProfile

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)


@dataclass
class MaterializedEnvelope:
    """Envelope with generated values materialized and identity extracted."""

    headers: dict[str, str] = field(default_factory=dict)
    body_fields: dict[str, Any] = field(default_factory=dict)
    system: list[dict[str, Any]] | None = None
    body_wrapper: str | None = None
    metadata_user_id: str | None = None


class ComplianceStamper:
    """Applies a compliance profile onto a request context.

    Subclass to override prepare_envelope() (what goes into the
    envelope) or wrap() (how the envelope merges into the request).
    """

    envelope_exclusions: frozenset[str] = frozenset(
        {
            "thinking",
            "context_management",
            "output_config",
        }
    )

    generated_fields: frozenset[str] = frozenset(
        {
            "user_prompt_id",
        }
    )

    list_valued_headers: frozenset[str] = frozenset({"anthropic-beta"})

    def __init__(self, ctx: Context, profile: ComplianceProfile) -> None:
        self.ctx = ctx
        self.profile = profile

    def stamp(self) -> None:
        envelope = self.prepare_envelope()
        self.wrap(envelope)

    def prepare_envelope(self) -> MaterializedEnvelope:
        """Build a materialized envelope from the profile.

        Filters exclusions, generates per-request values, extracts
        session identity from metadata.  Pure — no ctx access.
        """
        src = self.profile.envelope

        headers = dict(src.headers)

        body_fields: dict[str, Any] = {}
        metadata_user_id: str | None = None

        for path, value in src.body_fields.items():
            if path in self.envelope_exclusions:
                continue
            if path == "metadata" and isinstance(value, dict):
                metadata_user_id = self._synthesize_identity(value)
                continue
            if path in self.generated_fields:
                body_fields[path] = uuid.uuid4().hex[:13]
                continue
            body_fields[path] = deepcopy(value)

        return MaterializedEnvelope(
            headers=headers,
            body_fields=body_fields,
            system=src.system,
            body_wrapper=src.body_wrapper,
            metadata_user_id=metadata_user_id,
        )

    def wrap(self, envelope: MaterializedEnvelope) -> None:
        """Fill the envelope with the incoming request.

        Order matters: metadata lands inside the body wrapper,
        body_fields land outside.
        """
        self._apply_headers(envelope)
        self._apply_session_metadata(envelope)
        self._apply_body_wrapper(envelope)
        self._apply_body_fields(envelope)
        self._apply_system(envelope)

    def _apply_headers(self, envelope: MaterializedEnvelope) -> None:
        for name, value in envelope.headers.items():
            if name.lower() in self.list_valued_headers:
                existing = self.ctx.get_header(name)
                if existing:
                    merged = self._union_csv_tokens(existing, value)
                    if merged != existing:
                        self.ctx.set_header(name, merged)
                    continue
            self.ctx.set_header(name, value)

    def _apply_session_metadata(self, envelope: MaterializedEnvelope) -> None:
        if not envelope.metadata_user_id:
            return
        metadata = self.ctx._body.setdefault("metadata", {})
        if metadata.get("user_id"):
            return
        metadata["user_id"] = envelope.metadata_user_id

    def _apply_body_wrapper(self, envelope: MaterializedEnvelope) -> None:
        if not envelope.body_wrapper:
            return

        body = self.ctx._body
        wrapper_field = envelope.body_wrapper

        if wrapper_field in body:
            return

        model = body.pop("model", None)
        if not model:
            from ccproxy.inspector.flow_store import InspectorMeta

            record = self.ctx.flow.metadata.get(InspectorMeta.RECORD)
            if record and getattr(record, "transform", None):
                model = record.transform.model or None
        if not model:
            model = self._extract_model_from_path()

        wrapped = dict(body)
        body.clear()
        if model:
            body["model"] = model
        body[wrapper_field] = wrapped

    def _apply_body_fields(self, envelope: MaterializedEnvelope) -> None:
        body = self.ctx._body
        for path, value in envelope.body_fields.items():
            if path not in body:
                body[path] = value

    def _apply_system(self, envelope: MaterializedEnvelope) -> None:
        if envelope.system is None or not envelope.system:
            return

        profile_blocks = envelope.system
        current = self.ctx.system

        if current is None:
            self.ctx.system = profile_blocks
            return

        if isinstance(current, str):
            self.ctx.system = [*profile_blocks, {"type": "text", "text": current}]
            return

        if isinstance(current, list):
            if self._list_contains_profile(current, profile_blocks):
                return
            self.ctx.system = [*profile_blocks, *current]

    @staticmethod
    def _union_csv_tokens(existing: str, additional: str) -> str:
        seen: set[str] = set()
        result: list[str] = []
        for token in [*existing.split(","), *additional.split(",")]:
            token = token.strip()
            if token and token not in seen:
                seen.add(token)
                result.append(token)
        return ",".join(result)

    @staticmethod
    def _list_contains_profile(
        current: list[dict[str, Any]],
        profile_blocks: list[dict[str, Any]],
    ) -> bool:
        for pb in profile_blocks:
            pb_text = pb.get("text")
            if not isinstance(pb_text, str) or not pb_text:
                continue
            for cb in current:
                cb_text = cb.get("text") if isinstance(cb, dict) else None
                if isinstance(cb_text, str) and cb_text.startswith(pb_text):
                    return True
        return False

    def _extract_model_from_path(self) -> str | None:
        path = self.ctx.flow.request.path
        match = re.search(r"/models/([^/:]+)", path)
        return match.group(1) if match else None

    @staticmethod
    def _synthesize_identity(metadata_value: dict[str, Any]) -> str | None:
        user_id_raw = metadata_value.get("user_id")
        if not user_id_raw:
            return None
        try:
            data = json.loads(str(user_id_raw))
            if not isinstance(data, dict):
                return None
        except (json.JSONDecodeError, TypeError):
            return None

        identity: dict[str, Any] = {}
        if "device_id" in data:
            identity["device_id"] = data["device_id"]
        if "account_uuid" in data:
            identity["account_uuid"] = data["account_uuid"]

        if not identity:
            return None

        identity["session_id"] = str(uuid.uuid4())
        return json.dumps(identity)


def resolve_stamper_class(dotted_path: str) -> type[ComplianceStamper]:
    """Resolve a dotted import path to a ComplianceStamper subclass."""
    module_path, _, class_name = dotted_path.rpartition(".")
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    if not (isinstance(cls, type) and issubclass(cls, ComplianceStamper)):
        raise TypeError(f"{dotted_path} is not a ComplianceStamper subclass")
    return cls

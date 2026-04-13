"""Merge a compliance profile onto a pipeline Context.

All merge operations are idempotent. Subclass ComplianceMerger to
override individual operations.
"""

from __future__ import annotations

import importlib
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from ccproxy.compliance.models import ComplianceProfile

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)

# Body fields that are feature config, not compliance — never stamped
_BODY_MERGE_EXCLUSIONS = frozenset({
    "thinking",
    "context_management",
    "output_config",
})

# Body fields that need fresh generation per-request (like session_id)
_BODY_GENERATE_FIELDS = frozenset({
    "user_prompt_id",
})


class ComplianceMerger:
    """Base compliance merger. Subclass to override individual operations."""

    def __init__(self, ctx: Context, profile: ComplianceProfile) -> None:
        self.ctx = ctx
        self.profile = profile

    def merge(self) -> None:
        self.merge_headers()
        self.merge_session_metadata()
        self.wrap_body()
        self.merge_body_fields()
        self.merge_system()

    def merge_headers(self) -> None:
        for feature in self.profile.headers:
            existing = self.ctx.get_header(feature.name)
            if not existing:
                self.ctx.set_header(feature.name, feature.value)
                logger.debug("Compliance: added header %s", feature.name)

    def merge_session_metadata(self) -> None:
        """Synthesize session metadata from profile identity fields.

        Uses device_id and account_uuid from the profile, generates a
        fresh session_id. Only applies if metadata.user_id is absent.
        """
        device_id: str | None = None
        account_uuid: str | None = None

        for feature in self.profile.body_fields:
            if feature.path == "metadata" and isinstance(feature.value, dict):
                user_id_raw = feature.value.get("user_id")
                if user_id_raw:
                    identity_out: dict[str, Any] = {}
                    self._extract_identity(str(user_id_raw), identity_out)
                    device_id = identity_out.get("device_id")
                    account_uuid = identity_out.get("account_uuid")

        if not device_id and not account_uuid:
            return

        metadata = self.ctx._body.setdefault("metadata", {})
        if metadata.get("user_id"):
            return

        identity: dict[str, Any] = {}
        if device_id:
            identity["device_id"] = device_id
        if account_uuid:
            identity["account_uuid"] = account_uuid
        identity["session_id"] = str(uuid.uuid4())

        metadata["user_id"] = json.dumps(identity)
        logger.debug("Compliance: synthesized session metadata")

    def wrap_body(self) -> None:
        """Wrap the request body inside a wrapper field if the profile requires it.

        cloudcode-pa style: {model: X, project: Y, request: {<actual API payload>}}
        """
        if not self.profile.body_wrapper:
            return

        body = self.ctx._body
        wrapper_field = self.profile.body_wrapper

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

        logger.debug("Compliance: wrapped body in '%s'", wrapper_field)

    def merge_body_fields(self) -> None:
        """Add compliance-relevant body envelope fields that are missing.

        Skips feature config fields (thinking, context_management, output_config)
        which are user choices, not compliance requirements. Generates fresh
        values for per-request fields (user_prompt_id).
        """
        body = self.ctx._body
        for feature in self.profile.body_fields:
            if feature.path in _BODY_MERGE_EXCLUSIONS:
                continue
            if feature.path in _BODY_GENERATE_FIELDS:
                if feature.path not in body:
                    body[feature.path] = uuid.uuid4().hex[:13]
                    logger.debug("Compliance: generated %s", feature.path)
                continue
            if feature.path not in body:
                body[feature.path] = feature.value
                logger.debug("Compliance: added body field %s", feature.path)

    def merge_system(self) -> None:
        """Inject the profile's system prompt when the request lacks one.

        Structured system blocks (list) indicate a client that manages its
        own identity (Claude CLI, Agent SDK) — skip injection entirely.
        String or absent system prompts get the profile's blocks prepended.
        """
        if self.profile.system is None:
            return

        profile_blocks = self.profile.system.structure
        if not profile_blocks:
            return

        current = self.ctx.system

        if current is None:
            self.ctx.system = profile_blocks
            return

        if isinstance(current, list):
            return

        if isinstance(current, str):
            self.ctx.system = [*profile_blocks, {"type": "text", "text": current}]

    def _extract_model_from_path(self) -> str | None:
        """Extract model name from URL path patterns like /models/{model}:method."""
        import re

        path = self.ctx.flow.request.path
        match = re.search(r"/models/([^/:]+)", path)
        return match.group(1) if match else None

    def _extract_identity(self, user_id_str: str, out: dict[str, Any]) -> None:
        """Parse identity fields from a user_id JSON string."""
        try:
            data = json.loads(user_id_str)
            if isinstance(data, dict):
                if "device_id" in data:
                    out["device_id"] = data["device_id"]
                if "account_uuid" in data:
                    out["account_uuid"] = data["account_uuid"]
        except (json.JSONDecodeError, TypeError):
            pass


def resolve_merger_class(dotted_path: str) -> type[ComplianceMerger]:
    """Resolve a dotted import path to a ComplianceMerger subclass."""
    module_path, _, class_name = dotted_path.rpartition(".")
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    if not (isinstance(cls, type) and issubclass(cls, ComplianceMerger)):
        raise TypeError(f"{dotted_path} is not a ComplianceMerger subclass")
    return cls

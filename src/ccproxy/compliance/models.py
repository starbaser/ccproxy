"""Data models for the compliance profile learning system.

Profiles are keyed by (provider, user_agent). An ObservationAccumulator
collects feature candidates across multiple observations. Once
min_observations is reached, stable features (identical across all
observations) are finalized into a ComplianceProfile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class ProfileFeatureHeader:
    """A learned header that should be present on compliant requests."""

    name: str
    value: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "value": self.value}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ProfileFeatureHeader:
        return cls(name=d["name"], value=d["value"])


@dataclass
class ProfileFeatureBodyField:
    """A learned body envelope field (non-content) that should be present."""

    path: str
    value: Any

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "value": self.value}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ProfileFeatureBodyField:
        return cls(path=d["path"], value=d["value"])


@dataclass
class ProfileFeatureSystem:
    """Learned system prompt structure (block layout with cache_control etc.)."""

    structure: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {"structure": self.structure}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ProfileFeatureSystem:
        return cls(structure=d["structure"])


@dataclass
class ComplianceProfile:
    """Finalized compliance profile for a (provider, user_agent) pair."""

    provider: str
    user_agent: str
    created_at: str
    updated_at: str
    observation_count: int
    is_complete: bool
    headers: list[ProfileFeatureHeader] = field(default_factory=list)
    body_fields: list[ProfileFeatureBodyField] = field(default_factory=list)
    system: ProfileFeatureSystem | None = None
    body_wrapper: str | None = None
    """If set, the user's request body is nested inside this field name.
    e.g. 'request' means the body becomes {request: {<original body>}}."""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "provider": self.provider,
            "user_agent": self.user_agent,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "observation_count": self.observation_count,
            "is_complete": self.is_complete,
            "headers": [h.to_dict() for h in self.headers],
            "body_fields": [f.to_dict() for f in self.body_fields],
            "system": self.system.to_dict() if self.system else None,
            "body_wrapper": self.body_wrapper,
        }
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ComplianceProfile:
        return cls(
            provider=d["provider"],
            user_agent=d["user_agent"],
            created_at=d["created_at"],
            updated_at=d["updated_at"],
            observation_count=d["observation_count"],
            is_complete=d["is_complete"],
            headers=[ProfileFeatureHeader.from_dict(h) for h in d.get("headers", [])],
            body_fields=[ProfileFeatureBodyField.from_dict(f) for f in d.get("body_fields", [])],
            system=ProfileFeatureSystem.from_dict(d["system"]) if d.get("system") else None,
            body_wrapper=d.get("body_wrapper"),
        )


@dataclass
class ObservationBundle:
    """Extracted features from a single observed ClientRequest."""

    provider: str
    user_agent: str
    headers: dict[str, str]
    body_envelope: dict[str, Any]
    system: Any = None
    body_wrapper: str | None = None
    """Field name that wraps the actual API payload (e.g. 'request' for cloudcode-pa)."""


@dataclass
class ObservationAccumulator:
    """Accumulates observations for a (provider, user_agent) pair.

    Tracks all seen values for each candidate feature. After
    min_observations, features with a single unique value are "stable"
    and included in the finalized profile.
    """

    provider: str
    user_agent: str
    observation_count: int = 0
    header_candidates: dict[str, list[str]] = field(default_factory=dict)
    body_candidates: dict[str, list[Any]] = field(default_factory=dict)
    system_observations: list[Any] = field(default_factory=list)
    body_wrapper_observations: list[str | None] = field(default_factory=list)
    last_seen: float = 0.0

    def submit(self, bundle: ObservationBundle) -> None:
        """Incorporate a new observation into the accumulator."""
        self.observation_count += 1
        self.last_seen = datetime.now(tz=UTC).timestamp()

        for name, value in bundle.headers.items():
            self.header_candidates.setdefault(name, []).append(value)

        for path, value in bundle.body_envelope.items():
            self.body_candidates.setdefault(path, []).append(value)

        if bundle.system is not None:
            self.system_observations.append(bundle.system)

        self.body_wrapper_observations.append(bundle.body_wrapper)

    def finalize(self) -> ComplianceProfile:
        """Produce a ComplianceProfile from accumulated observations.

        Features where all observed values are identical are "stable"
        and included. Variable features are excluded.
        """
        now = datetime.now(tz=UTC).isoformat()

        stable_headers: list[ProfileFeatureHeader] = []
        for name, values in self.header_candidates.items():
            if len(set(values)) == 1:
                stable_headers.append(ProfileFeatureHeader(name=name, value=values[0]))

        stable_body: list[ProfileFeatureBodyField] = []
        for path, values in self.body_candidates.items():
            serialized = [_serialize_for_comparison(v) for v in values]
            if len(set(serialized)) == 1:
                stable_body.append(ProfileFeatureBodyField(path=path, value=values[0]))

        system_feature: ProfileFeatureSystem | None = None
        if self.system_observations:
            serialized_sys = [_serialize_for_comparison(s) for s in self.system_observations]
            if len(set(serialized_sys)) == 1:
                system_val = self.system_observations[0]
                if isinstance(system_val, list):
                    system_feature = ProfileFeatureSystem(structure=system_val)
                elif isinstance(system_val, str):
                    system_feature = ProfileFeatureSystem(
                        structure=[{"type": "text", "text": system_val}]
                    )

        # body_wrapper is stable if all observations agree
        wrapper_values = [w for w in self.body_wrapper_observations if w is not None]
        body_wrapper = wrapper_values[0] if wrapper_values and len(set(wrapper_values)) == 1 else None

        return ComplianceProfile(
            provider=self.provider,
            user_agent=self.user_agent,
            created_at=now,
            updated_at=now,
            observation_count=self.observation_count,
            is_complete=True,
            headers=stable_headers,
            body_fields=stable_body,
            system=system_feature,
            body_wrapper=body_wrapper,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "user_agent": self.user_agent,
            "observation_count": self.observation_count,
            "header_candidates": self.header_candidates,
            "body_candidates": self.body_candidates,
            "system_observations": self.system_observations,
            "body_wrapper_observations": self.body_wrapper_observations,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ObservationAccumulator:
        return cls(
            provider=d["provider"],
            user_agent=d["user_agent"],
            observation_count=d["observation_count"],
            header_candidates=d.get("header_candidates", {}),
            body_candidates=d.get("body_candidates", {}),
            system_observations=d.get("system_observations", []),
            body_wrapper_observations=d.get("body_wrapper_observations", []),
            last_seen=d.get("last_seen", 0.0),
        )


def _serialize_for_comparison(value: Any) -> str:
    """Serialize a value for set-based deduplication."""
    import json

    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value)

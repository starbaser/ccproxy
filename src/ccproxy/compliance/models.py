"""Data models for the compliance profile system.

Profiles are keyed by (provider, user_agent). An ObservationAccumulator
collects feature candidates across multiple observations. Once
min_observations is reached, stable features (identical across all
observations) are finalized into a ComplianceProfile.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class Envelope:
    """The HTTP request shape — headers, body envelope fields, system
    prompt blocks, and optional body wrapper.  Shared currency across
    extraction, accumulation, persistence, and stamping.
    """

    headers: dict[str, str] = field(default_factory=dict)
    body_fields: dict[str, Any] = field(default_factory=dict)
    system: list[dict[str, Any]] | None = None
    body_wrapper: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "headers": dict(self.headers),
            "body_fields": dict(self.body_fields),
            "system": self.system,
            "body_wrapper": self.body_wrapper,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Envelope:
        return cls(
            headers=d.get("headers", {}),
            body_fields=d.get("body_fields", {}),
            system=d.get("system"),
            body_wrapper=d.get("body_wrapper"),
        )


@dataclass
class ComplianceProfile:
    """Finalized compliance profile for a (provider, user_agent) pair."""

    provider: str
    user_agent: str
    created_at: str
    updated_at: str
    observation_count: int
    is_complete: bool
    envelope: Envelope = field(default_factory=Envelope)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "user_agent": self.user_agent,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "observation_count": self.observation_count,
            "is_complete": self.is_complete,
            "envelope": self.envelope.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ComplianceProfile:
        return cls(
            provider=d["provider"],
            user_agent=d["user_agent"],
            created_at=d["created_at"],
            updated_at=d["updated_at"],
            observation_count=d["observation_count"],
            is_complete=d["is_complete"],
            envelope=Envelope.from_dict(d.get("envelope", {})),
        )


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

    def submit(self, envelope: Envelope) -> None:
        self.observation_count += 1
        self.last_seen = datetime.now(tz=UTC).timestamp()

        for name, value in envelope.headers.items():
            self.header_candidates.setdefault(name, []).append(value)

        for path, value in envelope.body_fields.items():
            self.body_candidates.setdefault(path, []).append(value)

        if envelope.system is not None:
            self.system_observations.append(envelope.system)

        self.body_wrapper_observations.append(envelope.body_wrapper)

    def finalize(self) -> ComplianceProfile:
        """Produce a ComplianceProfile from accumulated observations."""
        now = datetime.now(tz=UTC).isoformat()

        stable_headers: dict[str, str] = {}
        for name, values in self.header_candidates.items():
            if len(set(values)) == 1:
                stable_headers[name] = values[0]

        stable_body: dict[str, Any] = {}
        for path, values in self.body_candidates.items():
            serialized = [_serialize_for_comparison(v) for v in values]
            if len(set(serialized)) == 1:
                stable_body[path] = values[0]

        system: list[dict[str, Any]] | None = None
        if self.system_observations:
            serialized_sys = [_serialize_for_comparison(s) for s in self.system_observations]
            if len(set(serialized_sys)) == 1:
                system = self.system_observations[0]

        wrapper_values = [w for w in self.body_wrapper_observations if w is not None]
        body_wrapper = wrapper_values[0] if wrapper_values and len(set(wrapper_values)) == 1 else None

        return ComplianceProfile(
            provider=self.provider,
            user_agent=self.user_agent,
            created_at=now,
            updated_at=now,
            observation_count=self.observation_count,
            is_complete=True,
            envelope=Envelope(
                headers=stable_headers,
                body_fields=stable_body,
                system=system,
                body_wrapper=body_wrapper,
            ),
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
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value)

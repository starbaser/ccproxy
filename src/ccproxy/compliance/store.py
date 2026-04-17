"""ProfileStore — persistent compliance profile storage.

Thread-safe singleton that persists profiles and accumulators to a
JSON file in the config directory. Atomic writes via temp+rename.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from ccproxy.compliance.models import (
    ComplianceProfile,
    ObservationAccumulator,
    ObservationBundle,
    ProfileFeatureHeader,
    ProfileFeatureSystem,
)

logger = logging.getLogger(__name__)

_FORMAT_VERSION = 1


class ProfileStore:
    """Thread-safe persistent store for compliance profiles."""

    def __init__(
        self,
        store_path: Path,
        min_observations: int = 3,
        seed_profiles: list[ComplianceProfile] | None = None,
    ) -> None:
        self._path = store_path
        self._min_observations = min_observations
        self._lock = threading.Lock()

        self._profiles: dict[str, ComplianceProfile] = {}
        self._accumulators: dict[str, ObservationAccumulator] = {}
        self._is_degraded: bool = False

        self._load()

        if seed_profiles:
            seeded = False
            for profile in seed_profiles:
                key = _make_key(profile.provider, profile.user_agent)
                if key not in self._profiles:
                    self._profiles[key] = profile
                    logger.info("Seeded compliance profile for %s (ua=%s)", profile.provider, profile.user_agent)
                    seeded = True
            if seeded:
                self._flush()

    def submit_observation(self, bundle: ObservationBundle) -> None:
        key = _make_key(bundle.provider, bundle.user_agent)

        with self._lock:
            acc = self._accumulators.get(key)
            if acc is None:
                acc = ObservationAccumulator(provider=bundle.provider, user_agent=bundle.user_agent)
                self._accumulators[key] = acc

            acc.submit(bundle)
            logger.info(
                "Compliance observation %d/%d for %s (ua=%s)",
                acc.observation_count,
                self._min_observations,
                bundle.provider,
                bundle.user_agent,
            )

            if acc.observation_count >= self._min_observations:
                profile = acc.finalize()
                self._profiles[key] = profile
                logger.info(
                    "Compliance profile finalized for %s: %d headers, %d body fields, system=%s",
                    bundle.provider,
                    len(profile.headers),
                    len(profile.body_fields),
                    profile.system is not None,
                )
                self._flush()
            elif acc.observation_count % 10 == 0:
                self._flush()

    def get_profile(self, provider: str, ua_hint: str | None = None) -> ComplianceProfile | None:
        """Look up a complete profile for a provider.

        If ``ua_hint`` is given, only profiles whose user_agent contains
        the hint (substring match) are considered. Returns the most
        recently updated match, or None.
        """
        with self._lock:
            match: ComplianceProfile | None = None
            for profile in self._profiles.values():
                if profile.provider != provider or not profile.is_complete:
                    continue
                if ua_hint and ua_hint not in profile.user_agent:
                    continue
                if match is None or profile.updated_at > match.updated_at:
                    match = profile
            return match

    def get_all_profiles(self) -> dict[str, ComplianceProfile]:
        with self._lock:
            return dict(self._profiles)

    @property
    def is_degraded(self) -> bool:
        """True when the store discarded profiles due to a format version mismatch."""
        return self._is_degraded

    def _load(self) -> None:
        if not self._path.exists():
            return

        try:
            data = json.loads(self._path.read_text())
            if data.get("format_version") != _FORMAT_VERSION:
                has_data = bool(data.get("profiles") or data.get("accumulators"))
                if has_data:
                    self._is_degraded = True
                    logger.warning(
                        "Compliance profile format version %r (expected %r) — "
                        "profiles discarded. Delete %s to start fresh.",
                        data.get("format_version"),
                        _FORMAT_VERSION,
                        self._path,
                    )
                else:
                    logger.debug(
                        "Compliance profile format version %r (expected %r), no data present",
                        data.get("format_version"),
                        _FORMAT_VERSION,
                    )
                return

            for key, pd in data.get("profiles", {}).items():
                self._profiles[key] = ComplianceProfile.from_dict(pd)

            for key, ad in data.get("accumulators", {}).items():
                self._accumulators[key] = ObservationAccumulator.from_dict(ad)

            logger.info(
                "Loaded %d compliance profiles, %d accumulators from %s",
                len(self._profiles),
                len(self._accumulators),
                self._path,
            )
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Malformed compliance profiles file, starting fresh: %s", e)

    def _flush(self) -> None:
        """Persist current state to disk atomically."""
        data: dict[str, Any] = {
            "format_version": _FORMAT_VERSION,
            "profiles": {k: v.to_dict() for k, v in self._profiles.items()},
            "accumulators": {k: v.to_dict() for k, v in self._accumulators.items()},
        }

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2, default=str))
            tmp.rename(self._path)
        except OSError as e:
            logger.error("Failed to write compliance profiles: %s", e)


def _make_key(provider: str, user_agent: str) -> str:
    return f"{provider}/{user_agent}"


def _build_anthropic_seed_profile() -> ComplianceProfile:
    """Construct the Anthropic v0 seed ComplianceProfile from known constants."""
    from ccproxy.constants import ANTHROPIC_BETA_HEADERS, CLAUDE_CODE_SYSTEM_PREFIX

    return ComplianceProfile(
        provider="anthropic",
        user_agent="v0-seed",
        created_at="1970-01-01T00:00:00+00:00",
        updated_at="1970-01-01T00:00:00+00:00",
        observation_count=0,
        is_complete=True,
        headers=[
            ProfileFeatureHeader(name="anthropic-beta", value=",".join(ANTHROPIC_BETA_HEADERS)),
            ProfileFeatureHeader(name="anthropic-version", value="2023-06-01"),
        ],
        body_fields=[],
        system=ProfileFeatureSystem(structure=[{"type": "text", "text": CLAUDE_CODE_SYSTEM_PREFIX}]),
    )


# --- Singleton ---

_store_instance: ProfileStore | None = None
_store_lock = threading.Lock()


def get_store() -> ProfileStore:
    global _store_instance
    if _store_instance is None:
        with _store_lock:
            if _store_instance is None:
                _store_instance = _create_store()
    return _store_instance


def _create_store() -> ProfileStore:
    from ccproxy.config import get_config, get_config_dir

    config = get_config()
    config_dir = get_config_dir()

    if config.compliance.profile_path:
        store_path = Path(config.compliance.profile_path).expanduser()
    else:
        store_path = config_dir / "compliance_profiles.json"

    seed_profiles: list[ComplianceProfile] | None = None
    if config.compliance.seed_anthropic:
        seed_profiles = [_build_anthropic_seed_profile()]

    return ProfileStore(
        store_path=store_path,
        min_observations=config.compliance.min_observations,
        seed_profiles=seed_profiles,
    )


def clear_store_instance() -> None:
    """Clear the singleton (for testing)."""
    global _store_instance
    _store_instance = None

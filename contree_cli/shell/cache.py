"""TTL-aware persistent cache for completion sources.

Wraps :class:`contree_cli.session.ImageCache` (sqlite-backed
``MutableMapping[(image_uuid, kind), object]``) to add a fetched-at
timestamp and per-source TTL semantics. Keys are namespaced by the active
profile name so a stale image list from one profile is never returned to
another after ``contree auth switch``.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass

from contree_cli.session import ImageCache


@dataclass(frozen=True)
class CacheEntry:
    value: object
    fetched_at: float

    def is_fresh(self, ttl: float, now: float | None = None) -> bool:
        if ttl <= 0:
            return True
        current = now if now is not None else time.time()
        return current - self.fetched_at < ttl


class SourceCache:
    """Adapter over :class:`ImageCache` that adds TTL bookkeeping."""

    __slots__ = ("backend", "profile")

    def __init__(self, backend: ImageCache, profile: str) -> None:
        self.backend = backend
        self.profile = profile

    def namespaced(self, scope: str, kind: str) -> tuple[str, str]:
        """Build a profile-scoped key."""
        if scope:
            return (f"profile:{self.profile}:{scope}", kind)
        return (f"profile:{self.profile}", kind)

    def get(self, scope: str, kind: str, ttl: float) -> object | None:
        key = self.namespaced(scope, kind)
        try:
            raw = self.backend[key]
        except KeyError:
            return None
        if not isinstance(raw, dict):
            return None
        if "value" not in raw or "fetched_at" not in raw:
            return None
        entry = CacheEntry(
            value=raw["value"],
            fetched_at=float(raw["fetched_at"]),
        )
        if not entry.is_fresh(ttl):
            return None
        return entry.value

    def set(self, scope: str, kind: str, value: object) -> None:
        key = self.namespaced(scope, kind)
        self.backend[key] = {"value": value, "fetched_at": time.time()}

    def invalidate(self, scope: str, kind: str) -> None:
        key = self.namespaced(scope, kind)
        with contextlib.suppress(KeyError):
            del self.backend[key]

    def invalidate_kind_prefix(self, kind_prefix: str) -> None:
        """Drop every cache entry whose kind starts with *kind_prefix*."""
        scope_prefix = f"profile:{self.profile}"
        to_drop = [
            (image_uuid, kind)
            for image_uuid, kind in list(self.backend)
            if image_uuid.startswith(scope_prefix) and kind.startswith(kind_prefix)
        ]
        for key in to_drop:
            with contextlib.suppress(KeyError):
                del self.backend[key]

    def invalidate_scope(self, scope: str) -> None:
        """Drop every cache entry under the given scope (e.g. an image uuid)."""
        prefix = (
            f"profile:{self.profile}:{scope}" if scope else f"profile:{self.profile}"
        )
        to_drop = [
            (image_uuid, kind)
            for image_uuid, kind in list(self.backend)
            if image_uuid == prefix or image_uuid.startswith(prefix + ":")
        ]
        for key in to_drop:
            with contextlib.suppress(KeyError):
                del self.backend[key]

    def invalidate_all(self) -> None:
        """Drop every entry under the active profile."""
        prefix = f"profile:{self.profile}"
        to_drop = [
            (image_uuid, kind)
            for image_uuid, kind in list(self.backend)
            if image_uuid == prefix or image_uuid.startswith(prefix + ":")
        ]
        for key in to_drop:
            with contextlib.suppress(KeyError):
                del self.backend[key]

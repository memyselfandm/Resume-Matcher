"""MCP-side cache of tailoring previews awaiting confirmation.

``POST /resumes/improve/confirm`` needs the full previewed payload, but the
backend persists only hashes and improvements. The web UI keeps the payload in
browser state; MCP clients get a ``preview_id`` handle instead and the payload
lives here until it is confirmed or expires.
"""

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

DEFAULT_MAX_PREVIEWS = 64


@dataclass(frozen=True)
class CachedPreview:
    """Everything the confirm route needs to persist a previewed resume."""

    preview_id: str
    resume_id: str
    job_id: str
    improved_data: dict[str, Any]
    improvements: list[dict[str, Any]]
    expires_at: float


def preview_expiry(expires_at_iso: str | None, fallback_ttl_seconds: float, now: float) -> float:
    """Convert the preview's ISO-8601 expiry to epoch seconds (TTL fallback)."""
    if expires_at_iso:
        try:
            return datetime.fromisoformat(expires_at_iso).timestamp()
        except ValueError:
            pass
    return now + fallback_ttl_seconds


class PreviewCache:
    """Bounded LRU of previews keyed by ``preview_id``, expiring with the preview."""

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_PREVIEWS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._entries: OrderedDict[str, CachedPreview] = OrderedDict()
        self._max_entries = max_entries
        self._clock = clock

    def __len__(self) -> int:
        return len(self._entries)

    def now(self) -> float:
        """Current time on the cache's clock (epoch seconds)."""
        return self._clock()

    def put(self, preview: CachedPreview) -> None:
        """Store a preview, evicting expired entries and then the least recent."""
        self._evict_expired()
        self._entries[preview.preview_id] = preview
        self._entries.move_to_end(preview.preview_id)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def get(self, preview_id: str) -> CachedPreview | None:
        """Return a live preview, or None when unknown, evicted or expired."""
        preview = self._entries.get(preview_id)
        if preview is None:
            return None
        if preview.expires_at <= self._clock():
            del self._entries[preview_id]
            return None
        self._entries.move_to_end(preview_id)
        return preview

    def discard(self, preview_id: str) -> None:
        """Drop a preview (after a successful confirmation)."""
        self._entries.pop(preview_id, None)

    def _evict_expired(self) -> None:
        now = self._clock()
        for preview_id in [key for key, value in self._entries.items() if value.expires_at <= now]:
            del self._entries[preview_id]

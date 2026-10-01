"""Disk cache for API responses.

Rationale: incident investigation is iterative. You run the same window three or
four times while you refine the service name or add a flag. Without a cache you
pay CloudWatch four times for identical data; with it you pay once. Entries are
keyed on (operation, params) and expire after --cache-ttl seconds (default 900).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable


def cache_dir() -> Path:
    base = os.environ.get("SRE_CACHE_DIR")
    if base:
        return Path(base)
    xdg = os.environ.get("XDG_CACHE_HOME")
    root = Path(xdg) if xdg else Path.home() / ".cache"
    return root / "sre-toolkit"


class Cache:
    def __init__(self, ttl: int = 900, enabled: bool = True):
        self.ttl = ttl
        self.enabled = enabled and ttl > 0
        self.root = cache_dir()
        self.hits = 0
        self.misses = 0

    def _path(self, operation: str, params: dict[str, Any]) -> Path:
        blob = json.dumps({"op": operation, "p": params}, sort_keys=True, default=str)
        digest = hashlib.sha256(blob.encode()).hexdigest()[:24]
        safe_op = operation.replace(":", "_").replace("/", "_")
        return self.root / safe_op / f"{digest}.json"

    def get(self, operation: str, params: dict[str, Any]) -> Any | None:
        if not self.enabled:
            return None
        path = self._path(operation, params)
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            self.misses += 1
            return None
        if time.time() - payload.get("stored_at", 0) > self.ttl:
            self.misses += 1
            return None
        self.hits += 1
        return payload.get("value")

    def set(self, operation: str, params: dict[str, Any], value: Any) -> None:
        if not self.enabled:
            return
        path = self._path(operation, params)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"stored_at": time.time(), "value": value}, default=str))
            tmp.replace(path)
        except (OSError, TypeError, ValueError):
            pass  # a cache that cannot write is a cache miss, never an error

    def memoize(
        self, operation: str, params: dict[str, Any], producer: Callable[[], Any]
    ) -> tuple[Any, bool]:
        """Return (value, from_cache)."""
        hit = self.get(operation, params)
        if hit is not None:
            return hit, True
        value = producer()
        self.set(operation, params, value)
        return value, False

    def clear(self) -> int:
        if not self.root.exists():
            return 0
        count = sum(1 for _ in self.root.rglob("*.json"))
        shutil.rmtree(self.root, ignore_errors=True)
        return count

    def stats(self) -> dict:
        return {"hits": self.hits, "misses": self.misses, "ttl_seconds": self.ttl,
                "enabled": self.enabled, "path": str(self.root)}

"""
LRU Cache with TTL support.

Thread-safe, generic cache used for embedding vectors and classification results.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Generic, Optional, TypeVar

T = TypeVar("T")


class LRUCache(Generic[T]):
    """
    Least-Recently-Used cache with time-based expiration.

    Parameters:
        max_size: Maximum number of items to store.
        ttl_seconds: Time-to-live for each entry in seconds.
    """

    def __init__(self, max_size: int = 300, ttl_seconds: float = 300.0):
        # Clamp to >= 1 so a degenerate size never makes set() raise on an
        # empty cache (popitem on an empty OrderedDict raises KeyError).
        self._max_size = max(1, max_size)
        self._ttl_seconds = ttl_seconds
        self._cache: OrderedDict[str, tuple[T, float]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[T]:
        """Get a value from cache. Returns None if missing or expired."""
        with self._lock:
            if key not in self._cache:
                return None

            value, timestamp = self._cache[key]

            # Check TTL
            if time.time() - timestamp > self._ttl_seconds:
                del self._cache[key]
                return None

            # Move to end (mark as recently used)
            self._cache.move_to_end(key)
            return value

    def set(self, key: str, value: T) -> None:
        """Set a value in cache."""
        with self._lock:
            # Remove if exists (to update position)
            if key in self._cache:
                del self._cache[key]

            # Evict oldest if at capacity
            while self._cache and len(self._cache) >= self._max_size:
                self._cache.popitem(last=False)

            self._cache[key] = (value, time.time())

    def clear(self) -> None:
        """Clear all cached entries."""
        with self._lock:
            self._cache.clear()

    @property
    def size(self) -> int:
        """Current number of items in cache."""
        return len(self._cache)

    def has(self, key: str) -> bool:
        """Check if a key exists and is not expired."""
        return self.get(key) is not None

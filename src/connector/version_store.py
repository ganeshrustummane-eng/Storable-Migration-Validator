"""
Optimistic Concurrency / Version Store
=======================================
Tracks monotonically-increasing versions for:
  - Validation plans  (key: "plan/<layer>/<table>")
  - Column mappings   (key: "mapping/<table>/<source_col>")
  - Rules             (key: "rule/<rule_id>")
  - Exclusions        (key: "exclusion/<table>/<col>")

Every write operation should:
  1. Call check_and_bump(entity_key, expected_version)
  2. If the current version != expected_version → raise VersionConflictError
  3. Otherwise atomically bump the version and return the new version

Persistence:
  JSON file at output/entity_versions.json — a flat dict of key → version int.
  Reads and writes are serialized by a threading.Lock (no multi-process safety;
  for production upgrade to a database row-level lock or Redis WATCH).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Dict, Optional

_VERSIONS_PATH = Path(__file__).resolve().parents[2] / "output" / "entity_versions.json"
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class VersionConflictError(Exception):
    """
    Optimistic concurrency conflict.

    The entity was modified by another writer between the time the client
    read the version and the time it tried to write.  The client should
    re-read the latest state and retry if the operation is still valid.
    """
    def __init__(
        self,
        entity_key: str,
        expected: int,
        actual: int,
    ):
        self.entity_key = entity_key
        self.expected   = expected
        self.actual     = actual
        super().__init__(
            f"Version conflict on '{entity_key}': "
            f"expected v{expected}, current is v{actual}. "
            f"Re-read the latest state and retry."
        )
        self.code = "VERSION_CONFLICT"
        self.message = str(self)


# ---------------------------------------------------------------------------
# VersionStore
# ---------------------------------------------------------------------------

class VersionStore:
    """Thread-safe entity version tracker with JSON persistence."""

    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path else _VERSIONS_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # -- Persistence ---------------------------------------------------------

    def _load(self) -> Dict[str, int]:
        if not self._path.exists():
            return {}
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
                return {k: int(v) for k, v in data.items()}
        except Exception:
            return {}

    def _save(self, versions: Dict[str, int]) -> None:
        tmp = self._path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(versions, f, indent=2)
            tmp.replace(self._path)
        except Exception as exc:
            raise RuntimeError(f"VersionStore: could not persist versions: {exc}") from exc

    # -- Public API ----------------------------------------------------------

    def current(self, entity_key: str) -> int:
        """Return the current version for an entity (0 if not yet tracked)."""
        with _lock:
            return self._load().get(entity_key, 0)

    def bump(self, entity_key: str) -> int:
        """
        Unconditionally increment the version and return the new version.
        Use this after a successful write when no concurrency check is needed
        (e.g. initial creation).
        """
        with _lock:
            versions = self._load()
            new_v = versions.get(entity_key, 0) + 1
            versions[entity_key] = new_v
            self._save(versions)
            return new_v

    def check_and_bump(self, entity_key: str, expected_version: int) -> int:
        """
        Verify the current version equals expected_version, then bump.

        Returns the new version on success.
        Raises VersionConflictError if current != expected.

        Pass expected_version=0 to indicate "create if not exists" (first write).
        Pass expected_version=-1 to skip the check entirely (unsafe — avoid).
        """
        with _lock:
            versions = self._load()
            current_v = versions.get(entity_key, 0)

            if expected_version != -1 and current_v != expected_version:
                raise VersionConflictError(entity_key, expected_version, current_v)

            new_v = current_v + 1
            versions[entity_key] = new_v
            self._save(versions)
            return new_v

    def initialize(self, entity_key: str, version: int = 1) -> int:
        """
        Set the version for a newly created entity.
        Does nothing if the entity already has a version (idempotent).
        Returns the resulting version.
        """
        with _lock:
            versions = self._load()
            if entity_key not in versions:
                versions[entity_key] = version
                self._save(versions)
                return version
            return versions[entity_key]

    def all_versions(self) -> Dict[str, int]:
        """Return a snapshot of all tracked versions (for debugging)."""
        with _lock:
            return dict(self._load())


# Module-level singleton
version_store = VersionStore()

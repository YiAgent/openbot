"""In-process substitutes for external services.

build_inmemory_redis() yields a fakeredis client compatible with the
redis-py async API. build_inmemory_db() yields an aiosqlite-backed
SQLAlchemy session factory with Base.metadata.create_all already run.
Used by the contract layer's "real" parametrization to exercise real
adapter code without network/docker."""

from __future__ import annotations

__all__: list[str] = []

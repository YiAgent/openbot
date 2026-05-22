"""Cache key derivation for the snapshot cache + the corruption signal.

Two small surfaces colocated because they share the same vocabulary
("cache entry identity" and "cache entry validity"):

  - :func:`_cache_key` — derives the deterministic, content-addressed
    24-char hex key from a :class:`~openbot.domain.checkout.CheckoutSpec`
    and an installation id. Pure function; safe to call from any
    adapter without touching network or filesystem.
  - :class:`CacheCorruptedError` — adapter-level signal that a
    hydrated workspace failed its post-load ``git reset --hard {ref}``
    check. The dispatcher catches this and treats it as a miss; the
    adapter evicts the offending entry.

Per spec ``docs/superpowers/specs/2026-05-21-sandbox-snapshot-cache-design.md``
§ "Cache key derivation" and § "Resolution algorithm" step 6.

Module-private leading underscore on :func:`_cache_key` is intentional:
only adapters in ``infrastructure/sandboxes/cache_*.py`` should derive
keys, and they import via the explicit module path. The
:class:`CacheCorruptedError` is public so the dispatcher can catch it
without reaching into a private symbol.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openbot.domain.checkout import CheckoutSpec


# The credentialed-URL marker that GitHub installation tokens take:
# ``https://x-access-token:<token>@github.com/...``. If a caller hands
# us a URL with this fragment we refuse to key against it — a token in
# the cache key would survive past the token's TTL and leak the
# credential into any log line that ever prints the key.
_TOKEN_URL_MARKER = "x-access-token:"

# 24 hex chars = 96 bits of entropy from SHA-256. Plenty for a single
# installation's cache namespace; short enough to read in logs.
_KEY_HEX_PREFIX_LEN = 24


class CacheCorruptedError(Exception):
    """Raised when a hydrated workspace cannot be reset to its ref.

    Adapter ``_refresh_to_ref`` raises this after ``git reset --hard``
    returns non-zero; the dispatcher catches it at the cache-acquire
    boundary, increments ``sandbox_cache_total{result=backend_error}``,
    and falls through to the cold path. Adapters should also
    asynchronously evict the offending entry so the next acquire
    doesn't hit the same bad snapshot.
    """


def _cache_key(checkout: CheckoutSpec, *, installation_id: int) -> str:
    """Derive the 24-char hex cache key for one snapshot.

    Components, joined by ``\\t`` and hashed with SHA-256:

      1. ``installation_id`` — tenant scope.
      2. ``repo_url`` normalized to lowercase + trailing-slash stripped.
      3. ``ref`` — exact SHA (never a branch name; resolved upstream).
      4. ``strategy.value`` — BLOBLESS vs SHALLOW produce different
         working trees, so they must produce different keys.
      5. ``sparse_paths`` sorted then tab-joined — sparse-checkout is
         set-semantics so the order doesn't matter.

    ``diff_base`` is intentionally NOT a key component: it's review-time
    metadata for the responder and doesn't change the working tree.

    Raises :class:`TypeError` if ``repo_url`` contains an
    ``x-access-token:`` fragment — a defensive boundary preventing a
    GitHub installation token from ever becoming part of a cache key.
    """
    if _TOKEN_URL_MARKER in checkout.repo_url:
        raise TypeError(
            "repo_url contains an x-access-token credential; cache keys "
            "must be derived from the bare clone URL, not the credentialed "
            "form. Strip the token before calling _cache_key."
        )

    normalized_url = checkout.repo_url.rstrip("/").lower()
    sparse_joined = "\t".join(sorted(checkout.sparse_paths))
    components = "\t".join(
        [
            str(installation_id),
            normalized_url,
            checkout.ref,
            checkout.strategy.value,
            sparse_joined,
        ]
    )
    digest = hashlib.sha256(components.encode("utf-8")).hexdigest()
    return digest[:_KEY_HEX_PREFIX_LEN]


__all__ = ["CacheCorruptedError", "_cache_key"]

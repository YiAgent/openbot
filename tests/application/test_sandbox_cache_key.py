"""Cache key derivation — every invariant from the spec, pinned.

The cache key is the addressing surface for the snapshot cache: every
``(installation, repo, ref, strategy, sparse_paths)`` tuple maps to
exactly one snapshot. If the key is wrong, two different working trees
collide (correctness bug) or one tree maps to many keys (cost bug).

Each test pins one invariant:

  - **Determinism** — same inputs → same key, always.
  - **Tenant isolation** — installation_id varies → key varies (no
    cross-tenant snapshot reuse).
  - **URL normalization** — case-insensitive + trailing slash stripped,
    so ``Github.com/X`` and ``github.com/x/`` cache as one entry.
  - **Tree-shape sensitivity** — ``ref`` and ``strategy`` and the SET
    of ``sparse_paths`` all vary the working tree, so they vary the
    key.
  - **Order-independence on sparse_paths** — sparse-checkout is
    set-semantics; ``("a","b")`` and ``("b","a")`` are the same tree.
  - **diff_base is NOT a key component** — it's review-time metadata
    handed to the responder, not a working-tree input.
  - **Output shape** — 24-char lowercase hex prefix of SHA-256.
  - **Security** — token-shaped ``repo_url`` rejected at the boundary
    so an installation token can never become part of a cache key.

Per Task 1.2 of ``docs/superpowers/plans/2026-05-21-sandbox-snapshot-cache-plan.md``
and § "Cache key derivation" of the predecessor spec.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from openbot.application.sandbox_cache_key import CacheCorruptedError, _cache_key
from openbot.domain.checkout import CheckoutSpec, CloneStrategy


def _spec(**overrides: Any) -> CheckoutSpec:
    """Build a CheckoutSpec with sensible defaults; override one field per test."""
    defaults: dict[str, Any] = {
        "repo_url": "https://github.com/foo/bar.git",
        "ref": "abc123def456abc123def456abc123def456abcd",
        "strategy": CloneStrategy.SHALLOW,
        "diff_base": None,
        "sparse_paths": (),
    }
    defaults.update(overrides)
    return CheckoutSpec(**defaults)


def test_key_is_deterministic() -> None:
    k1 = _cache_key(_spec(), installation_id=42)
    k2 = _cache_key(_spec(), installation_id=42)
    assert k1 == k2


def test_key_varies_on_installation_id() -> None:
    k1 = _cache_key(_spec(), installation_id=42)
    k2 = _cache_key(_spec(), installation_id=43)
    assert k1 != k2


def test_key_varies_on_repo_url_case_is_same_key() -> None:
    # Normalization: lowercase. Same repo under different casing is the
    # same cache entry — Git is case-insensitive on the host part of
    # the URL, and we don't want two snapshots for "Github.com" vs
    # "github.com".
    k1 = _cache_key(_spec(repo_url="https://Github.com/Foo/Bar.git"), installation_id=42)
    k2 = _cache_key(_spec(repo_url="https://github.com/foo/bar.git"), installation_id=42)
    assert k1 == k2


def test_key_strips_trailing_slash() -> None:
    k1 = _cache_key(_spec(repo_url="https://github.com/foo/bar.git"), installation_id=42)
    k2 = _cache_key(_spec(repo_url="https://github.com/foo/bar.git/"), installation_id=42)
    assert k1 == k2


def test_key_varies_on_ref() -> None:
    k1 = _cache_key(_spec(ref="a" * 40), installation_id=42)
    k2 = _cache_key(_spec(ref="b" * 40), installation_id=42)
    assert k1 != k2


def test_key_varies_on_strategy() -> None:
    k1 = _cache_key(_spec(strategy=CloneStrategy.SHALLOW), installation_id=42)
    k2 = _cache_key(_spec(strategy=CloneStrategy.BLOBLESS), installation_id=42)
    assert k1 != k2


def test_key_is_order_stable_on_sparse_paths() -> None:
    # Sparse-checkout is set-semantics: ("a","b") and ("b","a") produce
    # the same working tree, so they MUST produce the same cache key.
    k1 = _cache_key(_spec(sparse_paths=("a", "b")), installation_id=42)
    k2 = _cache_key(_spec(sparse_paths=("b", "a")), installation_id=42)
    assert k1 == k2


def test_key_varies_on_sparse_paths_membership() -> None:
    # Different SET membership → different tree → different key.
    k1 = _cache_key(_spec(sparse_paths=("a",)), installation_id=42)
    k2 = _cache_key(_spec(sparse_paths=("a", "b")), installation_id=42)
    assert k1 != k2


def test_key_ignores_diff_base() -> None:
    # diff_base is review-time metadata for the responder; it doesn't
    # change the working tree. Two specs differing only in diff_base
    # MUST cache as the same entry.
    k1 = _cache_key(_spec(diff_base=None), installation_id=42)
    k2 = _cache_key(_spec(diff_base="deadbeef"), installation_id=42)
    assert k1 == k2


def test_key_length_is_24_hex_chars() -> None:
    key = _cache_key(_spec(), installation_id=42)
    assert re.fullmatch(r"[0-9a-f]{24}", key) is not None


def test_key_rejects_token_shaped_repo_url() -> None:
    # An installation token must never reach the cache key. If a caller
    # passes a URL that already carries an x-access-token credential,
    # raise — never silently key against the credentialed string.
    spec = _spec(repo_url="https://x-access-token:ghp_AAAA@github.com/foo/bar.git")
    with pytest.raises(TypeError, match="token"):
        _cache_key(spec, installation_id=42)


def test_cache_corrupted_error_is_exception() -> None:
    # CacheCorruptedError lives in this module because it shares the
    # cache vocabulary; verify it's a real Exception subclass adapters
    # can raise and the dispatcher can catch.
    assert issubclass(CacheCorruptedError, Exception)

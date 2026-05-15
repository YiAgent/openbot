"""Setup wizard — focused unit tests for the safe-to-test pieces.

The interactive parts (HTTPServer loop, webbrowser.open) aren't exercised
here; we test the deterministic helpers: manifest shape, env merge,
PEM persistence, smee minting (mocked), config resolution.
"""

from __future__ import annotations

import json
import os
import stat as stat_mod
from pathlib import Path
from typing import Any

import httpx
import pytest

from openbot import setup_wizard

# ───── _build_manifest ─────


def _config(tmp_path: Path, **overrides: Any) -> setup_wizard.WizardConfig:
    base: dict[str, Any] = {
        "owner": "YiAgent",
        "is_org": True,
        "app_name": "OpenBot Dev",
        "homepage_url": "https://github.com/YiAgent",
        "webhook_proxy_url": "https://smee.io/abc",
        "callback_port": 9182,
        "state": "state-token",
        "env_file": tmp_path / ".env",
        "pem_path": tmp_path / "secrets" / "key.pem",
    }
    base.update(overrides)
    return setup_wizard.WizardConfig(**base)


def test_manifest_contains_locked_permissions_and_events(tmp_path: Path) -> None:
    m = setup_wizard._build_manifest(_config(tmp_path))

    assert m["default_permissions"] == {
        "contents": "write",
        "issues": "write",
        "pull_requests": "write",
        "metadata": "read",
    }
    assert set(m["default_events"]) == {
        "issues",
        "issue_comment",
        "pull_request",
        "pull_request_review_comment",
    }


def test_manifest_redirect_url_includes_state(tmp_path: Path) -> None:
    m = setup_wizard._build_manifest(_config(tmp_path))
    assert "state=state-token" in m["redirect_url"]
    assert m["redirect_url"].startswith("http://127.0.0.1:9182/callback")


def test_manifest_hook_attributes_use_smee_proxy(tmp_path: Path) -> None:
    m = setup_wizard._build_manifest(_config(tmp_path))
    assert m["hook_attributes"] == {"url": "https://smee.io/abc", "active": True}


def test_manifest_is_json_serializable(tmp_path: Path) -> None:
    # The form page embeds it as a hidden field — must round-trip cleanly.
    m = setup_wizard._build_manifest(_config(tmp_path))
    assert json.loads(json.dumps(m)) == m


# ───── _new_app_url ─────


def test_org_new_app_url(tmp_path: Path) -> None:
    cfg = _config(tmp_path, is_org=True, owner="YiAgent")
    assert setup_wizard._new_app_url(cfg).startswith(
        "https://github.com/organizations/YiAgent/settings/apps/new"
    )


def test_user_new_app_url(tmp_path: Path) -> None:
    cfg = _config(tmp_path, is_org=False, owner="someone")
    # User-scope endpoint ignores `owner` — GitHub uses the authed user.
    assert setup_wizard._new_app_url(cfg) == "https://github.com/settings/apps/new"


# ───── _mint_smee_channel ─────


def test_mint_smee_channel_follows_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith("https://smee.io/new")
        return httpx.Response(307, headers={"location": "https://smee.io/AbCdEf12345"})

    transport = httpx.MockTransport(handler)

    def patched_get(url, **kw):
        with httpx.Client(transport=transport) as c:
            return c.get(url, **kw)

    monkeypatch.setattr(setup_wizard.httpx, "get", patched_get)
    url = setup_wizard._mint_smee_channel()
    assert url == "https://smee.io/AbCdEf12345"


def test_mint_smee_channel_raises_on_unexpected_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        setup_wizard.httpx,
        "get",
        lambda url, **kw: httpx.Client(transport=transport).get(url, **kw),
    )
    with pytest.raises(setup_wizard.WizardError, match="did not redirect"):
        setup_wizard._mint_smee_channel()


# ───── _free_port ─────


def test_free_port_returns_preferred_when_available() -> None:
    # Ports 41xxx are rarely held by anything; even if 41000 is busy,
    # the search window of 20 should find a free one.
    port = setup_wizard._free_port(41000)
    assert 41000 <= port < 41020


# ───── _existing_env ─────


def test_existing_env_finds_key(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("FOO=1\nOPENBOT_GITHUB_WEBHOOK_PROXY_URL=https://smee.io/X\n")
    assert (
        setup_wizard._existing_env(env, "OPENBOT_GITHUB_WEBHOOK_PROXY_URL") == "https://smee.io/X"
    )


def test_existing_env_returns_none_when_absent(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("FOO=1\n")
    assert setup_wizard._existing_env(env, "OPENBOT_GITHUB_WEBHOOK_PROXY_URL") is None


def test_existing_env_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert setup_wizard._existing_env(tmp_path / "nope.env", "ANYTHING") is None


# ───── _merge_env ─────


def _creds(pem_body: str = "PEM-BODY-PLACEHOLDER-FOR-TESTS") -> setup_wizard.AppCredentials:
    return setup_wizard.AppCredentials(
        id=42,
        slug="openbot-dev",
        name="OpenBot Dev",
        owner_login="YiAgent",
        html_url="https://github.com/apps/openbot-dev",
        pem=pem_body,
        webhook_secret="hex-secret",
        client_id="Iv23.client",
        client_secret="cs-secret",
        install_url="https://github.com/apps/openbot-dev/installations/new",
    )


def test_merge_env_creates_file_when_missing(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    pem = tmp_path / "secrets" / "k.pem"
    setup_wizard._merge_env(env, "https://smee.io/Q", _creds(), pem)
    txt = env.read_text()
    assert "OPENBOT_GITHUB_APP_ID=42" in txt
    assert "OPENBOT_GITHUB_WEBHOOK_SECRET=hex-secret" in txt
    assert "OPENBOT_GITHUB_WEBHOOK_PROXY_URL=https://smee.io/Q" in txt
    assert f"OPENBOT_GITHUB_APP_PRIVATE_KEY_PATH=./{pem}" in txt
    # New files get the wizard header.
    assert "DO NOT COMMIT" in txt


def test_merge_env_updates_existing_key_in_place(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# header\nOPENBOT_GITHUB_APP_ID=999\nOTHER=stay\n")
    setup_wizard._merge_env(env, "https://smee.io/Q", _creds(), tmp_path / "x.pem")
    lines = env.read_text().splitlines()
    # Updated in place, not duplicated.
    assert lines.count("OPENBOT_GITHUB_APP_ID=42") == 1
    assert "OPENBOT_GITHUB_APP_ID=999" not in env.read_text()
    # Existing unrelated lines are preserved.
    assert "OTHER=stay" in env.read_text()
    assert "# header" in env.read_text()


def test_merge_env_appends_missing_keys_without_clobbering(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("EXISTING=value\n")
    setup_wizard._merge_env(env, "https://smee.io/Q", _creds(), tmp_path / "x.pem")
    txt = env.read_text()
    assert "EXISTING=value" in txt
    assert "OPENBOT_GITHUB_APP_ID=42" in txt
    # Don't add the wizard header to an already-existing file.
    assert "DO NOT COMMIT" not in txt


def test_merge_env_sets_owner_only_permission(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    setup_wizard._merge_env(env, "https://smee.io/Q", _creds(), tmp_path / "x.pem")
    mode = stat_mod.S_IMODE(env.stat().st_mode)
    assert mode == stat_mod.S_IRUSR | stat_mod.S_IWUSR


# ───── _write_pem ─────


def test_write_pem_creates_parent_and_locks_perms(tmp_path: Path) -> None:
    pem_path = tmp_path / "secrets" / "k.pem"
    setup_wizard._write_pem(pem_path, "PEM-BODY")
    assert pem_path.read_text() == "PEM-BODY"
    # File is owner read/write only.
    file_mode = stat_mod.S_IMODE(pem_path.stat().st_mode)
    assert file_mode == stat_mod.S_IRUSR | stat_mod.S_IWUSR
    # Parent dir is owner-only when we got to chmod it (some filesystems
    # may not allow it; we don't fail there).
    dir_mode = stat_mod.S_IMODE(pem_path.parent.stat().st_mode)
    if os.name == "posix":
        assert dir_mode == stat_mod.S_IRWXU


# ───── _exchange_manifest_code (async) ─────


async def test_exchange_manifest_code_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_resp = {
        "id": 1234,
        "slug": "openbot-dev",
        "name": "OpenBot Dev",
        "owner": {"login": "YiAgent"},
        "html_url": "https://github.com/apps/openbot-dev",
        "pem": "-----BEGIN-----\n...\n-----END-----\n",
        "webhook_secret": "hex",
        "client_id": "Iv23.c",
        "client_secret": "cs",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/app-manifests/CODE-XYZ/conversions")
        return httpx.Response(201, json=fake_resp)

    transport = httpx.MockTransport(handler)
    # Patch AsyncClient construction inside setup_wizard to use our transport.
    original = setup_wizard.httpx.AsyncClient

    def patched(**kwargs):
        kwargs.pop("timeout", None)
        return original(transport=transport)

    monkeypatch.setattr(setup_wizard.httpx, "AsyncClient", patched)
    creds = await setup_wizard._exchange_manifest_code("CODE-XYZ")
    assert creds.id == 1234
    assert creds.slug == "openbot-dev"
    assert creds.install_url == "https://github.com/apps/openbot-dev/installations/new"


async def test_exchange_manifest_code_raises_on_4xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    transport = httpx.MockTransport(handler)
    original = setup_wizard.httpx.AsyncClient

    def patched(**kwargs):
        kwargs.pop("timeout", None)
        return original(transport=transport)

    monkeypatch.setattr(setup_wizard.httpx, "AsyncClient", patched)
    with pytest.raises(setup_wizard.WizardError, match="manifest exchange failed"):
        await setup_wizard._exchange_manifest_code("BAD-CODE")

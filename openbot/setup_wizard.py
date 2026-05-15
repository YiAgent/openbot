"""Interactive setup wizard — bootstrap OpenBot's GitHub App via the manifest flow.

PRD §7 calls for a 30-minute first-run experience. This wizard collapses it to
~30 seconds + two browser clicks, by using GitHub's
[App Manifest flow](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest).

Run:
    uv run python -m openbot.setup_wizard           # interactive
    uv run python -m openbot.setup_wizard --org YiAgent --noninteractive

Flow:
    1. Resolve smee channel (reuse existing OPENBOT_GITHUB_WEBHOOK_PROXY_URL,
       or POST https://smee.io/new to mint one).
    2. Generate a manifest JSON with PRD §4 permissions + events baked in.
    3. Start a local HTTP server on 127.0.0.1:<port> with two endpoints:
         GET /             auto-submits an HTML form POSTing the manifest to
                           GitHub (App-Manifest is form-POST only)
         GET /callback     receives ?code=<temp> from GitHub's redirect
    4. Open the user's browser to GET / so the form auto-submits.
    5. After the user clicks "Create GitHub App", GitHub redirects to /callback.
    6. Exchange the code at /app-manifests/<code>/conversions for App credentials.
    7. Write the PEM (chmod 0600) and merge id/secret/key path into `.env`.
    8. Print the install URL — user opens it and clicks "Install".

Security notes:
  - The PEM is rendered chmod 0600 immediately.
  - The webhook secret is never echoed; .env file is written chmod 0600.
  - The local server binds only to 127.0.0.1.
  - Manifest's `redirect_url` includes a random `state` value; the callback
    refuses any request missing the matching state (CSRF protection).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import html
import json
import logging
import os
import secrets as secrets_mod
import socket
import stat
import sys
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Final

import httpx

_logger = logging.getLogger("openbot.setup_wizard")

# PRD §4 — locked permissions and events. Users edit AFTER the App is created
# via the GitHub UI; the wizard never offers a custom subset.
_DEFAULT_PERMISSIONS: Final[dict[str, str]] = {
    "contents": "write",
    "issues": "write",
    "pull_requests": "write",
    "metadata": "read",
}
_DEFAULT_EVENTS: Final[list[str]] = [
    "issues",
    "issue_comment",
    "pull_request",
    "pull_request_review_comment",
]

# Manifest-flow redirect/exchange constants.
_CALLBACK_PORT_DEFAULT: Final = 9_182
_CALLBACK_PATH: Final = "/callback"
_GITHUB_API: Final = "https://api.github.com"
_SMEE_NEW: Final = "https://smee.io/new"
_USER_AGENT: Final = "OpenBot-setup-wizard"

_HTML_FORM = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>OpenBot setup — creating GitHub App</title>
  <style>
    body {{ font: 16px/1.6 system-ui, sans-serif; max-width: 40rem;
            margin: 4rem auto; padding: 0 1.5rem; color: #222; }}
    code {{ background: #f4f4f5; padding: 0.1em 0.4em; border-radius: 4px; }}
  </style>
</head>
<body>
  <h1>Redirecting to GitHub…</h1>
  <p>You'll see a page titled "Create GitHub App" with all the permissions
     and events pre-filled. Click <b>Create GitHub App</b> to continue.</p>
  <p>If nothing happens automatically, click the button below.</p>
  <form action="{action}" method="post">
    <input type="hidden" name="manifest" value="{manifest}">
    <button type="submit" style="font-size: 1.1rem; padding: 0.6rem 1.2rem;
            background: #0969da; color: white; border: 0; border-radius: 6px;">
      Continue to GitHub →
    </button>
  </form>
  <script>setTimeout(() => document.forms[0].submit(), 250);</script>
</body>
</html>"""


_HTML_DONE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>OpenBot setup — done</title></head>
<body style="font: 16px/1.6 system-ui, sans-serif; max-width: 40rem;
             margin: 4rem auto; padding: 0 1.5rem;">
  <h1>{title}</h1>
  <p>{body}</p>
  <p>You can close this window and return to the terminal.</p>
</body>
</html>"""


@dataclass(frozen=True, slots=True)
class WizardConfig:
    """Resolved inputs before launching the manifest flow."""

    owner: str  # GitHub org or user login (App-owner scope)
    is_org: bool  # True → /organizations/{owner}/settings/apps/new
    app_name: str  # "OpenBot Dev" etc.
    homepage_url: str  # https://github.com/<owner>/openbot
    webhook_proxy_url: str  # smee channel URL
    callback_port: int
    state: str  # random CSRF token in redirect_url query
    env_file: Path
    pem_path: Path
    public: bool = False


@dataclass(frozen=True, slots=True)
class AppCredentials:
    """Subset of /app-manifests/<code>/conversions that we persist."""

    id: int
    slug: str
    name: str
    owner_login: str
    html_url: str
    pem: str  # PEM file contents
    webhook_secret: str
    client_id: str
    client_secret: str
    install_url: str  # built locally from slug


# ─────────────────────────────────────────────────────────────────────────
# Public entry point


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openbot.setup_wizard")
    parser.add_argument("--owner", help="GitHub org or user. Default: prompt or 'YiAgent'.")
    parser.add_argument(
        "--user",
        action="store_true",
        help="Create the App under a user account (default: organization).",
    )
    parser.add_argument(
        "--name", default="OpenBot Dev", help="App name on GitHub. Default: 'OpenBot Dev'."
    )
    parser.add_argument(
        "--smee-url",
        help="Existing smee.io channel. Default: reuse .env or mint via https://smee.io/new.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_CALLBACK_PORT_DEFAULT,
        help="Local callback port. Auto-bumps if in use.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Path to write/update credentials.",
    )
    parser.add_argument(
        "--pem-path",
        type=Path,
        default=Path("secrets/openbot-github-app.pem"),
        help="Where to save the App's private key.",
    )
    parser.add_argument(
        "--noninteractive",
        action="store_true",
        help="Fail on missing inputs instead of prompting.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        cfg = _resolve_config(args)
    except WizardError as exc:
        print(f"\n✘ {exc}", file=sys.stderr)
        return 2

    creds = asyncio.run(_run_manifest_flow(cfg))
    _persist(cfg, creds)
    _print_summary(cfg, creds)
    return 0


# ─────────────────────────────────────────────────────────────────────────
# Step 1 — config resolution


class WizardError(Exception):
    """User-facing setup failure."""


def _resolve_config(args: argparse.Namespace) -> WizardConfig:
    owner = args.owner or _prompt(
        "GitHub org or user that will own the App",
        default="YiAgent",
        noninteractive=args.noninteractive,
    )
    is_org = not args.user
    smee = args.smee_url or _existing_env(args.env_file, "OPENBOT_GITHUB_WEBHOOK_PROXY_URL")
    if not smee:
        smee = _mint_smee_channel()
        print(f"▸ minted smee channel: {smee}")
    port = _free_port(args.port)
    state = secrets_mod.token_urlsafe(24)

    return WizardConfig(
        owner=owner,
        is_org=is_org,
        app_name=args.name,
        homepage_url=f"https://github.com/{owner}",
        webhook_proxy_url=smee,
        callback_port=port,
        state=state,
        env_file=args.env_file,
        pem_path=args.pem_path,
    )


def _prompt(label: str, *, default: str, noninteractive: bool) -> str:
    if noninteractive:
        return default
    raw = input(f"{label} [{default}]: ").strip()
    return raw or default


def _existing_env(env_file: Path, key: str) -> str | None:
    if not env_file.exists():
        return None
    for line in env_file.read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return None


def _mint_smee_channel() -> str:
    response = httpx.get(_SMEE_NEW, follow_redirects=False, timeout=10)
    if response.status_code not in (301, 302, 303, 307, 308):
        raise WizardError(f"smee.io did not redirect to a new channel (got {response.status_code})")
    location = response.headers.get("location")
    if not location:
        raise WizardError("smee.io redirect missing Location header")
    return location


def _free_port(preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise WizardError(f"no free port in [{preferred}, {preferred + 20})")


# ─────────────────────────────────────────────────────────────────────────
# Step 2 — manifest


def _build_manifest(cfg: WizardConfig) -> dict[str, Any]:
    redirect_url = f"http://127.0.0.1:{cfg.callback_port}{_CALLBACK_PATH}?state={cfg.state}"
    return {
        "name": cfg.app_name,
        "url": cfg.homepage_url,
        "hook_attributes": {"url": cfg.webhook_proxy_url, "active": True},
        "redirect_url": redirect_url,
        "callback_urls": [],  # no OAuth — App acts as itself
        "public": cfg.public,
        "request_oauth_on_install": False,
        "setup_on_update": False,
        "default_permissions": dict(_DEFAULT_PERMISSIONS),
        "default_events": list(_DEFAULT_EVENTS),
    }


def _new_app_url(cfg: WizardConfig) -> str:
    return (
        f"https://github.com/organizations/{cfg.owner}/settings/apps/new"
        if cfg.is_org
        else "https://github.com/settings/apps/new"
    )


# ─────────────────────────────────────────────────────────────────────────
# Step 3-6 — local server + manifest exchange


async def _run_manifest_flow(cfg: WizardConfig) -> AppCredentials:
    manifest = _build_manifest(cfg)
    code = await _wait_for_callback_code(cfg, manifest)
    return await _exchange_manifest_code(code)


async def _wait_for_callback_code(cfg: WizardConfig, manifest: dict[str, Any]) -> str:
    """Spin up a localhost server; return the `code` GitHub posts back."""

    captured: dict[str, str] = {}
    done = threading.Event()
    handler = _make_handler(cfg, manifest, captured, done)

    server = HTTPServer(("127.0.0.1", cfg.callback_port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{cfg.callback_port}/"
        print(f"\n▸ opening browser to {url}")
        print('  click "Create GitHub App" on the GitHub page that follows.\n')
        webbrowser.open(url)

        # 10-minute window matches GitHub App Manifest `code` TTL.
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, done.wait, 600)
        if not ok:
            raise WizardError("timed out waiting for GitHub callback (10 min)")
        if "error" in captured:
            raise WizardError(f"callback returned error: {captured['error']}")
        return captured["code"]
    finally:
        server.shutdown()
        server.server_close()


def _make_handler(
    cfg: WizardConfig,
    manifest: dict[str, Any],
    captured: dict[str, str],
    done: threading.Event,
) -> type[BaseHTTPRequestHandler]:
    """Build a request handler closure with cfg/manifest/captured bound in."""

    new_app_url = _new_app_url(cfg)
    manifest_json = json.dumps(manifest, separators=(",", ":"))
    # HTML-escape so a malicious owner string can't inject markup.
    form_page = _HTML_FORM.format(
        action=html.escape(new_app_url, quote=True),
        manifest=html.escape(manifest_json, quote=True),
    )

    class Handler(BaseHTTPRequestHandler):
        # Silence default per-request console noise; our prints are tidier.
        def log_message(self, format: str, *args: Any) -> None:
            _logger.debug("setup-wizard http: " + format, *args)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)

            if parsed.path in ("/", ""):
                self._respond_html(200, form_page)
                return

            if parsed.path == _CALLBACK_PATH:
                if qs.get("state", [""])[0] != cfg.state:
                    self._respond_html(
                        400,
                        _HTML_DONE.format(
                            title="State mismatch",
                            body=(
                                "The callback state didn't match — possible CSRF. "
                                "Re-run <code>make setup</code>."
                            ),
                        ),
                    )
                    captured["error"] = "state mismatch"
                    done.set()
                    return
                code = qs.get("code", [""])[0]
                if not code:
                    self._respond_html(
                        400,
                        _HTML_DONE.format(
                            title="Missing code",
                            body="GitHub didn't return a manifest code.",
                        ),
                    )
                    captured["error"] = "missing code"
                    done.set()
                    return
                captured["code"] = code
                self._respond_html(
                    200,
                    _HTML_DONE.format(
                        title="✓ App created",
                        body="Setup wizard is finishing in your terminal.",
                    ),
                )
                done.set()
                return

            self._respond_html(404, _HTML_DONE.format(title="404", body="not found"))

        def _respond_html(self, status: int, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

    return Handler


async def _exchange_manifest_code(code: str) -> AppCredentials:
    url = f"{_GITHUB_API}/app-manifests/{code}/conversions"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": _USER_AGENT,
    }
    async with httpx.AsyncClient(timeout=15) as http:
        response = await http.post(url, headers=headers)
    if response.status_code >= 400:
        raise WizardError(f"manifest exchange failed: {response.status_code} {response.text[:200]}")
    data = response.json()
    return AppCredentials(
        id=int(data["id"]),
        slug=str(data["slug"]),
        name=str(data["name"]),
        owner_login=str(data["owner"]["login"]),
        html_url=str(data["html_url"]),
        pem=str(data["pem"]),
        webhook_secret=str(data["webhook_secret"]),
        client_id=str(data["client_id"]),
        client_secret=str(data["client_secret"]),
        install_url=f"https://github.com/apps/{data['slug']}/installations/new",
    )


# ─────────────────────────────────────────────────────────────────────────
# Step 7 — persist


def _persist(cfg: WizardConfig, creds: AppCredentials) -> None:
    _write_pem(cfg.pem_path, creds.pem)
    _merge_env(cfg.env_file, cfg.webhook_proxy_url, creds, cfg.pem_path)


def _write_pem(path: Path, pem: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Tighten parent dir perms — `secrets/` is owner-only (rwx------),
    # so on a shared host another account can't list this directory or
    # read the PEM via traversal. Stdlib `stat` constants used so the
    # value is named, not a magic octal literal.
    with contextlib.suppress(OSError):
        os.chmod(path.parent, stat.S_IRWXU)
    path.write_text(pem, encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _merge_env(env_file: Path, smee_url: str, creds: AppCredentials, pem_path: Path) -> None:
    """Merge OpenBot keys into .env, preserving every other line untouched.

    If a key already exists with a different value, we *update in place* rather
    than appending a duplicate — duplicate lines would lead to confusion the
    next time someone reads the file.
    """
    updates: dict[str, str] = {
        "OPENBOT_GITHUB_APP_ID": str(creds.id),
        "OPENBOT_GITHUB_APP_CLIENT_ID": creds.client_id,
        "OPENBOT_GITHUB_APP_PRIVATE_KEY_PATH": f"./{pem_path}",
        "OPENBOT_GITHUB_WEBHOOK_SECRET": creds.webhook_secret,
        "OPENBOT_GITHUB_WEBHOOK_PROXY_URL": smee_url,
    }

    existing = env_file.read_text() if env_file.exists() else ""
    lines = existing.splitlines() if existing else []
    seen: set[str] = set()
    for i, line in enumerate(lines):
        for key, value in updates.items():
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={value}"
                seen.add(key)
                break

    appends = [f"{key}={value}" for key, value in updates.items() if key not in seen]
    if appends:
        if not existing:
            lines = [
                "# OpenBot .env — DO NOT COMMIT (gitignored)",
                "# Generated by openbot.setup_wizard.",
                "",
            ]
        lines.extend(appends)

    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(env_file, stat.S_IRUSR | stat.S_IWUSR)


# ─────────────────────────────────────────────────────────────────────────
# Step 8 — summary


def _print_summary(cfg: WizardConfig, creds: AppCredentials) -> None:
    print()
    print(f"✓ created App #{creds.id} (slug: {creds.slug}) on {creds.owner_login}")
    print(f"✓ PEM saved: {cfg.pem_path}  (mode 0600)")
    print(f"✓ .env updated: {cfg.env_file}  (mode 0600)")
    print(f"✓ smee channel: {cfg.webhook_proxy_url}")
    print()
    print("Next steps:")
    print(f"  1. Install on a repo:   {creds.install_url}")
    print("  2. Start the dev loop:  make dev")
    print()


if __name__ == "__main__":
    sys.exit(main())

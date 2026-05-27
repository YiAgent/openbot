"""Architecture boundary: use cases never reach the live ``GitHubAdapter``.

The egress safety story rests on every emit-site flowing through
``EgressScannedAdapter``. If a use case imports ``GitHubAdapter`` directly,
it bypasses the decorator and the scanner can be silently sidestepped.
This test fails fast on a regression.
"""

from __future__ import annotations

import pathlib
import re

import pytest

# Anchor to the repo root via this test file's location — the autouse
# ``_isolate_openbot_env`` fixture chdirs to a tmp dir before every test,
# so relative paths cannot be used here.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_USE_CASE_DIR = _REPO_ROOT / "openbot" / "application" / "use_cases"
_FORBIDDEN = re.compile(r"from\s+openbot\.infrastructure\.adapters\.github\s+import")


@pytest.mark.unit
def test_use_cases_do_not_import_raw_github_adapter() -> None:
    offenders: list[str] = []
    for path in _USE_CASE_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if _FORBIDDEN.search(text):
            offenders.append(str(path.relative_to(_REPO_ROOT)))
    assert not offenders, (
        f"Use cases must depend on ChannelAdapterPort, not GitHubAdapter; offenders: {offenders}"
    )


def test_egress_scanned_adapter_is_used_at_composition_root() -> None:
    api_app = (_REPO_ROOT / "openbot" / "entrypoints" / "api" / "app.py").read_text(
        encoding="utf-8"
    )
    worker_main = (_REPO_ROOT / "openbot" / "entrypoints" / "worker" / "__main__.py").read_text(
        encoding="utf-8"
    )
    assert "EgressScannedAdapter(" in api_app, (
        "webapp must wrap GitHubAdapter in EgressScannedAdapter"
    )
    assert "EgressScannedAdapter(" in worker_main, (
        "worker must wrap GitHubAdapter in EgressScannedAdapter"
    )

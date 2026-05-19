"""Boot smoke — setup_wizard module loads and exposes its entry point."""

from __future__ import annotations

import importlib


def test_setup_wizard_module_loads() -> None:
    """Module loads cleanly and exposes a callable entry point.

    Accepts either ``main`` (Typer/plain) or ``cli`` (Click group) because
    different CLI scaffoldings use different conventionally-named callables.
    """
    mod = importlib.import_module("openbot.entrypoints.cli.setup_wizard")
    entry = getattr(mod, "main", None) or getattr(mod, "cli", None)
    assert callable(entry), "setup_wizard must expose `main` or `cli`"

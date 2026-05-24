"""SandboxedHandle — the dispatcher's gift to workflow handlers.

Frozen value object that bundles the three things every handler needs
when sandbox provisioning succeeded: the live sandbox, the resolved
checkout, and the short-lived GitHub installation token (for fix's
push step).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from openbot.application.ports.sandbox import SandboxPort
from openbot.application.sandbox_handle import SandboxedHandle
from openbot.domain.checkout import CheckoutSpec, CloneStrategy


class _FakeSandbox:
    """Minimal stand-in — SandboxedHandle only stores the reference."""

    workspace = "/tmp/sandbox"


def _handle(**overrides: object) -> SandboxedHandle:
    defaults: dict[str, object] = {
        "sandbox": cast(SandboxPort, _FakeSandbox()),
        "checkout": CheckoutSpec(
            repo_url="https://github.com/o/r.git",
            ref="deadbeef" * 5,
            strategy=CloneStrategy.SHALLOW,
        ),
        "token": "ghs_FAKE",
    }
    defaults.update(overrides)
    return SandboxedHandle(**defaults)  # type: ignore[arg-type]


def test_handle_holds_sandbox_checkout_and_token() -> None:
    h = _handle()
    assert h.sandbox.workspace == "/tmp/sandbox"
    assert h.checkout.ref == "deadbeef" * 5
    assert h.token == "ghs_FAKE"


def test_handle_is_frozen() -> None:
    h = _handle()
    with pytest.raises(FrozenInstanceError):
        h.token = "other"  # type: ignore[misc]


def test_handle_equality_with_same_components() -> None:
    sandbox = cast(SandboxPort, _FakeSandbox())
    checkout = CheckoutSpec(repo_url="https://github.com/o/r.git", ref="a" * 40)
    a = SandboxedHandle(sandbox=sandbox, checkout=checkout, token="t")
    b = SandboxedHandle(sandbox=sandbox, checkout=checkout, token="t")
    # Note: equality is value-based; two different _FakeSandbox instances
    # would NOT compare equal because Python's default dataclass equality
    # delegates to the field's __eq__ — the test asserts the contract for
    # the shared-reference case which is what the dispatcher constructs.
    assert a == b

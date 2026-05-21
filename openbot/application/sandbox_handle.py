"""SandboxedHandle — handler-facing bundle of (sandbox, checkout, token).

The dispatcher constructs one of these after preflight passes and clone
succeeds, then passes it into the workflow handler via
``PreflightContext.sandbox_handle``. Handlers READ from this; they do
not construct one themselves.

Why a separate type (rather than three keyword arguments on the
handler):

  - The unified entry surface means every handler signature is
    ``async def handler(ctx: PreflightContext) -> None``. Bundling the
    three correlated values keeps that signature stable.
  - ``token`` is the short-lived GitHub installation access token —
    needed by FIX for ``commit_and_push`` but irrelevant to read-only
    workflows. Keeping it on a single object lets read-only responders
    simply ignore the field rather than thread None through their
    signature.
  - When the sandbox is unavailable (degrade path), the field is set to
    ``None`` on the context rather than passing a sentinel handle —
    handlers branch on ``ctx.sandbox_handle is None``.
"""

from __future__ import annotations

from dataclasses import dataclass

from openbot.application.ports.sandbox import SandboxPort
from openbot.domain.checkout import CheckoutSpec


@dataclass(frozen=True, slots=True)
class SandboxedHandle:
    """The triple every workflow handler needs when the sandbox is live.

    ``sandbox`` is the open ``SandboxPort`` — already cloned at
    ``checkout.ref``. ``checkout`` carries the resolved repo / ref /
    strategy / optional diff_base. ``token`` is the short-lived GitHub
    installation token; only fix's push step needs it, but it's
    bundled here so the handler signature stays uniform across
    workflows.
    """

    sandbox: SandboxPort
    checkout: CheckoutSpec
    token: str


__all__ = ["SandboxedHandle"]

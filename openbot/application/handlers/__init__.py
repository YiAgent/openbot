"""Handlers — terminal workflow callables invoked after middleware passes.

The Router resolves a (Feature, handler) pair from each
classified event; the handler is then invoked by ``dispatch.run_dispatch``
once the middleware chain returns PROCEED.

This package currently exposes only ``debug_echo`` — the placeholder
handler used while the state-machine slice is being verified end-to-end
on the live worker. The real workflow handlers continue to live under
``openbot.workflows.{triage,review,fix,chat}`` and become reachable
again when ``OPENBOT_DEBUG_ECHO=0``.
"""

from openbot.application.handlers.debug_echo import debug_echo_handler

__all__ = ["debug_echo_handler"]

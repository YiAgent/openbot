# openbot/dispatcher/__init__.py
"""openbot.dispatcher — webhook async segment.

Runs D1-D9 preflight, builds TaskSpec v3, and enqueues to Redis Stream.
The worker receives the TaskSpec and calls execute_handler() directly.
"""

from openbot.dispatcher.decide import decide_and_enqueue

__all__ = ["decide_and_enqueue"]

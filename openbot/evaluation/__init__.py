"""openbot.evaluation — production-path eval facade.

Public API:

    from openbot.evaluation import (
        run_review_sample,
        run_fix_sample,
        run_chat_sample,
        run_test_generation_sample,
    )

See ``openbot/evaluation/runner.py`` for implementation details.
"""

from openbot.evaluation.runner import (
    run_chat_sample,
    run_fix_sample,
    run_review_sample,
    run_test_generation_sample,
)

__all__ = [
    "run_chat_sample",
    "run_fix_sample",
    "run_review_sample",
    "run_test_generation_sample",
]

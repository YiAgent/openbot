<<<<<<< HEAD
"""Eval data lifecycle package.

Provides per-suite EvalDataset singletons (REVIEW, CHAT, FIX, SWT) and
the shared EvalDataset ABC.
"""

from __future__ import annotations

from evals.data._base import (
    CollectedExample,
    EvalDataset,
    PublishResult,
    WritebackSummary,
    sha256_examples,
)
from evals.data.chat import ChatDataset
from evals.data.fix import FixDataset
from evals.data.review import ReviewDataset
from evals.data.swt import SwtDataset

REVIEW = ReviewDataset()
CHAT = ChatDataset()
FIX = FixDataset()
SWT = SwtDataset()
DATASETS: dict[str, EvalDataset] = {d.suite: d for d in (REVIEW, CHAT, FIX, SWT)}

__all__ = [
    "CHAT",
    "DATASETS",
    "FIX",
    "REVIEW",
    "SWT",
    "ChatDataset",
    "CollectedExample",
    "EvalDataset",
    "FixDataset",
    "PublishResult",
    "ReviewDataset",
    "SwtDataset",
    "WritebackSummary",
    "sha256_examples",
]
||||||| parent of cea222a (feat(evals/data): scaffold package, copy predictions + samples modules)
=======
"""Eval data lifecycle package.

Provides per-suite EvalDataset singletons (REVIEW, CHAT, FIX, SWT) and
the shared EvalDataset ABC. Singletons are wired in this module; suite
classes live in review.py / chat.py / fix.py / swt.py.
"""
# Singletons wired in Task 10; ABC re-exported in Task 4.
>>>>>>> cea222a (feat(evals/data): scaffold package, copy predictions + samples modules)

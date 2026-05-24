"""Eval data lifecycle package.

Provides per-suite EvalDataset singletons (REVIEW, CHAT, FIX, SWT) and
the shared EvalDataset ABC. Singletons are wired in this module; suite
classes live in review.py / chat.py / fix.py / swt.py.
"""
# Singletons wired in Task 10; ABC re-exported in Task 4.

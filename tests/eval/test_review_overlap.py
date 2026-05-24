"""Unit tests for evals.scorers.review_overlap — PRD §4.1 / §6.1 / §10.3."""

from __future__ import annotations

from evals.scorers.review_overlap import (
    Finding,
    JudgeVerdict,
    compute_review_overlap,
)


def _f(file: str, line: int | None, body: str, severity: str = "medium") -> Finding:
    return {"file": file, "line": line, "body": body, "severity": severity}


def _verdict(match: bool, conf: float = 0.9, why: str = "") -> JudgeVerdict:
    return {"match": match, "confidence": conf, "rationale": why}


# ─── AC: 三种 case — 完全 match / 部分 match / 完全 miss ──────────────────────


def test_perfect_match_all_findings_align() -> None:
    """3 golden, 3 candidate, judge accepts each pair in order."""
    golden = [_f("a.py", 1, "g1"), _f("b.py", 2, "g2"), _f("c.py", 3, "g3")]
    candidate = [_f("a.py", 1, "c1"), _f("b.py", 2, "c2"), _f("c.py", 3, "c3")]
    judge = _stub_judge(golden, candidate, {(0, 0), (1, 1), (2, 2)})

    report = compute_review_overlap(golden, candidate, judge)

    assert report.precision == 1.0
    assert report.recall == 1.0
    assert report.f1 == 1.0
    assert sorted(report.matched_pairs) == [(0, 0), (1, 1), (2, 2)]
    assert report.unmatched_golden == []
    assert report.unmatched_candidate == []


def test_partial_match_half_caught() -> None:
    """2/4 golden caught, 1 spurious candidate. precision=2/3, recall=2/4."""
    golden = [_f("a.py", 1, "g1"), _f("b.py", 2, "g2"), _f("c.py", 3, "g3"), _f("d.py", 4, "g4")]
    candidate = [_f("a.py", 1, "c1"), _f("b.py", 2, "c2"), _f("x.py", 99, "noise")]
    # Judge says candidate 0 ↔ golden 0, candidate 1 ↔ golden 1, candidate 2 matches nobody.
    judge = _stub_judge(golden, candidate, {(0, 0), (1, 1)})

    report = compute_review_overlap(golden, candidate, judge)

    assert report.precision == 2 / 3
    assert report.recall == 0.5
    assert abs(report.f1 - (2 * (2 / 3) * 0.5 / ((2 / 3) + 0.5))) < 1e-9
    assert sorted(report.matched_pairs) == [(0, 0), (1, 1)]
    assert report.unmatched_golden == [2, 3]
    assert report.unmatched_candidate == [2]


def test_total_miss_no_overlap() -> None:
    """Judge rejects everything → precision = recall = f1 = 0."""
    golden = [_f("a.py", 1, "g1"), _f("b.py", 2, "g2")]
    candidate = [_f("x.py", 99, "garbage"), _f("y.py", 100, "more garbage")]
    judge = _stub_judge(golden, candidate, set())

    report = compute_review_overlap(golden, candidate, judge)

    assert report.precision == 0.0
    assert report.recall == 0.0
    assert report.f1 == 0.0
    assert report.matched_pairs == []
    assert report.unmatched_golden == [0, 1]
    assert report.unmatched_candidate == [0, 1]


# ─── Edge cases (PRD §10.3 — judge errors must not silently boost score) ─────


def test_both_empty_is_vacuously_perfect() -> None:
    report = compute_review_overlap([], [], _stub_judge([], [], set()))
    assert (report.precision, report.recall, report.f1) == (1.0, 1.0, 1.0)


def test_empty_candidate_misses_all_golden() -> None:
    """Eval-trap fix: empty candidate must not inflate precision to 1.0.

    The pre-fix scorer returned ``precision = 1.0`` whenever ``len(candidate)
    == 0`` (formal "no false positives" reading of the formula's zero
    denominator). That silently boosted dataset-level precision averages
    every time the agent truncated mid-thought or otherwise emitted nothing.
    Aligned with sklearn's ``zero_division=0`` convention so an empty
    contribution scores 0, not 1.
    """
    golden = [_f("a.py", 1, "g1")]
    report = compute_review_overlap(golden, [], _stub_judge(golden, [], set()))
    assert report.precision == 0.0
    assert report.recall == 0.0
    assert report.f1 == 0.0
    assert report.unmatched_golden == [0]


def test_empty_golden_makes_all_candidate_false_positives() -> None:
    """Symmetric: invented findings on a clean PR must not score recall=1.0."""
    candidate = [_f("x.py", 1, "noise")]
    report = compute_review_overlap([], candidate, _stub_judge([], candidate, set()))
    assert report.precision == 0.0
    assert report.recall == 0.0
    assert report.f1 == 0.0
    assert report.unmatched_candidate == [0]


def test_greedy_match_consumes_golden_at_most_once() -> None:
    """Two candidates would both match golden 0 — only the first wins."""
    golden = [_f("a.py", 1, "g0")]
    candidate = [_f("a.py", 1, "c0"), _f("a.py", 1, "c1-duplicate-of-c0")]
    # Judge would say match for both, but algorithm shouldn't double-count.
    judge = _stub_judge(golden, candidate, {(0, 0), (0, 1)})

    report = compute_review_overlap(golden, candidate, judge)

    assert len(report.matched_pairs) == 1  # not 2
    assert report.precision == 0.5  # one true positive + one false positive
    assert report.recall == 1.0
    assert report.unmatched_candidate == [1]


def test_judge_is_called_through_locked_martian_model(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Judge must run through the JudgeSettings-driven model — wire test.

    Asserts ``evals.scorers.review_judge.MARTIAN_JUDGE_MODEL_ID`` reflects
    whatever :class:`evals.runtime.config.JudgeSettings` resolves, so callers
    downstream (overlap scorer, task metadata, LangSmith experiment record)
    all see the same id. Pre-v3 this pinned the literal ``claude-opus-4-5``;
    v3 moved the id behind a resolver, and v5 collapsed the resolver into
    ``JudgeSettings`` directly so per-gateway overrides work without
    touching code.
    """
    import importlib

    from evals.runtime import config as runtime_config
    from evals.scorers import review_judge

    monkeypatch.setenv("OPENBOT_REVIEW_JUDGE_MODEL_ID", "test-judge-model")
    runtime_config.get_eval_config.cache_clear()
    importlib.reload(review_judge)
    try:
        seen_models: list[str] = []

        def judge_fn(golden: Finding, candidate: Finding) -> JudgeVerdict:
            seen_models.append(review_judge.MARTIAN_JUDGE_MODEL_ID)
            return _verdict(match=False)

        compute_review_overlap([_f("a.py", 1, "g")], [_f("a.py", 1, "c")], judge_fn)
        assert seen_models == ["test-judge-model"]
    finally:
        # Restore module-level constant for downstream tests in this session.
        monkeypatch.delenv("OPENBOT_REVIEW_JUDGE_MODEL_ID", raising=False)
        runtime_config.get_eval_config.cache_clear()
        importlib.reload(review_judge)


# ─── helpers ─────────────────────────────────────────────────────────────────


def _stub_judge(
    golden_list: list[Finding], candidate_list: list[Finding], match_pairs: set[tuple[int, int]]
):
    """Return a judge callable that matches by *list position* of the args."""
    g_id_to_idx = {id(f): i for i, f in enumerate(golden_list)}
    c_id_to_idx = {id(f): i for i, f in enumerate(candidate_list)}

    def fn(golden: Finding, candidate: Finding) -> JudgeVerdict:
        g_idx = g_id_to_idx.get(id(golden))
        c_idx = c_id_to_idx.get(id(candidate))
        if g_idx is None or c_idx is None:
            return _verdict(match=False)
        return _verdict(match=(g_idx, c_idx) in match_pairs)

    return fn

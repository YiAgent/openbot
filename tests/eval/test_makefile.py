from __future__ import annotations

from pathlib import Path


def test_evals_makefile_includes_inspect_view_targets() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    content = (repo_root / "evals/Makefile").read_text(encoding="utf-8")

    assert "view-bundle:" in content
    assert "view-open:" in content
    assert "view-stop:" in content
    assert (
        "$(INSPECT) view bundle --log-dir $(VIEW_LOG_DIR) --output-dir $(VIEW_OUTPUT_DIR) --overwrite"
        in content
    )
    assert "python3 -m http.server $(VIEW_PORT) -b $(VIEW_HOST) -d $(VIEW_OUTPUT_DIR)" in content
    assert "open http://127.0.0.1:$(VIEW_PORT)/" in content


def test_evals_makefile_smoke_fix_and_test_do_not_pass_per_task_models() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    content = (repo_root / "evals/Makefile").read_text(encoding="utf-8")

    assert "--model $(FIX_MODEL)" not in content
    assert "--model $(TEST_MODEL)" not in content
    assert "FIX_MODEL" not in content
    assert "TEST_MODEL" not in content

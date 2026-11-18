"""Tests for the reproduce_python_issue plugin."""

from __future__ import annotations

from openbot_plugins.reproduce_python_issue import _reproduce_python_issue


def test_no_traceback_returns_message():
    result = _reproduce_python_issue(issue_body="Everything is fine.", repo_files=[])
    assert "No Python traceback found" in result


def test_extracts_traceback_info():
    body = """\
Traceback (most recent call last):
  File "/app/main.py", line 42, in run
    result = process(data)
  File "/app/processor.py", line 10, in process
    return data.decode("utf-8")
UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff
"""
    result = _reproduce_python_issue(issue_body=body, repo_files=["main.py", "processor.py"])
    assert "UnicodeDecodeError" in result
    assert "processor.py" in result
    assert "Suggested reproduction" in result


def test_matches_repo_files():
    body = """\
Traceback (most recent call last):
  File "src/handlers/webhook.py", line 15, in handle
    process(payload)
KeyError: 'missing_key'
"""
    result = _reproduce_python_issue(
        issue_body=body,
        repo_files=["src/handlers/webhook.py", "src/utils.py"],
    )
    assert "webhook.py" in result
    assert "KeyError" in result


def test_multiple_tracebacks_uses_last():
    body = """\
Traceback (most recent call last):
  File "first.py", line 1, in first
    pass
ValueError: first error

Traceback (most recent call last):
  File "second.py", line 2, in second
    pass
TypeError: second error
"""
    result = _reproduce_python_issue(issue_body=body, repo_files=[])
    assert "TypeError" in result
    assert "second error" in result

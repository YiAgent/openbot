"""Tests for the reproduce_js_issue plugin."""

from __future__ import annotations

from openbot_plugins.reproduce_js_issue import _reproduce_js_issue


def test_no_stack_returns_message():
    result = _reproduce_js_issue(issue_body="Everything works fine.", repo_files=[])
    assert "No JavaScript" in result


def test_extracts_error_and_stack():
    body = """\
TypeError: Cannot read properties of undefined (reading 'map')
    at processData (/app/src/utils.js:42:15)
    at async handler (/app/src/api.js:10:5)
"""
    result = _reproduce_js_issue(issue_body=body, repo_files=["src/utils.js", "src/api.js"])
    assert "TypeError" in result
    assert "Cannot read properties" in result
    assert "utils.js" in result
    assert "Suggested reproduction" in result


def test_reference_error():
    body = """\
ReferenceError: myVar is not defined
    at Object.<anonymous> (/app/index.js:5:1)
"""
    result = _reproduce_js_issue(issue_body=body, repo_files=[])
    assert "ReferenceError" in result
    assert "myVar is not defined" in result

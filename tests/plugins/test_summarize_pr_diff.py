"""Tests for the summarize_pr_diff plugin."""

from __future__ import annotations

from openbot_plugins.summarize_pr_diff import _summarize_pr_diff


def test_empty_diff():
    result = _summarize_pr_diff(diff="", max_files=20)
    assert "Empty diff" in result


def test_parses_diff():
    diff = """\
diff --git a/src/main.py b/src/main.py
--- a/src/main.py
+++ b/src/main.py
@@ -1,3 +1,5 @@
 def main():
-    pass
+    result = process()
+    return result
+
+def process():
+    return 42
"""
    result = _summarize_pr_diff(diff=diff, max_files=20)
    assert "main.py" in result
    assert "Files changed:** 1" in result


def test_groups_added_modified_deleted():
    diff = """\
diff --git a/new_file.py b/new_file.py
--- /dev/null
+++ b/new_file.py
@@ -0,0 +1,3 @@
+def hello():
+    return "world"
+
diff --git a/old_file.py b/old_file.py
--- a/old_file.py
+++ /dev/null
@@ -1,3 +0,0 @@
-def goodbye():
-    return "world"
-
diff --git a/existing.py b/existing.py
--- a/existing.py
+++ b/existing.py
@@ -1,3 +1,4 @@
 def existing():
-    pass
+    return True
+
+def new_func():
+    pass
"""
    result = _summarize_pr_diff(diff=diff, max_files=20)
    assert "Added" in result
    assert "Deleted" in result
    assert "Modified" in result


def test_max_files_limit():
    files_diff = ""
    for i in range(30):
        files_diff += f"""\
diff --git a/file{i}.py b/file{i}.py
--- a/file{i}.py
+++ b/file{i}.py
@@ -1,2 +1,3 @@
 def func():
-    pass
+    return {i}
+
"""
    result = _summarize_pr_diff(diff=files_diff, max_files=5)
    assert "Files changed:** 30" in result

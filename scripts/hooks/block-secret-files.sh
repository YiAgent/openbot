#!/usr/bin/env bash
# Pre-commit: 拦截 .env / *.pem / *.key 进 git
# PRD §4.8：secret 保护是横切刚需；trufflehog 是内容扫描，本脚本是路径白名单

set -euo pipefail

BAD=$(git diff --cached --name-only --diff-filter=A \
  | grep -E '(^|/)(\.env(\..+)?$|.*\.pem$|.*\.key$)' \
  | grep -vE '\.(example|sample|template|dist)$' \
  || true)

if [ -n "$BAD" ]; then
  echo "  [block] secret-like file staged:"
  echo "$BAD" | sed 's/^/    /'
  echo "  use .env.example for templates; never commit real secrets"
  exit 1
fi

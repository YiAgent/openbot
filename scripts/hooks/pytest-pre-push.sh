#!/usr/bin/env bash
# Pre-push: 跑 unit + integration test，跳过 evals/
# PRD §8.3：unit (~70%) + integration (~20%) + middleware (~7%) + security (~3%)
# PRD §8.4：eval 永远不进 CI/hook，太贵

set -euo pipefail

if [ ! -d tests ]; then
  echo "  [skip] tests/ not found yet"
  exit 0
fi

if ! command -v pytest >/dev/null 2>&1; then
  echo "  [skip] pytest not installed in PATH"
  exit 0
fi

exec pytest -q --ignore=evals --ignore=tests/real_service --no-header -x

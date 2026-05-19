#!/usr/bin/env bash
# Pre-push: 检查 hexagonal architecture 依赖边界
# PRD §8.4：保持 core/domain/application/infrastructure/entrypoints 严苛的分层

set -euo pipefail

if ! command -v lint-imports >/dev/null 2>&1; then
  echo "  [skip] import-linter not installed in PATH"
  exit 0
fi

echo "  [linter] checking hexagonal layer boundaries..."
exec lint-imports

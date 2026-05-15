#!/usr/bin/env bash
# Pre-commit: 扫 staged diff，找 verified secret
# PRD §4.8：bot 输出过 trufflehog；本地 commit 也同样过

set -euo pipefail

if ! command -v trufflehog >/dev/null 2>&1; then
  cat <<'EOF' >&2
  [error] trufflehog not in PATH
  macOS:  brew install trufflesecurity/trufflehog/trufflehog
  Linux:  curl -sSfL https://raw.githubusercontent.com/trufflesecurity/trufflehog/main/scripts/install.sh | sh -s -- -b /usr/local/bin
EOF
  exit 1
fi

# 只扫工作区新增/修改的内容（不依赖 HEAD，兼容初始 commit）
exec trufflehog filesystem . \
  --no-update \
  --only-verified \
  --fail \
  --exclude-paths=<(printf '%s\n' '.git/' '.venv/' 'node_modules/' '.pre-commit-cache/')

#!/usr/bin/env bash
# OpenBot — 一键安装 pre-commit / commit-msg / pre-push hooks
#
# 使用：
#   ./scripts/install-hooks.sh

set -euo pipefail

cd "$(dirname "$0")/.."

# 1. 安装 pre-commit 本体（优先 uv，回落 pipx，再回落 pip --user）
if ! command -v pre-commit >/dev/null 2>&1; then
  echo "[install-hooks] pre-commit not found, installing..."
  if command -v uv >/dev/null 2>&1; then
    uv tool install pre-commit
  elif command -v pipx >/dev/null 2>&1; then
    pipx install pre-commit
  else
    python3 -m pip install --user pre-commit
  fi
fi

# 2. 安装 trufflehog（PRD §4.8 必备）—— 用户提示而不强装
if ! command -v trufflehog >/dev/null 2>&1; then
  cat <<'EOF'
[install-hooks] WARNING: trufflehog not in PATH.
  macOS:   brew install trufflesecurity/trufflehog/trufflehog
  Linux:   curl -sSfL https://raw.githubusercontent.com/trufflesecurity/trufflehog/main/scripts/install.sh | sh -s -- -b /usr/local/bin
  Trufflehog hook 在 pre-commit / pre-push 阶段都会用到，请先装。
EOF
fi

# 3. 配置 commit message 模板
git config commit.template .gitmessage

# 4. 安装所有 hook 类型
pre-commit install --install-hooks
pre-commit install --hook-type commit-msg
pre-commit install --hook-type pre-push

echo
echo "[install-hooks] done. 试跑一次："
echo "  pre-commit run --all-files"

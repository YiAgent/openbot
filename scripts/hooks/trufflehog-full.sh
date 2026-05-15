#!/usr/bin/env bash
# Pre-push: 扫全 git 历史，防止把秘密 push 到 origin
# PRD §4.8：trufflehog 是横切安全组件

set -euo pipefail

if ! command -v trufflehog >/dev/null 2>&1; then
  echo "  [error] trufflehog not in PATH; install via brew/curl (see scripts/hooks/trufflehog-staged.sh)" >&2
  exit 1
fi

exec trufflehog git file://. \
  --no-update \
  --only-verified \
  --fail

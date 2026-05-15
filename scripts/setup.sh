#!/usr/bin/env bash
# OpenBot setup wrapper — delegates to openbot.setup_wizard.
# All real logic is in Python so it can be tested cleanly.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
  echo "❌ uv not found. Install: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

uv sync --dev >/dev/null
exec uv run python -m openbot.setup_wizard "$@"

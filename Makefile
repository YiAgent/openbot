# OpenBot — developer Makefile
# All targets run through `uv` so no manual venv activation is needed.
# See CLAUDE.md and docs/prd/openbot-prd.md for project conventions.

.DEFAULT_GOAL := help
SHELL := /usr/bin/env bash

# uv-managed entrypoints
UV       ?= uv
PY       ?= $(UV) run
RUFF     ?= $(PY) ruff
PYTEST   ?= $(PY) pytest
UVICORN  ?= $(PY) uvicorn

APP      ?= openbot.webapp:app
HOST     ?= 0.0.0.0
PORT     ?= 8000

.PHONY: help sync install fmt fmt-check lint lint-fix check test test-fast \
        run dev hooks secret-scan clean distclean

help:  ## Show this help
	@awk 'BEGIN {FS = ":.*?## "; printf "\nUsage: make \033[36m<target>\033[0m\n\nTargets:\n"} \
	     /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ── env / deps ────────────────────────────────────────────────────────────
sync:  ## Install + sync dev dependencies via uv
	$(UV) sync --dev

install: sync  ## Alias for `sync`

# ── code quality (PRD §8.4 verification trio) ─────────────────────────────
fmt:  ## Apply ruff formatting
	$(RUFF) format .

fmt-check:  ## Verify formatting (no writes)
	$(RUFF) format --check .

lint:  ## Run ruff lint
	$(RUFF) check .

lint-fix:  ## Run ruff lint with autofix
	$(RUFF) check --fix .

test:  ## Run pytest (excludes evals/ per PRD §8.3)
	$(PYTEST) --ignore=evals

test-fast:  ## Pytest, fail fast, quiet
	$(PYTEST) -q -x --ignore=evals

check: fmt-check lint test  ## Full verification (fmt + lint + test) — required after any Python change

# ── runtime ───────────────────────────────────────────────────────────────
run:  ## Run FastAPI app (production-style)
	$(UVICORN) $(APP) --host $(HOST) --port $(PORT)

dev:  ## Run FastAPI app with autoreload
	$(UVICORN) $(APP) --host $(HOST) --port $(PORT) --reload

# ── tooling ───────────────────────────────────────────────────────────────
hooks:  ## Install git hooks (pre-commit / pre-push)
	bash scripts/install-hooks.sh

secret-scan:  ## Run trufflehog over full git history
	bash scripts/hooks/trufflehog-full.sh

# ── housekeeping ──────────────────────────────────────────────────────────
clean:  ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

distclean: clean  ## Also remove the local virtualenv
	rm -rf .venv

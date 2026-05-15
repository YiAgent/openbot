# OpenBot · Makefile
# Common dev workflows. `make help` lists everything.

SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

# ─── Tunables ───────────────────────────────────────────────────
PORT ?= 8080
HOST ?= 127.0.0.1
TARGET_URL := http://$(HOST):$(PORT)/webhook/github
# Read smee channel from .env; falls back to empty so `make dev` errors clearly.
SMEE_URL := $(shell sed -n 's/^OPENBOT_GITHUB_WEBHOOK_PROXY_URL=\(.*\)$$/\1/p' .env 2>/dev/null)

.PHONY: help install hooks test lint fmt dev dev-server dev-smee smoke setup \
        compose-up compose-down compose-logs compose-ps clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-15s\033[0m %s\n",$$1,$$2}'

install: ## uv sync + git hooks
	uv sync --dev
	./scripts/install-hooks.sh

hooks: ## (re)install pre-commit / pre-push hooks
	./scripts/install-hooks.sh

test: ## Run pytest
	uv run pytest

lint: ## ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Apply ruff format
	uv run ruff format .

# ─── Live dev loop ────────────────────────────────────────────────
dev: ## uvicorn + smee-client concurrently (Ctrl-C kills both)
	@if [ -z "$(SMEE_URL)" ]; then \
	  echo "❌ OPENBOT_GITHUB_WEBHOOK_PROXY_URL not set in .env"; \
	  echo "   run \`make setup\` first, or set it manually."; \
	  exit 1; \
	fi
	@command -v npx >/dev/null || { echo "❌ npx not found — install Node"; exit 1; }
	@echo "▶ uvicorn   $(HOST):$(PORT)"
	@echo "▶ smee      $(SMEE_URL) → $(TARGET_URL)"
	@trap 'kill 0' EXIT INT TERM; \
	uv run uvicorn openbot.webapp:app --host $(HOST) --port $(PORT) & \
	npx --yes smee-client@latest --url "$(SMEE_URL)" --target "$(TARGET_URL)" & \
	wait

dev-server: ## Just uvicorn (no smee), with --reload
	uv run uvicorn openbot.webapp:app --host $(HOST) --port $(PORT) --reload

dev-smee: ## Just smee-client (assumes server is up)
	@if [ -z "$(SMEE_URL)" ]; then echo "❌ SMEE_URL unset"; exit 1; fi
	npx --yes smee-client@latest --url "$(SMEE_URL)" --target "$(TARGET_URL)"

smoke: ## Hit /health to verify server is up
	@curl -sf "http://$(HOST):$(PORT)/health" && echo

setup: ## Interactive GitHub App + .env wizard (manifest flow)
	uv run python -m openbot.setup_wizard

# ─── Postgres + Redis (docker-compose) ────────────────────────────
compose-up: ## Start Postgres + Redis in background
	docker compose up -d

compose-down: ## Stop containers (data preserved in named volumes)
	docker compose down

compose-logs: ## Tail compose logs
	docker compose logs -f --tail=100

compose-ps: ## Show compose container state
	docker compose ps

clean: ## Remove local caches (preserves .env / secrets / data volumes)
	rm -rf .venv .pytest_cache .ruff_cache .uv-cache .mypy_cache

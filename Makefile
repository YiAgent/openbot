# OpenBot · Makefile
# Common dev workflows. `make help` lists everything.
# All targets run through `uv` so no manual venv activation is needed.

SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

# ─── Tunables ───────────────────────────────────────────────────
UV       ?= uv
PY       ?= $(UV) run
RUFF     ?= $(PY) ruff
PYTEST   ?= $(PY) pytest
UVICORN  ?= $(PY) uvicorn

APP      ?= openbot.webapp:app
PORT     ?= 8080
HOST     ?= 127.0.0.1
TARGET_URL := http://$(HOST):$(PORT)/webhook/github
# Read smee channel from .env; falls back to empty so `make dev` errors clearly.
SMEE_URL := $(shell sed -n 's/^OPENBOT_GITHUB_WEBHOOK_PROXY_URL=\(.*\)$$/\1/p' .env 2>/dev/null)

.PHONY: help install sync hooks test test-fast lint lint-fix fmt fmt-check check \
        dev dev-server dev-smee run smoke setup secret-scan \
        compose-up compose-down compose-logs compose-ps clean distclean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-15s\033[0m %s\n",$$1,$$2}'

# ─── env / deps ───────────────────────────────────────────────────
sync: ## uv sync --dev (install + sync dev deps)
	$(UV) sync --dev

install: sync ## Alias for `sync` (also installs git hooks)
	./scripts/install-hooks.sh

hooks: ## (re)install pre-commit / pre-push hooks
	./scripts/install-hooks.sh

# ─── code quality (PRD §8.4 verification trio) ────────────────────
fmt: ## Apply ruff format
	$(RUFF) format .

fmt-check: ## Verify formatting (no writes)
	$(RUFF) format --check .

lint: ## ruff lint
	$(RUFF) check .

lint-fix: ## ruff lint with autofix
	$(RUFF) check --fix .

test: ## Run pytest (excludes evals/ per PRD §8.3)
	$(PYTEST) --ignore=evals

test-fast: ## pytest, fail fast, quiet
	$(PYTEST) -q -x --ignore=evals

check: fmt-check lint test ## Full verification (fmt-check + lint + test) — required after any Python change

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
	$(UVICORN) $(APP) --host $(HOST) --port $(PORT) & \
	npx --yes smee-client@latest --url "$(SMEE_URL)" --target "$(TARGET_URL)" & \
	wait

dev-server: ## Just uvicorn (no smee), with --reload
	$(UVICORN) $(APP) --host $(HOST) --port $(PORT) --reload

dev-smee: ## Just smee-client (assumes server is up)
	@if [ -z "$(SMEE_URL)" ]; then echo "❌ SMEE_URL unset"; exit 1; fi
	npx --yes smee-client@latest --url "$(SMEE_URL)" --target "$(TARGET_URL)"

run: ## Run FastAPI app (production-style, no reload)
	$(UVICORN) $(APP) --host $(HOST) --port $(PORT)

worker: ## Run the Redis Stream worker (slice D — consumes openbot:workflows)
	uv run python -m openbot.queue.runner

smoke: ## Hit /health to verify server is up
	@curl -sf "http://$(HOST):$(PORT)/health" && echo

setup: ## Interactive GitHub App + .env wizard (manifest flow)
	uv run python -m openbot.setup_wizard

secret-scan: ## Run trufflehog over full git history
	bash scripts/hooks/trufflehog-full.sh

# ─── Postgres + Redis (docker-compose) ────────────────────────────
compose-up: ## Start Postgres + Redis in background
	docker compose up -d

compose-down: ## Stop containers (data preserved in named volumes)
	docker compose down

compose-logs: ## Tail compose logs
	docker compose logs -f --tail=100

compose-ps: ## Show compose container state
	docker compose ps

# ─── housekeeping ─────────────────────────────────────────────────
clean: ## Remove local caches (preserves .env / secrets / data volumes)
	rm -rf .pytest_cache .ruff_cache .uv-cache .mypy_cache build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

distclean: clean ## Also remove the local virtualenv
	rm -rf .venv

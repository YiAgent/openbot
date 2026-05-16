# OpenBot evals · Makefile
# Eval-only workflows live here so the repo-root Makefile stays focused on
# normal development tasks.

SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

# ─── Tunables ───────────────────────────────────────────────────
UV              ?= uv
UV_CACHE_DIR    ?= /tmp/openbot-uv-cache
export UV_CACHE_DIR
PY              ?= $(UV) run
PYTEST          ?= $(PY) pytest
INSPECT         ?= $(PY) inspect
DOPPLER         ?= doppler run --project openbot --config dev --

LIMIT           ?= 5
VIEW_LOG_DIR    ?= logs
VIEW_OUTPUT_DIR ?= logs-www
VIEW_HOST       ?= 127.0.0.1
VIEW_PORT       ?= 8123
VIEW_SERVER_LOG ?= /tmp/openbot-inspect-view.log

# ─── Inspect display / logging ──────────────────────────────────
# DISPLAY:   full | rich | plain | log | none  (rich = live progress + CI-friendly)
# LOG_LEVEL: debug | http | sandbox | info | warning | error | critical
# LOG_DIR:   where Inspect writes per-sample .eval logs (timestamped by default)
DISPLAY         ?= rich
LOG_LEVEL       ?= info
ifndef LOG_DIR
LOG_DIR         := logs/$(shell date +%Y%m%d-%H%M%S)
endif
INSPECT_FLAGS   ?= --display $(DISPLAY) --log-level $(LOG_LEVEL) --log-dir evals/$(LOG_DIR)

define inspect_banner
	@printf '\033[36m▶ inspect eval\033[0m %s\n  \033[2mlimit=%s display=%s log-level=%s\n  log-dir=evals/%s\033[0m\n' "$(1)" "$(LIMIT)" "$(DISPLAY)" "$(LOG_LEVEL)" "$(LOG_DIR)"
	@mkdir -p "evals/$(LOG_DIR)"
endef

.PHONY: help \
        data data-refresh \
        data-review data-chat data-fix data-test \
        data-review-refresh data-chat-refresh data-fix-refresh data-test-refresh \
        test smoke smoke-docker smoke-review smoke-fix smoke-test smoke-chat check \
        view-bundle view-open view-stop

help: ## Show eval-only targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-20s\033[0m %s\n",$$1,$$2}'

# ─── Dataset publication / refresh ──────────────────────────────
data: data-review data-chat data-fix data-test ## Publish all eval datasets safely (fails if a dataset already exists)

data-refresh: data-review-refresh data-chat-refresh data-fix-refresh data-test-refresh ## Rebuild all eval datasets with --force

data-review: ## Publish Martian review dataset to LangSmith
	$(DOPPLER) $(PY) python -m evals.scripts.build_review_martian_dataset

data-chat: ## Publish SWE-QA-Pro chat dataset to LangSmith
	$(DOPPLER) $(PY) python -m evals.scripts.build_chat_swe_qa_pro_dataset

data-fix: ## Publish SWE-bench Verified LangSmith mirror
	$(DOPPLER) $(PY) python -m evals.scripts.build_swe_bench_verified_dataset

data-test: ## Publish SWT-Bench Verified LangSmith mirror
	$(DOPPLER) $(PY) python -m evals.scripts.build_swt_bench_verified_dataset

data-review-refresh: ## Rebuild Martian review dataset with --force
	$(DOPPLER) $(PY) python -m evals.scripts.build_review_martian_dataset --force

data-chat-refresh: ## Rebuild SWE-QA-Pro chat dataset with --force
	$(DOPPLER) $(PY) python -m evals.scripts.build_chat_swe_qa_pro_dataset --force

data-fix-refresh: ## Rebuild SWE-bench Verified LangSmith mirror with --force
	$(DOPPLER) $(PY) python -m evals.scripts.build_swe_bench_verified_dataset --force

data-test-refresh: ## Rebuild SWT-Bench Verified LangSmith mirror with --force
	$(DOPPLER) $(PY) python -m evals.scripts.build_swt_bench_verified_dataset --force

# ─── Local verification ─────────────────────────────────────────
test: ## Run eval-focused pytest suite only
	cd .. && $(PYTEST) tests/eval

check: test smoke ## Run eval tests, then all live smoke evals

# ─── Live smoke evals ────────────────────────────────────────────
smoke: smoke-docker smoke-review smoke-fix smoke-test smoke-chat ## Run all implemented live smoke evals

smoke-docker: ## Verify DockerSandboxBackend end-to-end (local docker daemon, ~10 s, free)
	cd .. && $(PY) python scripts/smoke_docker_backend.py

smoke-review: ## Run review baseline smoke eval
	$(call inspect_banner,review_martian_baseline_crb)
	cd .. && $(DOPPLER) $(INSPECT) eval \
		'evals/tasks/review_martian.py@review_martian_baseline_crb' \
		--limit $(LIMIT) $(INSPECT_FLAGS)

smoke-fix: ## Run SWE-bench fix baseline smoke eval
	$(call inspect_banner,fix_swe_bench_verified_deepagents)
	cd .. && $(DOPPLER) $(INSPECT) eval \
		'evals/tasks/fix_swe_bench_verified.py@fix_swe_bench_verified_deepagents' \
		--limit $(LIMIT) $(INSPECT_FLAGS)

smoke-test: ## Run SWT-Bench test-generation baseline smoke eval
	$(call inspect_banner,test_swt_bench_verified_deepagents)
	cd .. && $(DOPPLER) $(INSPECT) eval \
		'evals/tasks/test_swt_bench_verified.py@test_swt_bench_verified_deepagents' \
		--limit $(LIMIT) $(INSPECT_FLAGS)

smoke-chat: ## Run SWE-QA-Pro chat baseline smoke eval
	$(call inspect_banner,chat_swe_qa_pro_baseline)
	cd .. && $(DOPPLER) $(INSPECT) eval \
		'evals/tasks/chat_swe_qa_pro.py@chat_swe_qa_pro_baseline' \
		--limit $(LIMIT) $(INSPECT_FLAGS)

# ─── Inspect log viewer ─────────────────────────────────────────
view-bundle: ## Build a static Inspect viewer bundle from eval logs
	cd .. && $(INSPECT) view bundle --log-dir $(VIEW_LOG_DIR) --output-dir $(VIEW_OUTPUT_DIR) --overwrite

view-open: view-bundle ## Build the viewer bundle, serve it locally, and open it in the browser
	@if lsof -tiTCP:$(VIEW_PORT) -sTCP:LISTEN >/dev/null 2>&1; then \
		printf '\033[36m▶ inspect view\033[0m reusing existing server on http://$(VIEW_HOST):$(VIEW_PORT)/\n'; \
	else \
		printf '\033[36m▶ inspect view\033[0m starting server on http://$(VIEW_HOST):$(VIEW_PORT)/\n'; \
		cd .. && nohup python3 -m http.server $(VIEW_PORT) -b $(VIEW_HOST) -d $(VIEW_OUTPUT_DIR) >"$(VIEW_SERVER_LOG)" 2>&1 & \
		printf '%s\n' "$$!" >"$(VIEW_SERVER_LOG).pid"; \
	fi
	@open http://127.0.0.1:$(VIEW_PORT)/
	@printf '  \033[2mbundle=%s\n  server-log=%s\n\033[0m' "$(VIEW_OUTPUT_DIR)" "$(VIEW_SERVER_LOG)"

view-stop: ## Stop the local Inspect viewer server for VIEW_PORT
	@pid="$$(lsof -tiTCP:$(VIEW_PORT) -sTCP:LISTEN || true)"; \
	if [[ -n "$$pid" ]]; then \
		kill $$pid; \
		rm -f "$(VIEW_SERVER_LOG).pid"; \
		printf '\033[36m▶ inspect view\033[0m stopped server on port %s\n' "$(VIEW_PORT)"; \
	else \
		printf '\033[36m▶ inspect view\033[0m no server listening on port %s\n' "$(VIEW_PORT)"; \
	fi

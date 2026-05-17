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

# ─── Remote docker daemon for SWT-bench harness ─────────────────
# Path A grading reuses ReProAgent's vendored swt-bench harness
# (see CLAUDE.md). The harness shells out to a Docker daemon; the dev
# Mac doesn't run docker locally, so we point DOCKER_HOST at a Linux
# host with `docker` installed and the user in the docker group.
#
# Default is the LAN-only TCP listener wy-linux exposes on 10.0.0.3:2375
# (drop-in unit lives at /etc/systemd/system/docker.service.d/tcp-bind.conf;
# see docs/runbooks/expose-remote-docker-tcp.md). TCP > SSH for this
# workload because long-running `docker exec` streams over an SSH tunnel
# (paramiko or use_ssh_client) consistently deadlock during multi-minute
# pytest output reads — SSH idle drops kill the HTTP stream silently and
# Python blocks on read forever. Raw TCP keeps the connection healthy
# for the full grading run.
#
# `ssh://linux` (HostName 10.0.0.3, User wy, keepalive + ControlMaster
# already in ~/.ssh/config) is still supported as a fallback if TCP
# becomes unavailable — override per-invocation:
#   make grade-test DOCKER_HOST=ssh://linux
#
# When DOCKER_HOST is ssh://*, the vendored harness auto-patches
# docker.from_env() via evals.common.docker_ssh.apply_ssh_patch() so
# the SDK shells out to the system `ssh` binary (Keychain-aware) rather
# than paramiko. For tcp:// the patch is a no-op.
DOCKER_HOST     ?= tcp://10.0.0.3:2375
export DOCKER_HOST

# ─── HuggingFace auth (pulled from Doppler, never on a cmdline) ─
# The vendored SWT-bench harness loads princeton-nlp/SWE-bench_Verified
# via `datasets.load_dataset()`, which auto-reads `HF_TOKEN`. Without
# it HF Hub rate-limits us — fine for the harness today since the
# parquet shards cache locally after first read, but explicit auth is
# better practice and silences the "unauthenticated requests" warning.
#
# grade-test deliberately skips $(DOPPLER) (so the bogus
# DOCKER_HOST=unix://colima from the dev config can't clobber the
# Makefile-set ssh:// / tcp:// endpoint). We fetch HF_TOKEN at parse
# time instead — the value lands in Make's process env, never on a
# child cmdline or in shell history. Empty if Doppler isn't installed
# or the secret hasn't been set.
HF_TOKEN := $(shell doppler secrets get HF_TOKEN --project openbot --config dev --plain 2>/dev/null)
export HF_TOKEN

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

# ─── Resilience flags (overridable) ─────────────────────────────
# MAX_SAMPLES:     concurrent samples — keep modest so a flaky endpoint can
#                  back off (50 samples × parallel HTTP storms 429'd mimo).
# ATTEMPT_TIMEOUT: hard ceiling for one model-API attempt. Inspect kills the
#                  attempt past this; ``--retry-on-error`` then re-tries.
# RETRY_ON_ERROR:  per-sample retry budget for errors the HTTP layer didn't
#                  recover from. Independent from
#                  ``OPENBOT_DEEPAGENTS_MODEL_MAX_RETRIES`` (HTTP layer).
# FAIL_ON_ERROR:   threshold above which the eval **halts** instead of
#                  pushing more empty-output noise to LangSmith. Inspect
#                  accepts an integer (absolute error count) or a fraction
#                  0-1 (proportion of samples errored). Default 0.1 → halt
#                  when 10% of samples error — generous enough for genuine
#                  transient blips (rate limits, network jitter), strict
#                  enough to catch systemic failures (gateway quota
#                  exhausted, missing API key) before they pollute the
#                  experiment with 30+ zero-score feedback rows.
#                  Previously this was ``--no-fail-on-error --score-on-error``
#                  which kept every errored sample in the aggregate as a 0
#                  and forced LangSmith to record empty-output Feedback for
#                  each — pure noise. We now drop both flags so errored
#                  samples are excluded from the metric *and* from feedback
#                  upload, and the run terminates once the failure rate
#                  crosses ``FAIL_ON_ERROR``.
MAX_SAMPLES     ?= 4
ATTEMPT_TIMEOUT ?= 180
RETRY_ON_ERROR  ?= 2
FAIL_ON_ERROR   ?= 0.1
RESILIENCE_FLAGS ?= --fail-on-error=$(FAIL_ON_ERROR) --retry-on-error=$(RETRY_ON_ERROR) --attempt-timeout $(ATTEMPT_TIMEOUT) --max-samples $(MAX_SAMPLES)

INSPECT_FLAGS   ?= --display $(DISPLAY) --log-level $(LOG_LEVEL) --log-dir evals/$(LOG_DIR) $(RESILIENCE_FLAGS)

define inspect_banner
	@printf '\033[36m▶ inspect eval\033[0m %s\n  \033[2mlimit=%s display=%s log-level=%s\n  log-dir=evals/%s\033[0m\n' "$(1)" "$(LIMIT)" "$(DISPLAY)" "$(LOG_LEVEL)" "$(LOG_DIR)"
	@mkdir -p "evals/$(LOG_DIR)"
endef

.PHONY: help \
        data data-refresh \
        data-review data-chat data-fix data-test \
        data-review-refresh data-chat-refresh data-fix-refresh data-test-refresh \
        test smoke smoke-docker smoke-review smoke-fix smoke-test smoke-chat check \
        grade-test writeback-test eval-test \
        grade-fix writeback-fix eval-fix \
        resume resume-latest \
        sandbox-clean sandbox-status sandbox-prune-images \
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

smoke-chat: ## Run SWE-QA-Pro chat "+Agent" smoke eval — paper Table 2 right column (Docker sandbox + repo clone + read tools)
	$(call inspect_banner,chat_swe_qa_pro_openbot)
	cd .. && $(DOPPLER) $(INSPECT) eval \
		'evals/tasks/chat_swe_qa_pro.py@chat_swe_qa_pro_openbot' \
		--limit $(LIMIT) $(INSPECT_FLAGS)

# ─── End-to-end SWT pipeline (smoke → grade → writeback) ────────
# `eval-test` runs the full Path-A SWT evaluation in one shot:
#
#   1. smoke-test       Inspect AI agent run → predictions.jsonl + LangSmith
#                       Experiment with one Run per instance + swt_export_ok
#                       feedback (in-task sentinel).
#   2. grade-test       Vendored SWT-bench harness → 6-pass pytest grid on
#                       remote Docker → evaluation_results/<...>.json with
#                       per-instance resolved/unresolved verdict.
#   3. writeback-test   Append swt_bench_pass_at_1 feedback (1.0=resolved,
#                       0.0=unresolved, skip=harness-errored) to the same
#                       LangSmith Runs that step 1 created.
#
# Each phase is still standalone-runnable for partial retries. Defaults
# pick the latest predictions / latest report, so back-to-back invocations
# stay aligned without explicit IDs.
#
# Skip the grade+writeback tail for fast iteration:
#   make eval-test SKIP_GRADE=1   ← equivalent to plain `make smoke-test`
eval-test: ## Full SWT-bench pipeline: agent run → offline grading → LangSmith writeback
	@$(MAKE) smoke-test
ifndef SKIP_GRADE
	@$(MAKE) grade-test
	@$(MAKE) writeback-test
else
	@echo "▸ SKIP_GRADE set — stopping after Inspect agent run"
endif

# ─── Offline SWT-bench grading (Path A) ─────────────────────────
# Runs the vendored SWT-bench harness (evals/third_party/swt_bench/)
# against an exported predictions.jsonl. The harness spawns per-
# instance Docker containers; the Mac dev box doesn't run docker
# locally, so DOCKER_HOST=ssh://linux (set at the top of this
# Makefile) forwards every docker call to 10.0.0.3.
#
# Knobs (all are `GRADE_*` namespaced to avoid colliding with pydantic-
# settings field names — bare `PREDICTIONS` / `INSTANCE_IDS` would be
# auto-bound by evals.common.config.EvalSettings and crash on JSON parse):
#   GRADE_PREDICTIONS    — path to predictions.jsonl (default: latest)
#   GRADE_MAX_WORKERS    — concurrent instances on the remote daemon
#                          (default 4; the remote has 20 CPUs but
#                          conda-env builds are I/O heavy, polite start)
#   GRADE_RUN_ID         — name for this grading run (default openbot-<ts>)
#   GRADE_DATASET        — HF dataset the predictions came from (default
#                          princeton-nlp/SWE-bench_Verified; --filter_swt
#                          is always set, so we restrict to SWT subset)
#   GRADE_INSTANCE_IDS   — optional space-separated instance subset
#
# The harness writes:
#   evals/logs/swt_bench/run_instances/<run-id>/<instance>/{test_output.txt, eval.log, …}
#   evals/logs/swt_bench/results/<run-id>.json   (per-instance verdicts)
GRADE_PREDICTIONS  ?= $(shell ls -1t outputs/test_swt_bench_verified/*.predictions.jsonl 2>/dev/null | head -1)
GRADE_MAX_WORKERS  ?= 4
GRADE_RUN_ID       ?= openbot-$(shell date -u +%Y%m%dT%H%M%SZ)
GRADE_DATASET      ?= princeton-nlp/SWE-bench_Verified
GRADE_INSTANCE_IDS ?=
# Pass `GRADE_FORCE_REBUILD=true` to rebuild base/env/instance images
# from scratch — needed after a cross-arch build failure pollutes the
# remote cache (e.g. arm64 ARG/ENV layers being reused on x86_64). The
# harness only nukes the specific image tags it manages (exec.base.*,
# exec.env.*, exec.eval.*), so other tenants on the shared daemon are
# unaffected.
GRADE_FORCE_REBUILD ?= false

writeback-test: ## Writeback SWT harness verdicts → LangSmith swt_bench_pass_at_1 feedback
	@# Absolutise the resolved path BEFORE the `cd ..` below — otherwise
	@# the `../evals/...` glob (relative to evals/) gets re-resolved
	@# under repo-root, ending up one dir too high.
	@if [ -z "$(GRADE_REPORT)" ]; then \
		LATEST="$$(ls -1t ../evals/logs/swt_bench/results/*.json 2>/dev/null | head -1)"; \
		if [ -z "$$LATEST" ]; then \
			echo "no evals/logs/swt_bench/results/*.json — run make grade-test first" >&2; \
			exit 1; \
		fi; \
		GRADE_REPORT="$$(cd "$$(dirname "$$LATEST")" && pwd)/$$(basename "$$LATEST")"; \
	else \
		GRADE_REPORT="$(GRADE_REPORT)"; \
	fi; \
	if [ -z "$(GRADE_EXPERIMENT)" ]; then \
		LATEST_PRED="$$(ls -1t outputs/test_swt_bench_verified/*.predictions.jsonl 2>/dev/null | head -1)"; \
		EXP_NAME="$$(basename "$$LATEST_PRED" .predictions.jsonl)"; \
	else \
		EXP_NAME="$(GRADE_EXPERIMENT)"; \
	fi; \
	printf '\033[36m▶ swt-bench writeback\033[0m %s → experiment %s\n' "$$GRADE_REPORT" "$$EXP_NAME"; \
	cd .. && $(DOPPLER) $(PY) python -m evals.scripts.writeback_swt_grades \
		--evaluation-result "$$GRADE_REPORT" \
		--experiment-name "$$EXP_NAME" \
		$(if $(DRY_RUN),--dry-run)

grade-test: ## Run vendored SWT-bench harness on remote docker (DOCKER_HOST) against GRADE_PREDICTIONS
	@if [ -z "$(GRADE_PREDICTIONS)" ]; then \
		echo "no predictions.jsonl under evals/outputs/test_swt_bench_verified/ — run make smoke-test first" >&2; \
		exit 1; \
	fi
	@printf '\033[36m▶ swt-bench grade\033[0m %s\n  \033[2mDOCKER_HOST=%s run_id=%s workers=%s\033[0m\n' \
		"$(GRADE_PREDICTIONS)" "$(DOCKER_HOST)" "$(GRADE_RUN_ID)" "$(GRADE_MAX_WORKERS)"
	@# NB: deliberately NOT wrapped in $(DOPPLER) — the harness only needs
	@# docker + public HuggingFace datasets, and `doppler run -p openbot -c
	@# dev` injects DOCKER_HOST=unix://.../colima/docker.sock which clobbers
	@# the SSH endpoint Makefile set above. Writeback to LangSmith (which
	@# DOES need secrets) lives in a separate script + target.
	cd .. && $(PY) python -m evals.third_party.swt_bench.main \
		--dataset_name $(GRADE_DATASET) \
		--filter_swt \
		--predictions_path "evals/$(GRADE_PREDICTIONS)" \
		--max_workers $(GRADE_MAX_WORKERS) \
		--run_id $(GRADE_RUN_ID) \
		--force_rebuild $(GRADE_FORCE_REBUILD) \
		$(if $(GRADE_INSTANCE_IDS),--instance_ids $(GRADE_INSTANCE_IDS))

# ─── Offline SWE-bench grading (Path A, parallel to SWT) ────────
# Uses the pip-installed `swebench` package (no vendor needed; unlike
# SWT it ships official PyPI distributions). Pulls per-instance
# eval images from docker.io/swebench/sweb.eval.x86_64.<inst>:latest
# (Docker Hub public registry — no auth required).
#
# Knobs mirror grade-test, with the FIX_* prefix replacing the SWT-only
# defaults so both pipelines can hold state in the same Make invocation.
#   GRADE_FIX_PREDICTIONS  — predictions.jsonl from `make smoke-fix`
#   GRADE_FIX_MAX_WORKERS  — concurrent instances (default 4)
#   GRADE_FIX_RUN_ID       — name for this grading run + report basename
#   GRADE_FIX_DATASET      — princeton-nlp/SWE-bench_Verified by default
#   GRADE_FIX_INSTANCE_IDS — optional space-separated subset
#   GRADE_FIX_NAMESPACE    — Docker Hub namespace for prebuilt images
#                            (default `swebench`; set to a private mirror
#                            if you've baked your own)
#   GRADE_FIX_REPORT_DIR   — where swebench writes <run_id>.<model>.json
#                            (default `evaluation_results_swe` — kept
#                            separate from SWT's `evaluation_results`
#                            so writeback can pick by directory)
#   GRADE_FIX_CACHE_LEVEL  — none|base|env|instance (default `env`)
GRADE_FIX_PREDICTIONS  ?= $(shell ls -1t outputs/fix_swe_bench_verified/*.predictions.jsonl 2>/dev/null | head -1)
GRADE_FIX_MAX_WORKERS  ?= 4
GRADE_FIX_RUN_ID       ?= openbot-fix-$(shell date -u +%Y%m%dT%H%M%SZ)
GRADE_FIX_DATASET      ?= princeton-nlp/SWE-bench_Verified
GRADE_FIX_INSTANCE_IDS ?=
GRADE_FIX_NAMESPACE    ?= swebench
# Mirrors the SWT layout (evals/logs/swt_bench/{results,…}) so all
# harness artefacts stay under evals/logs/ instead of scattering at
# repo root. swebench writes `<model>.<run_id>.json` relative to cwd,
# so the recipe cd's here before invoking the CLI.
GRADE_FIX_REPORT_DIR   ?= evals/logs/swe_bench/results
GRADE_FIX_CACHE_LEVEL  ?= env

grade-fix: ## Run pip-installed swebench harness on remote docker against GRADE_FIX_PREDICTIONS
	@if [ -z "$(GRADE_FIX_PREDICTIONS)" ]; then \
		echo "no predictions.jsonl under evals/outputs/fix_swe_bench_verified/ — run make smoke-fix first" >&2; \
		exit 1; \
	fi
	@printf '\033[36m▶ swe-bench grade\033[0m %s\n  \033[2mDOCKER_HOST=%s run_id=%s workers=%s namespace=%s\033[0m\n' \
		"$(GRADE_FIX_PREDICTIONS)" "$(DOCKER_HOST)" "$(GRADE_FIX_RUN_ID)" "$(GRADE_FIX_MAX_WORKERS)" "$(GRADE_FIX_NAMESPACE)"
	@mkdir -p "../$(GRADE_FIX_REPORT_DIR)"
	@# Resolve the predictions path to absolute BEFORE the `cd …` below,
	@# since the recipe's working dir changes to the report dir so
	@# swebench's cwd-relative `<model>.<run_id>.json` write lands in
	@# the right place. Also forwards --report_dir (currently ignored by
	@# upstream — reporting.py:152 silently uses cwd — but kept so the
	@# flag works automatically once swebench fixes the bug). Same
	@# Doppler-bypass reasoning as grade-test.
	cd ../$(GRADE_FIX_REPORT_DIR) && $(PY) python -m swebench.harness.run_evaluation \
		--dataset_name $(GRADE_FIX_DATASET) \
		--predictions_path "$(abspath ../$(GRADE_FIX_PREDICTIONS))" \
		--max_workers $(GRADE_FIX_MAX_WORKERS) \
		--run_id $(GRADE_FIX_RUN_ID) \
		--cache_level $(GRADE_FIX_CACHE_LEVEL) \
		--namespace $(GRADE_FIX_NAMESPACE) \
		--report_dir . \
		$(if $(GRADE_FIX_INSTANCE_IDS),--instance_ids $(GRADE_FIX_INSTANCE_IDS))

writeback-fix: ## Writeback SWE harness verdicts → LangSmith swe_bench_pass_at_1 feedback
	@# Resolve paths to absolute BEFORE the `cd ..` below — otherwise the
	@# `../<report_dir>/*.json` glob (relative to evals/) gets re-resolved
	@# under repo-root, ending up one dir too high.
	@# Resolve report path BEFORE the `cd ..` below; otherwise the
	@# ../<dir>/ glob (relative to evals/) gets re-resolved one dir too
	@# high.
	@if [ -z "$(GRADE_FIX_REPORT)" ]; then \
		LATEST="$$(ls -1t ../$(GRADE_FIX_REPORT_DIR)/*.json 2>/dev/null | head -1)"; \
		if [ -z "$$LATEST" ]; then \
			echo "no $(GRADE_FIX_REPORT_DIR)/*.json — run make grade-fix first" >&2; \
			exit 1; \
		fi; \
		GRADE_FIX_REPORT="$$(cd "$$(dirname "$$LATEST")" && pwd)/$$(basename "$$LATEST")"; \
	else \
		GRADE_FIX_REPORT="$(GRADE_FIX_REPORT)"; \
	fi; \
	if [ -z "$(GRADE_FIX_EXPERIMENT)" ]; then \
		LATEST_PRED="$$(ls -1t outputs/fix_swe_bench_verified/*.predictions.jsonl 2>/dev/null | head -1)"; \
		EXP_NAME="$$(basename "$$LATEST_PRED" .predictions.jsonl)"; \
	else \
		EXP_NAME="$(GRADE_FIX_EXPERIMENT)"; \
	fi; \
	printf '\033[36m▶ swe-bench writeback\033[0m %s → experiment %s\n' "$$GRADE_FIX_REPORT" "$$EXP_NAME"; \
	cd .. && $(DOPPLER) $(PY) python -m evals.scripts.writeback_swe_grades \
		--evaluation-result "$$GRADE_FIX_REPORT" \
		--experiment-name "$$EXP_NAME" \
		$(if $(DRY_RUN),--dry-run)

eval-fix: ## Full SWE-bench pipeline: agent run → offline grading → LangSmith writeback
	@$(MAKE) smoke-fix
ifndef SKIP_GRADE
	@$(MAKE) grade-fix
	@$(MAKE) writeback-fix
else
	@echo "▸ SKIP_GRADE set — stopping after Inspect agent run"
endif

# ─── Resume a partial / errored run ─────────────────────────────
# ``inspect eval-retry`` reads a ``.eval`` log, identifies errored or
# never-completed samples, and retries just those. Samples that already
# scored cleanly are skipped — so a 50-sample run that died at 23 picks
# up at 24, and a run where 7 samples errored gets the 7 reruns.
#
# Pass ``LOG=<path>`` to resume an explicit log; the default ``resume``
# target finds the most-recent ``.eval`` under ``evals/logs/`` and
# resumes it.
RESUME_FLAGS ?= --fail-on-error=$(FAIL_ON_ERROR) --retry-on-error=$(RETRY_ON_ERROR) --attempt-timeout $(ATTEMPT_TIMEOUT) --max-samples $(MAX_SAMPLES) --log-level $(LOG_LEVEL) --display $(DISPLAY)

resume: ## Resume the most recent partial eval (override with LOG=path/to/file.eval)
ifndef LOG
	@LATEST="$$(ls -1t evals/logs/*/*.eval 2>/dev/null | head -1)"; \
	if [ -z "$$LATEST" ]; then \
		echo "no .eval logs under evals/logs/ — run a make smoke-* target first" >&2; \
		exit 1; \
	fi; \
	printf '\033[36m▶ inspect eval-retry\033[0m %s\n' "$$LATEST"; \
	cd .. && $(DOPPLER) $(INSPECT) eval-retry "evals/$$LATEST" $(RESUME_FLAGS)
else
	@printf '\033[36m▶ inspect eval-retry\033[0m %s\n' "$(LOG)"
	@cd .. && $(DOPPLER) $(INSPECT) eval-retry "$(LOG)" $(RESUME_FLAGS)
endif

resume-latest: resume ## Alias for resume — explicit "pick up where we left off"

# ─── Sandbox housekeeping ───────────────────────────────────────
# Force-remove any DockerSandboxBackend containers left behind by a crashed
# eval run. Matches strictly on our label so user-owned containers are safe.
sandbox-status: ## List eval sandbox containers (any state)
	@docker ps -a --filter "label=openbot.eval.sandbox=1" \
		--format "table {{.Names}}\t{{.Status}}\t{{.CreatedAt}}" || true

sandbox-clean: ## Force-remove all leftover eval sandbox containers
	@ids="$$(docker ps -aq --filter 'label=openbot.eval.sandbox=1' 2>/dev/null)"; \
	if [ -z "$$ids" ]; then \
		echo "no orphan sandbox containers"; \
	else \
		echo "removing $$(echo $$ids | wc -w | tr -d ' ') sandbox container(s)…"; \
		docker rm -f $$ids; \
	fi

# Reap dangling per-sample SWE-bench / SWT-bench images. The DockerSandboxBackend
# now removes its own images on aclose, but images pulled by the *Inspect*
# docker-compose sandbox path (e.g. ghcr.io/epoch-research/swe-bench.eval.*)
# linger on disk at ~3 GB apiece — a 50-sample run leaves ~150 GB if you don't
# garbage-collect. Filter is intentionally narrow (only known eval-image
# registries) so user images can't be hit by accident.
sandbox-prune-images: ## Force-remove leftover SWE-bench/SWT-bench per-sample images
	@imgs="$$( \
		docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
		| grep -E '^(ghcr\.io/epoch-research/(swe|swt)-bench\.eval\.|ghcr\.io/swt-bench/)' \
		|| true \
	)"; \
	if [ -z "$$imgs" ]; then \
		echo "no per-sample SWE/SWT bench images on disk"; \
	else \
		echo "removing $$(echo "$$imgs" | wc -l | tr -d ' ') per-sample image(s)…"; \
		echo "$$imgs" | xargs -n1 docker rmi -f; \
	fi

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

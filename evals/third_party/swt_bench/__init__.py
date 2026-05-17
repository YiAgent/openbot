"""Vendored SWT-bench harness — see NOTICE for upstream attribution.

Source: https://github.com/logic-star-ai/swt-bench @ e0a1be17 (2026-04-27)
License: MIT (see LICENSE)

Only modification beyond the relative-import rewrite below is the
``apply_ssh_patch()`` call — the harness reaches a remote Docker
daemon via ``DOCKER_HOST=ssh://…`` (see CLAUDE.md / evals/sandboxes/
docker_ssh.py for why this monkey-patch is necessary on macOS).
"""

from evals.common.docker_ssh import apply_ssh_patch

# Apply BEFORE any submodule imports docker, otherwise modules that
# already captured a reference to docker.from_env will bypass the patch.
apply_ssh_patch()

__version__ = "1.2.0"  # upstream version, kept verbatim

from .constants import (
    KEY_INSTANCE_ID,
    KEY_MODEL,
    KEY_PREDICTION,
    MAP_REPO_TO_TEST_FRAMEWORK,
    MAP_VERSION_TO_INSTALL,
    ResolvedStatus,
)
from .docker_build import (
    build_base_images,
    build_env_images,
    build_image,
    build_instance_image,
    build_instance_images,
    close_logger,
    setup_logger,
)
from .docker_utils import (
    cleanup_container,
    copy_to_container,
    exec_run_with_timeout,
    list_images,
    remove_image,
)
from .grading import (
    TestStatus,
    compute_fail_to_pass,
    compute_pass_to_pass,
    get_eval_report,
    get_logs_eval,
    get_pred_report,
    get_resolution_success,
)
from .log_parsers import (
    MAP_REPO_TO_PARSER,
)
from .main import (
    run,
)
from .utils import (
    get_environment_yml,
    get_requirements,
)

import hashlib
import json
import platform
import re
from dataclasses import asdict, dataclass
from typing import Literal

from .constants import (
    MAP_REPO_TO_INSTALL,
    MAP_REPO_TO_TEST_FRAMEWORK,
    MAP_VERSION_TO_INSTALL,
    USE_X86,
    SWEbenchInstance,
)
from .dockerfiles import (
    get_dockerfile_base,
    get_dockerfile_env,
    get_dockerfile_instance,
)
from .utils import (
    extract_changed_files,
    get_environment_yml_by_commit,
    get_requirements_by_commit,
)

DIFF_MODIFIED_FILE_REGEX = r"--- a/(.*)"

ExecMode = Literal["unit_test", "reproduction_script"]


@dataclass
class ExecSpec:
    """
    A dataclass that represents a test specification for a single instance of SWE-bench.
    """

    instance_id: str
    repo: str
    version: str
    environment_setup_commit: str
    patch_list: list[str]
    arch: str
    base_commit: str
    test_directives: list[str] | None
    coverage_files: list[str] | None
    env_name: str

    run_id: str = None
    patch_id: str = None

    timeout: float = None

    rm_image: bool = False
    force_rebuild: bool = False

    exec_mode: ExecMode = "unit_test"
    reproduction_script_name: str | None = None
    compute_coverage: bool = False

    @property
    def repo_directory(self):
        return f"/{self.env_name}"

    def as_dict(self):
        self_dict = asdict(self)
        ### add missing fields
        self_dict["install"] = self.install
        self_dict["cache_level"] = "env" if self.rm_image else "instance"
        self_dict["test_command"] = self.test_command
        self_dict["req_install_commands"] = self.req_install_commands
        return self_dict

    @property
    def test_command(self):
        trace_path = "/root/trace.py"
        changed_files_pattern = "({})".format("|".join(re.escape(x) for x in self.coverage_files))
        trace_pattern = f"python3 {trace_path} --count -C coverage.cover --include-pattern '/testbed/{changed_files_pattern}'"

        if self.exec_mode == "reproduction_script":
            reproduction_script_path = f"/testbed/{self.reproduction_script_name}"
            # executes just the reproduction script to determine the exit status
            test_command = f"python3 {reproduction_script_path}"
            if not self.compute_coverage:
                return test_command
            # executes the coverage script first to compute coverage, then the reproduction script to determine the exit status
            return f"{trace_pattern} {reproduction_script_path} && {test_command}"

        # otherwise execute the test suite command
        test_command = " ".join(
            [
                MAP_REPO_TO_TEST_FRAMEWORK[self.repo][self.version],
                *self.test_directives,
            ]
        )
        if not self.compute_coverage:
            return test_command

        cleaned_test_cmd = test_command.replace("--tb=no", "")

        if re.findall(r"python(3?) -m", cleaned_test_cmd):
            trace_test_cmd = re.subn(r"python(3?) -m", f"{trace_pattern} -m", cleaned_test_cmd, 1)
        elif (
            cleaned_test_cmd.strip().startswith("pytest")
            or cleaned_test_cmd.strip().startswith("unittest")
            or cleaned_test_cmd.strip().startswith("tox")
        ):
            trace_test_cmd = f"{trace_pattern} -m {cleaned_test_cmd}"
        elif cleaned_test_cmd.strip().startswith(
            "PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning'"
        ):
            cleaned_test_cmd = cleaned_test_cmd.strip(
                "PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning'"
            ).strip()
            trace_test_cmd = f"PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' {trace_pattern} {cleaned_test_cmd}"
        else:
            trace_test_cmd = f"{trace_pattern} {cleaned_test_cmd}"
        return trace_test_cmd

    def as_json(self):
        return json.dumps(self.as_dict())

    @property
    def install(self):
        return MAP_VERSION_TO_INSTALL[self.repo][self.version]

    @property
    def env_script(self) -> str:
        return "\n".join(["#!/bin/bash", "set -euxo pipefail"] + self.env_script_list) + "\n"

    @property
    def eval_script(self) -> str:
        return "\n".join(["#!/bin/bash", "set -uxo pipefail"] + self.eval_script_list) + "\n"
        # Don't exit early because we need to revert tests at the end

    @property
    def repo_script(self) -> str:
        return "\n".join(["#!/bin/bash", "set -euxo pipefail"] + self.repo_script_list) + "\n"

    @property
    def base_image_key(self) -> str:
        return f"exec.base.{self.arch}:latest"

    @property
    def env_image_key(self) -> str:
        """
        The key for the environment image is based on the hash of the environment script list.
        If the environment script list changes, the image will be rebuilt automatically.

        Note that old images are not automatically deleted, so consider cleaning up old images periodically.
        """

        return f"exec.env.{self.arch}.{self.env_hash}:latest"

    @property
    def instance_image_key(self) -> str:
        """
        The key for the instance image is based on the hash of the repo_setup script list.
        If the repo_setup script list changes, the image will be rebuilt automatically.

        Note that old images are not automatically deleted, so consider cleaning up old images periodically.
        """
        return f"exec.eval.{self.arch}.{self.env_hash}.{self.instance_hash}:latest"

    @property
    def env_hash(self):
        hash_object = hashlib.sha256()
        hash_object.update(self.env_script.encode("utf-8"))
        hash_value = hash_object.hexdigest()
        val = hash_value[:22]  # 22 characters is still very likely to be unique
        return val

    @property
    def instance_hash(self):
        hash_object = hashlib.sha256()
        hash_object.update(self.repo_script.encode("utf-8"))
        hash_value = hash_object.hexdigest()
        val = hash_value[:22]  # 22 characters is still very likely to be unique
        return val

    def get_instance_container_name(self):
        return f"exec.eval.{self.arch}.{self.env_hash}.{self.instance_hash}.{id(self)}"

    @property
    def base_dockerfile(self):
        return get_dockerfile_base(self.platform, self.arch)

    @property
    def env_dockerfile(self):
        return get_dockerfile_env(self.platform, self.base_image_key)

    @property
    def instance_dockerfile(self):
        return get_dockerfile_instance(self.platform, self.env_image_key)

    @property
    def platform(self):
        if self.arch == "x86_64":
            # Vendor patch: upstream emits the non-standard string
            # "linux/x86_64". Docker's manifest list spec only recognises
            # "linux/amd64"; passing "linux/x86_64" makes the daemon
            # silently fall back to the client's default arch (which is
            # arm64 on a Mac M-series), pulling an arm64 layer onto an
            # amd64 host and exploding with `exec format error` at the
            # first RUN step. This appears to work on upstream's own
            # x86_64 dev boxes because client arch already matches.
            return "linux/amd64"
        elif self.arch == "arm64":
            return "linux/arm64/v8"
        else:
            raise ValueError(f"Invalid architecture: {self.arch}")

    @property
    def repo_script_list(self):
        """
        Create a list of bash commands to set up the repository for testing.
        This is the setup script for the instance image.
        """
        setup_commands = [
            f"git clone -o origin https://github.com/{self.repo} {self.repo_directory}",
            f"chmod -R 777 {self.repo_directory}",  # So nonroot user can run tests
            f"cd {self.repo_directory}",
            f"git reset --hard {self.base_commit}",
            # Remove the remote so the agent won't see newer commits.
            "git remote remove origin",
            # Make sure conda is available for later use
            "source /opt/miniconda3/bin/activate",
            f"conda activate {self.env_name}",
            'echo "Current environment: $CONDA_DEFAULT_ENV"',
        ]
        if self.repo in MAP_REPO_TO_INSTALL:
            setup_commands.append(MAP_REPO_TO_INSTALL[self.repo])

        # Run pre-install set up if provided
        if "pre_install" in self.install:
            for pre_install in self.install["pre_install"]:
                setup_commands.append(pre_install)

        if "install" in self.install:
            setup_commands.append(self.install["install"])
        return setup_commands

    @property
    def eval_script_list(self):
        """
        Applies the test patch and runs the tests.
        """
        install = self.install
        env_name = self.env_name
        repo_directory = self.repo_directory
        base_commit = self.base_commit
        patch_list = self.patch_list

        patch_base_command = f"git diff HEAD {base_commit} >> /root/pre_state.patch"

        reset_commands = [f"git checkout {base_commit}", "git apply /root/pre_state.patch"]

        HEREDOC_DELIMITER = "EOF_114329324912"

        apply_patch_commands = [
            f"git apply -v - <<'{HEREDOC_DELIMITER}'\n{patch}\n{HEREDOC_DELIMITER}"
            for patch in patch_list
        ]

        test_command = self.test_command

        eval_commands = [
            "source /opt/miniconda3/bin/activate",
            f"conda activate {env_name}",
            f"cd {repo_directory}",
        ]

        eval_commands.append(patch_base_command)

        if "eval_commands" in install:
            eval_commands += install["eval_commands"]
        eval_commands += [
            f"git config --global --add safe.directory {repo_directory}",  # for nonroot user
            f"cd {repo_directory}",
            # This is just informational, so we have a record
            "git status",
            "git show",
            f"git diff {base_commit}",
            "source /opt/miniconda3/bin/activate",
            f"conda activate {env_name}",
        ]

        if "install" in install:
            eval_commands.append(install["install"])
        if self.exec_mode == "reproduction_script":
            exit_mode_command = ["echo $?"]
        else:
            exit_mode_command = []

        if self.compute_coverage:
            cat_coverage_commands = ["cat coverage.cover"]
        else:
            cat_coverage_commands = []
        eval_commands += (
            apply_patch_commands
            + [test_command]
            + exit_mode_command
            + cat_coverage_commands
            + reset_commands
        )

        return eval_commands

    @property
    def req_install_commands(self):
        install = self.install
        env_name = self.env_name
        HEREDOC_DELIMITER = "EOF_59812759871"

        pkgs = install.get("packages", "")
        reqs_commands = []

        if pkgs == "requirements.txt":
            # Create environment
            cmd = f"conda create -n {env_name} python={install['python']} -y"
            reqs_commands.append(cmd)

            # Install dependencies
            reqs = get_requirements_by_commit(self.repo, self.environment_setup_commit)
            path_to_reqs = "$HOME/requirements.txt"
            reqs_commands.append(
                f"cat <<'{HEREDOC_DELIMITER}' > {path_to_reqs}\n{reqs}\n{HEREDOC_DELIMITER}"
            )
            cmd = f"conda activate {env_name} && python -m pip install -r {path_to_reqs}"
            reqs_commands.append(cmd)
            reqs_commands.append(f"rm {path_to_reqs}")
        elif pkgs == "environment.yml":
            # Create environment from yml
            reqs = get_environment_yml_by_commit(
                self.repo, self.environment_setup_commit, self.env_name
            )
            path_to_reqs = "environment.yml"
            reqs_commands.append(
                f"cat <<'{HEREDOC_DELIMITER}' > {path_to_reqs}\n{reqs}\n{HEREDOC_DELIMITER}"
            )
            if install.get("no_use_env"):
                # `conda create` based installation
                cmd = f"conda create -c conda-forge -n {env_name} python={install['python']} -y"
                reqs_commands.append(cmd)

                # Install dependencies
                cmd = f"conda env update -f {path_to_reqs}"
                reqs_commands.append(cmd)
            else:
                # `conda env create` based installation
                cmd = f"conda env create --file {path_to_reqs}"
                reqs_commands.append(cmd)

                cmd = f"conda activate {env_name} && conda install python={install['python']} -y"
                reqs_commands.append(cmd)

            # Remove environment.yml
            reqs_commands.append(f"rm {path_to_reqs}")
        else:
            # Create environment + install dependencies
            cmd = f"conda create -n {env_name} python={install['python']} {pkgs} -y"
            reqs_commands.append(cmd)

        return reqs_commands

    @property
    def env_script_list(self):
        """
        Creates the list of commands to set up the conda environment for testing.
        This is the setup script for the environment image.
        """
        install = self.install
        env_name = self.env_name
        reqs_commands = [
            "source /opt/miniconda3/bin/activate",
        ]
        # Create conda environment according to install instructinos
        reqs_commands += self.req_install_commands

        reqs_commands.append(f"conda activate {env_name}")

        # Install additional packages if specified
        if "pip_packages" in install:
            pip_packages = " ".join(install["pip_packages"])
            cmd = f"python -m pip install {pip_packages}"
            reqs_commands.append(cmd)
        return reqs_commands


def get_exec_specs_from_dataset(dataset: list[SWEbenchInstance] | list[ExecSpec]) -> list[ExecSpec]:
    """
    Idempotent function that converts a list of SWEbenchInstance objects to a list of TestSpec objects.
    """
    if isinstance(dataset[0], ExecSpec):
        return dataset
    return list(map(make_exec_spec, dataset))


def make_exec_spec(
    instance: SWEbenchInstance,
    exec_mode: ExecMode = "unit_test",
    reproduction_script_name: str | None = None,
) -> ExecSpec:
    if isinstance(instance, ExecSpec):
        return instance
    instance_id = instance["instance_id"]
    repo = instance["repo"]
    version = instance["version"]
    base_commit = instance["base_commit"]
    patch_list = []
    env_name = "testbed"
    test_directives = None  # get_test_directives(instance)
    environment_setup_commit = (
        instance["environment_setup_commit"]
        if "environment_setup_commit" in instance
        else instance["base_commit"]
    )

    # Vendor patch: upstream assumes the docker daemon shares the local
    # CPU arch. With DOCKER_HOST=ssh://linux we may have an arm64 Mac
    # client talking to an x86_64 Linux daemon — building an arm64 base
    # image then runs under qemu emulation, which is buggy at `apt
    # update` time (exit 255). Resolution order, highest priority first:
    #   1. OPENBOT_SWT_ARCH env var ("x86_64" | "arm64") — explicit
    #   2. remote daemon's Architecture if DOCKER_HOST is non-local
    #   3. platform.machine() — original upstream behaviour
    import os as _os

    _forced = _os.environ.get("OPENBOT_SWT_ARCH")
    if _forced in {"x86_64", "arm64"}:
        _machine = _forced
    elif _os.environ.get("DOCKER_HOST", "").startswith(("ssh://", "tcp://")):
        try:
            import docker as _docker  # imported via vendored package init → patched for SSH

            _daemon_arch = _docker.from_env().info().get("Architecture", "")
            # docker reports "x86_64", "aarch64", "amd64", "arm64" depending on platform
            if _daemon_arch in {"x86_64", "amd64"}:
                _machine = "x86_64"
            elif _daemon_arch in {"aarch64", "arm64"}:
                _machine = "arm64"
            else:
                _machine = platform.machine()
        except Exception:
            _machine = platform.machine()
    else:
        _machine = platform.machine()

    if _machine in {"aarch64", "arm64"}:
        # use arm64 unless explicitly specified
        arch = "arm64" if instance_id not in USE_X86 else "x86_64"
    else:
        arch = "x86_64"

    changed_files = extract_changed_files(instance["golden_code_patch"])

    return ExecSpec(
        instance_id=instance_id,
        repo=repo,
        env_name=env_name,
        base_commit=base_commit,
        environment_setup_commit=environment_setup_commit,
        version=version,
        arch=arch,
        test_directives=test_directives,
        patch_list=patch_list,
        coverage_files=changed_files,
        exec_mode=exec_mode,
        reproduction_script_name=reproduction_script_name,
    )

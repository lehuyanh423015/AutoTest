"""Bounded source copying and subprocess provisioning outside an original checkout."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence

from autotest.environment_planner import (
    DependencyStrategy,
    EnvironmentPlan,
    EnvironmentPlanner,
    EnvironmentPlanStatus,
)
from autotest.errors import EnvironmentProvisionError
from autotest.project_inspector import INSPECTION_EXCLUDED_DIRS, ProjectInspector, ProjectProfile


class EnvironmentProvisionStatus(StrEnum):
    READY = "READY"
    UNSUPPORTED = "UNSUPPORTED"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    COPY_INTEGRITY_ERROR = "COPY_INTEGRITY_ERROR"
    SOURCE_INTEGRITY_ERROR = "SOURCE_INTEGRITY_ERROR"


@dataclass(frozen=True, slots=True)
class ManifestFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class CopyManifest:
    files: tuple[ManifestFile, ...]
    skipped_symlinks: tuple[str, ...]
    total_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": [
                {"path": item.path, "size": item.size, "sha256": item.sha256} for item in self.files
            ],
            "skipped_symlinks": list(self.skipped_symlinks),
            "file_count": len(self.files),
            "total_bytes": self.total_bytes,
        }


@dataclass(frozen=True, slots=True)
class EnvironmentCommandResult:
    stage: str
    argv: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool
    artifact_dir: Path | None = None


class EnvironmentCommandRunner:
    """The only Phase 5B external-command boundary; never invokes a shell."""

    def run(
        self,
        stage: str,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
        env: Mapping[str, str],
    ) -> EnvironmentCommandResult:
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                list(argv),
                cwd=cwd,
                env=dict(env),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                shell=False,
            )
            return EnvironmentCommandResult(
                stage,
                tuple(argv),
                completed.returncode,
                completed.stdout,
                completed.stderr,
                time.perf_counter() - started,
                False,
            )
        except subprocess.TimeoutExpired as exc:
            return EnvironmentCommandResult(
                stage,
                tuple(argv),
                None,
                self._text(exc.stdout),
                self._text(exc.stderr),
                time.perf_counter() - started,
                True,
            )
        except OSError as exc:
            return EnvironmentCommandResult(
                stage,
                tuple(argv),
                None,
                "",
                f"Could not start command: {exc}",
                time.perf_counter() - started,
                False,
            )

    @staticmethod
    def _text(value: str | bytes | None) -> str:
        if value is None:
            return ""
        return value.decode(errors="replace") if isinstance(value, bytes) else value


@dataclass(frozen=True, slots=True)
class TargetEnvironment:
    status: EnvironmentProvisionStatus
    workspace_root: Path | None
    source_root: Path | None
    venv_root: Path | None
    python_executable: Path | None
    python_version: str | None
    dependency_strategy: DependencyStrategy
    target_dependencies_installed: bool
    runner_tools_installed: bool
    original_integrity_verified: bool
    copy_integrity_verified: bool
    profile_copy_verified: bool
    commands: tuple[EnvironmentCommandResult, ...]
    tool_versions: Mapping[str, str]
    profile_sha256: str
    plan_sha256: str
    uv_version: str | None
    offline: bool
    copy_file_count: int
    copy_total_bytes: int
    copy_duration_seconds: float
    total_duration_seconds: float
    warnings: tuple[str, ...]
    error_message: str | None

    def to_dict(self) -> dict[str, Any]:
        durations = {item.stage: item.duration_seconds for item in self.commands}
        return {
            "schema_version": 1,
            "status": self.status.value,
            "workspace_root": str(self.workspace_root) if self.workspace_root else None,
            "source_root": str(self.source_root) if self.source_root else None,
            "venv_root": str(self.venv_root) if self.venv_root else None,
            "python_executable": str(self.python_executable) if self.python_executable else None,
            "python_version": self.python_version,
            "autotest_python_version": ".".join(str(part) for part in sys.version_info[:3]),
            "dependency_strategy": self.dependency_strategy.value,
            "target_dependencies_installed": self.target_dependencies_installed,
            "runner_tools_installed": self.runner_tools_installed,
            "original_integrity_verified": self.original_integrity_verified,
            "copy_integrity_verified": self.copy_integrity_verified,
            "profile_copy_verified": self.profile_copy_verified,
            "profile_sha256": self.profile_sha256,
            "plan_sha256": self.plan_sha256,
            "uv_version": self.uv_version,
            "offline": self.offline,
            "copy_file_count": self.copy_file_count,
            "copy_total_bytes": self.copy_total_bytes,
            "copy_duration_seconds": self.copy_duration_seconds,
            "command_durations_seconds": durations,
            "total_duration_seconds": self.total_duration_seconds,
            "tool_versions": dict(self.tool_versions),
            "commands": [
                {
                    "stage": item.stage,
                    "argv": list(item.argv),
                    "exit_code": item.exit_code,
                    "duration_seconds": item.duration_seconds,
                    "timed_out": item.timed_out,
                    "artifact_dir": str(item.artifact_dir) if item.artifact_dir else None,
                }
                for item in self.commands
            ],
            "warnings": list(self.warnings),
            "error_message": self.error_message,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"


class EnvironmentProvisioner:
    """Create one fresh copied workspace and a verified target venv."""

    def __init__(
        self,
        *,
        command_runner: EnvironmentCommandRunner | None = None,
        uv_executable: str | None = None,
        timeout: float = 600.0,
        max_files: int = 20_000,
        max_file_bytes: int = 32 * 1024 * 1024,
        max_total_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        if min(timeout, max_files, max_file_bytes, max_total_bytes) <= 0:
            raise ValueError("Provisioning limits and timeout must be positive.")
        self.runner = command_runner or EnvironmentCommandRunner()
        self.uv_executable = uv_executable
        self.timeout = timeout
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes

    def provision(
        self,
        project_root: Path | str,
        profile: ProjectProfile,
        plan: EnvironmentPlan,
        output_root: Path | str = Path("workspace/target_environments"),
    ) -> TargetEnvironment:
        """Provision only a supported, matching plan; persist all completed evidence."""
        if plan.status is EnvironmentPlanStatus.UNSUPPORTED:
            return self._unsupported(plan)
        root = Path(project_root).resolve()
        output = Path(output_root).resolve()
        if root != profile.root or plan.project_profile_sha256 != profile.sha256:
            raise EnvironmentProvisionError("Plan/profile/project root mismatch.")
        if ProjectInspector().inspect(root).sha256 != profile.sha256:
            raise EnvironmentProvisionError("Project profile changed since planning.")
        expected_plan = EnvironmentPlanner().plan(
            profile, plan.selected_python, offline=plan.offline
        )
        if expected_plan.sha256 != plan.sha256:
            raise EnvironmentProvisionError("Environment plan differs from current policy.")
        if output == root or output.is_relative_to(root):
            raise EnvironmentProvisionError(
                "Environment output must be outside the original repository."
            )
        if plan.install_target_project:
            raise EnvironmentProvisionError("Target project installation is forbidden in Phase 5B.")
        if plan.selected_python.implementation != "CPython":
            raise EnvironmentProvisionError("Selected interpreter is not CPython.")
        started = time.perf_counter()
        try:
            output.mkdir(parents=True, exist_ok=True)
            workspace = self._reserve(output)
        except OSError as exc:
            raise EnvironmentProvisionError(
                f"Could not reserve environment workspace: {exc}"
            ) from exc
        source = workspace / "source"
        venv = workspace / ".venv"
        venv_python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        commands: list[EnvironmentCommandResult] = []
        warnings: set[str] = set(plan.warnings)
        status = EnvironmentProvisionStatus.ERROR
        message: str | None = None
        original: CopyManifest | None = None
        copied = False
        profile_copied = False
        original_verified = False
        target_installed = False
        runner_installed = False
        tool_versions: dict[str, str] = {}
        uv_version: str | None = None
        copy_duration = 0.0
        file_count = total_bytes = 0
        try:
            self._write_new(workspace / "project_profile.json", profile.to_json())
            self._write_new(workspace / "environment_plan.json", plan.to_json())
            original = self._scan_manifest(root)
            file_count, total_bytes = len(original.files), original.total_bytes
            self._write_new(workspace / "original_manifest.json", self._json(original.to_dict()))
            warnings.update(
                f"Skipped original symlink: {path}" for path in original.skipped_symlinks
            )
            copying = time.perf_counter()
            source.mkdir(exist_ok=False)
            for item in original.files:
                src = root / item.path
                dest = source / item.path
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest, follow_symlinks=False)
            copy_duration = time.perf_counter() - copying
            copy_manifest = self._scan_manifest(source)
            self._write_new(workspace / "copied_manifest.json", self._json(copy_manifest.to_dict()))
            if original.files != copy_manifest.files or copy_manifest.skipped_symlinks:
                status = EnvironmentProvisionStatus.COPY_INTEGRITY_ERROR
                raise EnvironmentProvisionError("Copied source manifest differs from original.")
            copied = True
            try:
                copied_profile = ProjectInspector().inspect(source)
                profile_copied = copied_profile.sha256 == profile.sha256
                if not profile_copied:
                    warnings.add("Copied profile hash differs; manifests remain authoritative.")
            except Exception as exc:
                warnings.add(f"Copied profile could not be inspected: {exc.__class__.__name__}")

            uv = self.uv_executable or shutil.which("uv")
            if not uv:
                raise EnvironmentProvisionError("uv CLI is unavailable.")
            env = self._command_environment()
            result = self._command(workspace, commands, "uv-version", [uv, "--version"], env)
            uv_version = result.stdout.strip()
            self._command(
                workspace,
                commands,
                "create-venv",
                [str(plan.selected_python.executable), "-m", "venv", str(venv)],
                env,
            )
            if not venv_python.is_file():
                raise EnvironmentProvisionError("Virtualenv Python executable was not created.")
            install = [
                uv,
                "pip",
                "install",
                "--no-config",
                "--no-python-downloads",
                "--no-build",
                "--python",
                str(venv_python),
            ]
            if plan.offline:
                install.append("--offline")
            if plan.dependency_strategy is not DependencyStrategy.NONE:
                self._command(
                    workspace,
                    commands,
                    "install-target-dependencies",
                    [*install, *plan.dependencies],
                    env,
                )
            target_installed = True
            self._command(
                workspace,
                commands,
                "install-runner-tools",
                [*install, *plan.runner_requirements],
                env,
            )
            runner_installed = True
            verification_script = (
                "import importlib.metadata as m,json,platform; "
                "print(json.dumps({'python':platform.python_version(), "
                "'pytest':m.version('pytest'), 'coverage':m.version('coverage'), "
                "'pytest-timeout':m.version('pytest-timeout')}))"
            )
            verified = self._command(
                workspace,
                commands,
                "verify-tools",
                [str(venv_python), "-I", "-c", verification_script],
                env,
            )
            try:
                tool_versions = json.loads(verified.stdout.strip())
            except (ValueError, TypeError) as exc:
                raise EnvironmentProvisionError("Tool verification returned invalid JSON.") from exc
            expected = {"python": plan.selected_python.version}
            expected.update(dict(item.split("==", 1) for item in plan.runner_requirements))
            if tool_versions != expected:
                raise EnvironmentProvisionError(f"Tool versions differ from plan: {tool_versions}")
            status = EnvironmentProvisionStatus.READY
        except _CommandTimeout as exc:
            status = EnvironmentProvisionStatus.TIMEOUT
            message = str(exc)
        except EnvironmentProvisionError as exc:
            message = str(exc)
        except (OSError, ValueError) as exc:
            message = f"Provisioning failed: {exc}"
        finally:
            if original is not None:
                try:
                    original_verified = self._scan_manifest(root) == original
                except (EnvironmentProvisionError, OSError):
                    original_verified = False
                if not original_verified:
                    status = EnvironmentProvisionStatus.SOURCE_INTEGRITY_ERROR
                    message = "Original repository changed during provisioning."
            environment = TargetEnvironment(
                status=status,
                workspace_root=workspace,
                source_root=source if source.is_dir() else None,
                venv_root=venv if venv.is_dir() else None,
                python_executable=venv_python if venv_python.is_file() else None,
                python_version=plan.selected_python.version,
                dependency_strategy=plan.dependency_strategy,
                target_dependencies_installed=target_installed,
                runner_tools_installed=runner_installed,
                original_integrity_verified=original_verified,
                copy_integrity_verified=copied,
                profile_copy_verified=profile_copied,
                commands=tuple(commands),
                tool_versions=tool_versions,
                profile_sha256=profile.sha256,
                plan_sha256=plan.sha256,
                uv_version=uv_version,
                offline=plan.offline,
                copy_file_count=file_count,
                copy_total_bytes=total_bytes,
                copy_duration_seconds=copy_duration,
                total_duration_seconds=time.perf_counter() - started,
                warnings=tuple(sorted(warnings)),
                error_message=message,
            )
            self._write_new(workspace / "provision_result.json", environment.to_json())
        return environment

    def _command(
        self,
        workspace: Path,
        history: list[EnvironmentCommandResult],
        stage: str,
        argv: Sequence[str],
        env: Mapping[str, str],
    ) -> EnvironmentCommandResult:
        result = self.runner.run(stage, argv, cwd=workspace, timeout=self.timeout, env=env)
        directory = workspace / "commands" / f"{len(history) + 1:03d}-{stage}"
        directory.mkdir(parents=True, exist_ok=False)
        self._write_new(directory / "stdout.txt", result.stdout)
        self._write_new(directory / "stderr.txt", result.stderr)
        self._write_new(
            directory / "result.json",
            self._json(
                {
                    "stage": stage,
                    "argv": list(result.argv),
                    "exit_code": result.exit_code,
                    "duration_seconds": result.duration_seconds,
                    "timed_out": result.timed_out,
                }
            ),
        )
        recorded = EnvironmentCommandResult(
            result.stage,
            result.argv,
            result.exit_code,
            result.stdout,
            result.stderr,
            result.duration_seconds,
            result.timed_out,
            directory,
        )
        history.append(recorded)
        if result.timed_out:
            raise _CommandTimeout(f"{stage} timed out after {self.timeout:g} seconds.")
        if result.exit_code != 0:
            raise EnvironmentProvisionError(f"{stage} failed (exit {result.exit_code}).")
        return recorded

    def _scan_manifest(self, root: Path) -> CopyManifest:
        files: list[ManifestFile] = []
        skipped: list[str] = []
        total = 0
        stack = [root]
        while stack:
            directory = stack.pop()
            try:
                children = sorted(directory.iterdir(), key=lambda item: item.name)
            except OSError as exc:
                raise EnvironmentProvisionError(f"Cannot list repository directory: {exc}") from exc
            for path in children:
                relative = path.relative_to(root).as_posix()
                if self._is_link(path):
                    skipped.append(relative)
                elif path.is_dir():
                    if path.name not in INSPECTION_EXCLUDED_DIRS:
                        stack.append(path)
                elif path.is_file():
                    try:
                        size = path.stat().st_size
                    except OSError as exc:
                        raise EnvironmentProvisionError(
                            f"Cannot stat source file: {relative}"
                        ) from exc
                    if size > self.max_file_bytes:
                        raise EnvironmentProvisionError(
                            f"Individual file-size limit exceeded: {relative}"
                        )
                    total += size
                    if total > self.max_total_bytes:
                        raise EnvironmentProvisionError("Total copy-size limit exceeded.")
                    if len(files) >= self.max_files:
                        raise EnvironmentProvisionError("Copy file-count limit exceeded.")
                    files.append(ManifestFile(relative, size, self._sha256(path)))
                else:
                    raise EnvironmentProvisionError(f"Unsupported filesystem object: {relative}")
        return CopyManifest(
            tuple(sorted(files, key=lambda item: item.path)), tuple(sorted(skipped)), total
        )

    @staticmethod
    def _is_link(path: Path) -> bool:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise EnvironmentProvisionError(f"Could not hash source file: {path}") from exc
        return digest.hexdigest()

    @staticmethod
    def _reserve(output: Path) -> Path:
        for _ in range(10):
            name = (
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
            )
            path = output / name
            try:
                path.mkdir(exist_ok=False)
                return path
            except FileExistsError:
                continue
        raise EnvironmentProvisionError("Could not allocate a fresh environment directory.")

    @staticmethod
    def _write_new(path: Path, content: str) -> None:
        with path.open("x", encoding="utf-8", newline="\n") as destination:
            destination.write(content)

    @staticmethod
    def _json(payload: Mapping[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    @staticmethod
    def _command_environment() -> dict[str, str]:
        allowed = {
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "TMPDIR",
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "LANG",
            "LC_ALL",
            "UV_CACHE_DIR",
        }
        result = {name: value for name, value in os.environ.items() if name.upper() in allowed}
        result["PYTHONNOUSERSITE"] = "1"
        result["UV_PYTHON_DOWNLOADS"] = "never"
        return result

    @staticmethod
    def _unsupported(plan: EnvironmentPlan) -> TargetEnvironment:
        return TargetEnvironment(
            EnvironmentProvisionStatus.UNSUPPORTED,
            None,
            None,
            None,
            None,
            plan.selected_python.version,
            plan.dependency_strategy,
            False,
            False,
            False,
            False,
            False,
            (),
            {},
            plan.project_profile_sha256,
            plan.sha256,
            None,
            plan.offline,
            0,
            0,
            0.0,
            0.0,
            (),
            "; ".join(plan.unsupported_reasons),
        )


class _CommandTimeout(EnvironmentProvisionError):
    """Internal marker for a bounded external command timeout."""

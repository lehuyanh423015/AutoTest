"""Pure, conservative planning for isolated target Python environments."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from autotest.errors import EnvironmentPlanningError
from autotest.project_inspector import ProjectProfile

RUNNER_REQUIREMENTS = ("pytest==8.4.2", "coverage==7.16.0", "pytest-timeout==2.4.0")
_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){1,2}$")
_SPEC = re.compile(r"^(>=|<=|==|!=|~=|>|<)\s*([0-9]+(?:\.[0-9]+){1,2}(?:\.\*)?)$")
_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9_,.-]+\])?"
_ATOM = r"(?:===|==|~=|!=|<=|>=|<|>)\s*[A-Za-z0-9*_.+-]+"
_REGISTRY = re.compile(rf"^{_NAME}(?:\s*{_ATOM}(?:\s*,\s*{_ATOM})*)?$", re.ASCII)
_MARKER = re.compile(r"^[A-Za-z0-9_ .<>=!~'\"(),-]+$", re.ASCII)


class EnvironmentPlanStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"


class PythonCompatibility(StrEnum):
    COMPATIBLE = "COMPATIBLE"
    INCOMPATIBLE = "INCOMPATIBLE"
    UNKNOWN = "UNKNOWN"


class DependencyStrategy(StrEnum):
    NONE = "NONE"
    REQUIREMENTS = "REQUIREMENTS"
    PYPROJECT_DECLARATIONS = "PYPROJECT_DECLARATIONS"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class InterpreterInfo:
    executable: Path
    version: str
    implementation: str


@dataclass(frozen=True, slots=True)
class EnvironmentPlan:
    project_profile_sha256: str
    project_name: str
    status: EnvironmentPlanStatus
    selected_python: InterpreterInfo
    python_requirement: str | None
    compatibility: PythonCompatibility
    dependency_strategy: DependencyStrategy
    dependency_sources: tuple[str, ...]
    dependencies: tuple[str, ...]
    runner_requirements: tuple[str, ...]
    lock_files: tuple[str, ...]
    network_may_be_required: bool
    offline: bool
    install_target_project: bool
    warnings: tuple[str, ...]
    unsupported_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": 1,
            "project_profile_sha256": self.project_profile_sha256,
            "project_name": self.project_name,
            "status": self.status.value,
            "selected_python": {
                "executable": str(self.selected_python.executable),
                "version": self.selected_python.version,
                "implementation": self.selected_python.implementation,
            },
            "python_requirement": self.python_requirement,
            "compatibility": self.compatibility.value,
            "dependency_strategy": self.dependency_strategy.value,
            "dependency_sources": list(self.dependency_sources),
            "dependencies": list(self.dependencies),
            "runner_requirements": list(self.runner_requirements),
            "lock_files": list(self.lock_files),
            "network_may_be_required": self.network_may_be_required,
            "offline": self.offline,
            "install_target_project": self.install_target_project,
            "warnings": list(self.warnings),
            "unsupported_reasons": list(self.unsupported_reasons),
        }
        normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload["plan_sha256"] = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"

    @property
    def sha256(self) -> str:
        return self.to_dict()["plan_sha256"]


def probe_interpreter(
    executable: Path | str | None = None, *, timeout: float = 10.0
) -> InterpreterInfo:
    """Execute only the chosen interpreter to obtain its identity."""
    path = Path(executable or sys.executable).resolve()
    if not path.is_file():
        raise EnvironmentPlanningError(f"Python interpreter does not exist: {path}")
    script = (
        "import json,platform,sys; "
        "print(json.dumps({'version': platform.python_version(), "
        "'implementation': platform.python_implementation(), 'executable': sys.executable}))"
    )
    try:
        result = subprocess.run(
            [str(path), "-I", "-c", script],
            cwd=path.parent,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
        if result.returncode != 0:
            raise EnvironmentPlanningError(f"Selected Python probe failed: {path}")
        data = json.loads(result.stdout)
        version = data["version"]
        implementation = data["implementation"]
        if not isinstance(version, str) or not _VERSION.fullmatch(version):
            raise ValueError("invalid version")
        if not isinstance(implementation, str) or not implementation:
            raise ValueError("invalid implementation")
        return InterpreterInfo(path, version, implementation)
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise EnvironmentPlanningError(f"Could not inspect selected Python {path}: {exc}") from exc


def check_python_compatibility(requirement: str | None, version: str) -> PythonCompatibility:
    """Check a documented subset of PEP 440 specifiers without guessing on unknown syntax."""
    if requirement is None:
        return PythonCompatibility.UNKNOWN
    if not _VERSION.fullmatch(version):
        return PythonCompatibility.UNKNOWN
    selected = tuple(int(part) for part in version.split("."))
    selected += (0,) * (3 - len(selected))
    for token in requirement.split(","):
        match = _SPEC.fullmatch(token.strip())
        if match is None:
            return PythonCompatibility.UNKNOWN
        operator, raw = match.groups()
        wildcard = raw.endswith(".*")
        if wildcard:
            if operator not in {"==", "!="}:
                return PythonCompatibility.UNKNOWN
            parts = tuple(int(part) for part in raw[:-2].split("."))
            matched = selected[: len(parts)] == parts
        else:
            parts = tuple(int(part) for part in raw.split("."))
            padded = parts + (0,) * (3 - len(parts))
            if operator == "~=" and len(parts) < 2:
                return PythonCompatibility.UNKNOWN
            matched = {
                ">=": selected >= padded,
                ">": selected > padded,
                "<=": selected <= padded,
                "<": selected < padded,
                "==": selected == padded,
                "!=": selected != padded,
            }.get(operator, True)
            if operator == "~=":
                upper_parts = list(parts[:-1])
                upper_parts[-1] += 1
                upper = tuple(upper_parts) + (0,) * (3 - len(upper_parts))
                matched = selected >= padded and selected < upper
        if operator == "!=" and wildcard:
            matched = not matched
        if not matched:
            return PythonCompatibility.INCOMPATIBLE
    return PythonCompatibility.COMPATIBLE


def safe_registry_requirement(value: str) -> bool:
    """Accept only a small registry-specifier subset, retaining valid marker text."""
    if not value or len(value) > 4096 or "\n" in value or "\r" in value:
        return False
    if any(marker in value.lower() for marker in ("git+", "http://", "https://", "file:")):
        return False
    base, separator, marker = value.partition(";")
    if any(char in base for char in ("@", "/", "\\", "#")):
        return False
    if _REGISTRY.fullmatch(base.strip()) is None:
        return False
    return not separator or bool(marker.strip() and _MARKER.fullmatch(marker.strip()))


class EnvironmentPlanner:
    """Choose support and exact dependency inputs from a Phase 5A profile."""

    def plan(
        self, profile: ProjectProfile, interpreter: InterpreterInfo, *, offline: bool = False
    ) -> EnvironmentPlan:
        reasons: set[str] = set()
        warnings: set[str] = set()
        if not profile.is_python_project:
            reasons.add("No Python project evidence was found.")
        if interpreter.implementation != "CPython":
            reasons.add("Only CPython interpreters are supported in Phase 5B V1.")
        if len({item.value for item in profile.python_requirements}) > 1:
            reasons.add("Conflicting Python requirements were declared.")
        compatibility = check_python_compatibility(profile.python_requirement, interpreter.version)
        if profile.python_requirement is None and not profile.python_requirements:
            warnings.add("No Python compatibility requirement was declared.")
        elif compatibility is PythonCompatibility.UNKNOWN:
            reasons.add("Python requirement could not be checked conservatively.")
        elif compatibility is PythonCompatibility.INCOMPATIBLE:
            reasons.add("Selected Python does not satisfy the declared requirement.")

        manager_names = {item.rsplit(": ", 1)[-1] for item in profile.package_manager_evidence}
        if len(manager_names) > 1:
            reasons.add("Conflicting dependency-manager ecosystems were detected.")
        if profile.package_manager in {"poetry", "pipenv"}:
            reasons.add(f"{profile.package_manager} provisioning is not supported in V1.")
        if any(
            path.name in {"poetry.lock", "Pipfile", "Pipfile.lock"}
            for path in (*profile.lock_files, *profile.dependency_files)
        ):
            reasons.add("Poetry/Pipenv declarations or lock replay are not supported in V1.")
        if any("dynamic" in warning or "unresolved" in warning for warning in profile.warnings):
            reasons.add("Dynamic or unresolved dependency metadata requires manual review.")
        if any(path.name == "uv.lock" for path in profile.lock_files):
            warnings.add(
                "uv.lock detected; exact locked dependency replay is not implemented in V1."
            )

        runtime = tuple(item for item in profile.dependencies if item.group == "runtime")
        pyproject = tuple(item for item in runtime if item.source_file.name == "pyproject.toml")
        requirement_files = tuple(
            item for item in runtime if item.source_file.name.lower().startswith("requirements")
        )
        legacy = tuple(
            item for item in runtime if item not in pyproject and item not in requirement_files
        )
        if legacy:
            reasons.add("Runtime dependencies from setup.cfg/setup.py are not supported in V1.")
        if pyproject and requirement_files:
            reasons.add(
                "Both pyproject and requirements runtime declarations exist; "
                "no precedence policy is defined."
            )
        if any(path.name == "Pipfile" for path in profile.dependency_files):
            reasons.add("Pipfile declarations are detection-only in Phase 5A.")
        selected = pyproject if pyproject else requirement_files
        for item in selected:
            if not safe_registry_requirement(item.value):
                source = item.source_file.relative_to(profile.root).as_posix()
                reasons.add(f"Unsafe or unsupported dependency: {source}: {item.value}")
        if requirement_files:
            strategy = DependencyStrategy.REQUIREMENTS
        elif pyproject:
            strategy = DependencyStrategy.PYPROJECT_DECLARATIONS
        elif reasons and legacy:
            strategy = DependencyStrategy.UNSUPPORTED
        else:
            strategy = DependencyStrategy.NONE
        if reasons:
            strategy = DependencyStrategy.UNSUPPORTED
        dependencies = tuple(sorted(item.value for item in selected))
        sources = tuple(
            sorted({item.source_file.relative_to(profile.root).as_posix() for item in selected})
        )
        return EnvironmentPlan(
            project_profile_sha256=profile.sha256,
            project_name=profile.project_name,
            status=EnvironmentPlanStatus.UNSUPPORTED
            if reasons
            else EnvironmentPlanStatus.SUPPORTED,
            selected_python=interpreter,
            python_requirement=profile.python_requirement,
            compatibility=compatibility,
            dependency_strategy=strategy,
            dependency_sources=sources,
            dependencies=dependencies,
            runner_requirements=RUNNER_REQUIREMENTS,
            lock_files=tuple(
                sorted(path.relative_to(profile.root).as_posix() for path in profile.lock_files)
            ),
            network_may_be_required=not offline,
            offline=offline,
            install_target_project=False,
            warnings=tuple(sorted(warnings)),
            unsupported_reasons=tuple(sorted(reasons)),
        )

"""Versioned benchmark definitions and deterministic repository-run planning."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from autotest.context_selector import (
    ContextSelectionError,
    ContextSelectionPolicy,
    ContextSelector,
    ContextTarget,
)
from autotest.environment_planner import (
    DependencyStrategy,
    EnvironmentPlanner,
    EnvironmentPlanStatus,
    probe_interpreter,
)
from autotest.project_inspector import ProjectInspector
from autotest.repository_runner import RepositoryRunOptions


class ExperimentError(Exception):
    """Invalid definition, identity mismatch, or experiment infrastructure error."""


_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
CONFIGURATIONS = ("direct", "execution", "coverage", "mutation")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ExperimentError(f"{field} must be a stable ID using letters, digits, - or _")
    return value


def _positive(value: Any, field: str, *, zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if zero else 1):
        raise ExperimentError(f"{field} must be a {'nonnegative' if zero else 'positive'} integer")
    return value


@dataclass(frozen=True, slots=True)
class BenchmarkTarget:
    id: str
    target: str


@dataclass(frozen=True, slots=True)
class BenchmarkRepository:
    id: str
    path: Path
    manifest_path: str
    targets: tuple[BenchmarkTarget, ...]
    revision: str | None = None


@dataclass(frozen=True, slots=True)
class ExperimentConfiguration:
    id: str

    def options(self, definition: ExperimentDefinition, root: Path) -> RepositoryRunOptions:
        repair = 0 if self.id == "direct" else definition.max_repair_attempts
        coverage = definition.max_coverage_rounds if self.id in {"coverage", "mutation"} else 0
        mutation_feedback = self.id == "mutation"
        return RepositoryRunOptions(
            output_root=root / "repository_runs",
            environment_output_root=root / "target_environments",
            target_python=definition.target_python,
            environment_offline=definition.environment_offline,
            environment_timeout=definition.environment_timeout,
            test_timeout=definition.test_timeout,
            max_repair_attempts=repair,
            max_coverage_rounds=coverage,
            coverage_target=definition.coverage_target,
            context_policy=ContextSelectionPolicy(
                definition.context_max_chars,
                definition.context_max_files,
                definition.context_max_items,
                definition.context_max_depth,
            ),
            mutation=definition.evaluate_mutation or mutation_feedback,
            mutation_feedback=mutation_feedback,
            mutation_timeout=definition.mutation_timeout,
            mutation_venv=definition.mutation_venv,
            max_mutation_rounds=definition.max_mutation_rounds,
            max_mutants_per_round=definition.max_mutants_per_round,
        )


@dataclass(frozen=True, slots=True)
class ExperimentDefinition:
    schema_version: int
    experiment_name: str
    repositories: tuple[BenchmarkRepository, ...]
    configurations: tuple[ExperimentConfiguration, ...]
    repetitions: int
    model: str
    temperature: float
    evaluate_mutation: bool = False
    fail_fast: bool = False
    max_repair_attempts: int = 3
    max_coverage_rounds: int = 3
    coverage_target: float = 100.0
    max_mutation_rounds: int = 3
    max_mutants_per_round: int = 5
    context_max_chars: int = 32_000
    context_max_files: int = 8
    context_max_items: int = 24
    context_max_depth: int = 2
    target_python: Path | None = None
    environment_offline: bool = False
    environment_timeout: float = 600.0
    test_timeout: float = 30.0
    mutation_timeout: float = 300.0
    mutation_venv: str = "/home/ubuntu/autotest-mutation-env"

    @property
    def identity(self) -> dict[str, Any]:
        value = asdict(self)
        for repository in value["repositories"]:
            repository["path"] = repository.pop("manifest_path")
        value["target_python"] = str(self.target_python) if self.target_python else None
        return value

    @property
    def sha256(self) -> str:
        return _hash(self.identity)


def load_definition(path: Path) -> ExperimentDefinition:
    """Parse JSON strictly; relative benchmark paths belong to the manifest."""
    try:
        manifest = Path(path).resolve(strict=True)
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ExperimentError("Experiment manifest must be a JSON object")
        allowed = {field.name for field in ExperimentDefinition.__dataclass_fields__.values()}
        if set(raw) - allowed:
            raise ExperimentError(f"Unknown experiment fields: {sorted(set(raw) - allowed)}")
        if raw.get("schema_version") != 1:
            raise ExperimentError("Unsupported experiment schema_version")
        name = _name(raw.get("experiment_name"), "experiment_name")
        repetitions = _positive(raw.get("repetitions"), "repetitions")
        model = raw.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ExperimentError("model is required")
        temperature = raw.get("temperature", 0.0)
        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not math.isfinite(temperature)
            or temperature < 0
        ):
            raise ExperimentError("temperature must be a nonnegative finite number")
        repositories = []
        seen_repositories: set[str] = set()
        seen_targets: set[str] = set()
        for entry in raw.get("repositories", []):
            repo_id = _name(entry.get("id"), "repository id")
            if repo_id in seen_repositories:
                raise ExperimentError(f"Duplicate repository ID: {repo_id}")
            seen_repositories.add(repo_id)
            raw_path = entry.get("path")
            if not isinstance(raw_path, str) or not raw_path:
                raise ExperimentError("Repository path is required")
            targets = []
            for item in entry.get("targets", []):
                target_id = _name(item.get("id"), "target id")
                if target_id in seen_targets:
                    raise ExperimentError(f"Duplicate target ID: {target_id}")
                seen_targets.add(target_id)
                ContextTarget.parse(item.get("target", ""))
                targets.append(BenchmarkTarget(target_id, item["target"]))
            if not targets:
                raise ExperimentError(f"Repository {repo_id} has no targets")
            repository_path = Path(raw_path)
            repositories.append(
                BenchmarkRepository(
                    repo_id,
                    (manifest.parent / repository_path).resolve()
                    if not repository_path.is_absolute()
                    else repository_path.resolve(),
                    raw_path.replace("\\", "/"),
                    tuple(targets),
                    entry.get("revision"),
                )
            )
        if not repositories:
            raise ExperimentError("At least one repository is required")
        configs = []
        for item in raw.get("configurations", []):
            if (
                not isinstance(item, str)
                or item not in CONFIGURATIONS
                or item in [x.id for x in configs]
            ):
                raise ExperimentError(f"Unknown or duplicate configuration: {item}")
            configs.append(ExperimentConfiguration(item))
        if not configs:
            raise ExperimentError("At least one configuration is required")
        kwargs = {
            key: value
            for key, value in raw.items()
            if key
            not in {
                "schema_version",
                "experiment_name",
                "repositories",
                "configurations",
                "repetitions",
                "model",
                "temperature",
            }
        }
        for key in ("evaluate_mutation", "fail_fast", "environment_offline"):
            if key in kwargs and not isinstance(kwargs[key], bool):
                raise ExperimentError(f"{key} must be boolean")
        for key in (
            "max_repair_attempts",
            "max_coverage_rounds",
            "max_mutation_rounds",
            "context_max_depth",
        ):
            if key in kwargs:
                _positive(kwargs[key], key, zero=True)
        for key in (
            "max_mutants_per_round",
            "context_max_chars",
            "context_max_files",
            "context_max_items",
        ):
            if key in kwargs:
                _positive(kwargs[key], key)
        for key in ("coverage_target", "environment_timeout", "test_timeout", "mutation_timeout"):
            if key in kwargs and (
                isinstance(kwargs[key], bool)
                or not isinstance(kwargs[key], (int, float))
                or not math.isfinite(kwargs[key])
                or kwargs[key] <= 0
            ):
                raise ExperimentError(f"{key} must be a positive finite number")
        if kwargs.get("coverage_target", 100.0) > 100:
            raise ExperimentError("coverage_target must be at most 100")
        selected = {item.id for item in configs}
        if (
            selected & {"execution", "coverage", "mutation"}
            and kwargs.get("max_repair_attempts", 3) == 0
        ):
            raise ExperimentError("Repair feedback configurations require max_repair_attempts > 0")
        if selected & {"coverage", "mutation"} and kwargs.get("max_coverage_rounds", 3) == 0:
            raise ExperimentError(
                "Coverage feedback configurations require max_coverage_rounds > 0"
            )
        if "mutation" in selected and kwargs.get("max_mutation_rounds", 3) == 0:
            raise ExperimentError("Mutation feedback requires max_mutation_rounds > 0")
        if "target_python" in kwargs and kwargs["target_python"] is not None:
            python = Path(kwargs["target_python"])
            kwargs["target_python"] = (
                (manifest.parent / python).resolve()
                if not python.is_absolute()
                else python.resolve()
            )
        return ExperimentDefinition(
            1,
            name,
            tuple(repositories),
            tuple(configs),
            repetitions,
            model,
            float(temperature),
            **kwargs,
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ContextSelectionError) as exc:
        raise ExperimentError(f"Invalid experiment manifest: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ExperimentRunSpec:
    run_id: str
    repository_id: str
    target_id: str
    target: str
    configuration: str
    repetition: int
    model: str
    temperature: float
    profile_sha256: str | None
    support_status: str
    support_reason: str | None
    options: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ExperimentPlan:
    definition_sha256: str
    specs: tuple[ExperimentRunSpec, ...]
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "definition_sha256": self.definition_sha256,
            "planned_run_count": len(self.specs),
            "specs": [asdict(spec) for spec in self.specs],
            "plan_sha256": self.sha256,
        }


def plan_experiment(definition: ExperimentDefinition) -> ExperimentPlan:
    """Static preflight only: no venv, pytest, package install, or LLM call."""
    preflight: dict[tuple[str, str, str], tuple[str | None, str, str | None]] = {}
    inspector = ProjectInspector()
    selector = ContextSelector()
    try:
        interpreter = probe_interpreter(definition.target_python)
    except Exception as exc:
        raise ExperimentError(f"Target Python probe failed: {exc}") from exc
    for repository in sorted(definition.repositories, key=lambda item: item.id):
        profile = None
        reason = None
        try:
            profile = inspector.inspect(repository.path)
            plan = EnvironmentPlanner().plan(
                profile, interpreter, offline=definition.environment_offline
            )
        except Exception as exc:
            reason = str(exc)
        for target in sorted(repository.targets, key=lambda item: item.id):
            for configuration in sorted(
                definition.configurations, key=lambda item: CONFIGURATIONS.index(item.id)
            ):
                status = "SUPPORTED"
                problem = reason
                if profile is not None and problem is None:
                    try:
                        parsed = ContextTarget.parse(target.target)
                        matches = [
                            module
                            for module in profile.modules
                            if module.path.relative_to(profile.root) == parsed.file
                            and not module.is_test_module
                            and module.module_name
                        ]
                        if (
                            len(matches) != 1
                            or sum(
                                item.module_name == matches[0].module_name
                                for item in profile.modules
                            )
                            != 1
                        ):
                            raise ExperimentError("No unambiguous production module")
                        bundle = selector.select(
                            repository.path,
                            profile,
                            parsed,
                            ContextSelectionPolicy(
                                definition.context_max_chars,
                                definition.context_max_files,
                                definition.context_max_items,
                                definition.context_max_depth,
                            ),
                        )
                        if plan.status is EnvironmentPlanStatus.UNSUPPORTED:
                            raise ExperimentError("; ".join(plan.unsupported_reasons))
                        if configuration.id == "mutation" or definition.evaluate_mutation:
                            if plan.dependency_strategy is not DependencyStrategy.NONE or any(
                                item.classification != "STDLIB"
                                for item in bundle.external_references
                            ):
                                raise ExperimentError(
                                    "Repository mutation unsupported for non-local dependencies"
                                )
                    except Exception as exc:
                        problem = str(exc)
                if problem is not None:
                    status = "UNSUPPORTED"
                preflight[repository.id, target.id, configuration.id] = (
                    profile.sha256 if profile else None,
                    status,
                    problem,
                )
    specs = []
    for repository in sorted(definition.repositories, key=lambda item: item.id):
        for target in sorted(repository.targets, key=lambda item: item.id):
            for repetition in range(1, definition.repetitions + 1):
                for configuration in sorted(
                    definition.configurations, key=lambda item: CONFIGURATIONS.index(item.id)
                ):
                    profile_hash, status, reason = preflight[
                        repository.id, target.id, configuration.id
                    ]
                    options = configuration.options(definition, Path("<experiment-root>"))
                    settings = asdict(options)
                    settings["output_root"] = "repository_runs"
                    settings["environment_output_root"] = "target_environments"
                    settings["target_python"] = (
                        str(options.target_python) if options.target_python else None
                    )
                    specs.append(
                        ExperimentRunSpec(
                            f"{repository.id}::{target.id}::{configuration.id}::{repetition}",
                            repository.id,
                            target.id,
                            target.target,
                            configuration.id,
                            repetition,
                            definition.model,
                            definition.temperature,
                            profile_hash,
                            status,
                            reason,
                            settings,
                        )
                    )
    normalized = [asdict(spec) for spec in specs]
    return ExperimentPlan(
        definition.sha256,
        tuple(specs),
        _hash({"definition_sha256": definition.sha256, "specs": normalized}),
    )

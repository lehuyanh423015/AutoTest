"""Repository-scale pilot over a verified Phase 5B copy and Phase 5C evidence."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from autotest.artifact_store import ArtifactStore
from autotest.context_selector import (
    ContextBundle,
    ContextSelectionError,
    ContextSelectionPolicy,
    ContextSelector,
    ContextTarget,
)
from autotest.coverage_engine import CoverageEngine, CoverageStopReason
from autotest.coverage_runner import CoverageResult, CoverageRunner
from autotest.environment_planner import (
    DependencyStrategy,
    EnvironmentPlanner,
    EnvironmentPlanStatus,
    probe_interpreter,
)
from autotest.environment_provisioner import (
    EnvironmentProvisioner,
    EnvironmentProvisionStatus,
)
from autotest.errors import AutoTestError, EnvironmentProvisionError, GenerationError, LLMError
from autotest.llm.base import LLMProvider
from autotest.mutation_feedback import MutationFeedbackEngine, MutationFeedbackStopReason
from autotest.mutation_runner import DEFAULT_MUTATION_VENV, MutationStatus
from autotest.project_analyzer import ProjectAnalyzer
from autotest.project_inspector import ProjectInspector
from autotest.repair_engine import RepairEngine, StopReason
from autotest.repository_execution import RepositoryExecutionContext
from autotest.repository_mutation import RepositoryWSLMutmutBackend
from autotest.repository_prompts import RepositoryPromptBuilder
from autotest.test_runner import TestRunner, TestRunResult, TestStatus


class RepositoryRunError(Exception):
    """Controlled repository preflight or pipeline error."""


class RepositoryIntegrityError(RepositoryRunError):
    """Prepared source or previously accepted tests changed during execution."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class RepositoryRunOptions:
    output_root: Path = Path("workspace/repository_runs")
    environment_output_root: Path = Path("workspace/target_environments")
    target_python: Path | None = None
    environment_offline: bool = False
    environment_timeout: float = 600.0
    test_timeout: float = 30.0
    max_repair_attempts: int = 3
    max_coverage_rounds: int = 3
    coverage_target: float = 100.0
    context_policy: ContextSelectionPolicy = ContextSelectionPolicy()
    mutation: bool = False
    mutation_feedback: bool = False
    mutation_timeout: float = 300.0
    mutation_venv: str = DEFAULT_MUTATION_VENV
    max_mutation_rounds: int = 3
    max_mutants_per_round: int = 5


@dataclass(frozen=True, slots=True)
class RepositoryRunResult:
    project_name: str | None
    inspection_status: str
    target: ContextTarget
    module_name: str | None
    run_directory: Path
    project_profile_sha256: str | None
    environment_plan_sha256: str | None
    environment_plan_status: str | None
    context_bundle_sha256: str | None
    context_status: str | None
    context_chars: int
    context_files: int
    context_items: int
    unresolved_references: int
    omitted_items: int
    context_warnings: int
    environment_status: str | None
    environment_workspace: Path | None
    target_python: Path | None
    initial_execution_status: TestStatus | None
    final_execution_status: TestStatus | None
    repair_count: int
    repair_success: bool
    initial_line_coverage: float | None
    final_line_coverage: float | None
    initial_branch_coverage: float | None
    final_branch_coverage: float | None
    coverage_rounds: int
    accepted_coverage_rounds: int
    mutation_enabled: bool
    mutation_feedback_enabled: bool
    mutation_score_initial: float | None
    mutation_score_final: float | None
    mutation_total: int | None
    mutation_killed: int | None
    mutation_survived: int | None
    accepted_test_files: tuple[str, ...]
    source_integrity_verified: bool
    accepted_test_integrity_verified: bool
    stop_reason: str
    llm_calls: dict[str, int]
    timings: dict[str, float]
    error_message: str | None

    @property
    def exit_code(self) -> int:
        if self.stop_reason not in {"PASS_REACHED", "FINAL_TEST_FAILURE", "FINAL_TEST_TIMEOUT"}:
            return 2
        return {
            TestStatus.PASS: 0,
            TestStatus.FAIL: 1,
            TestStatus.ERROR: 2,
            TestStatus.TIMEOUT: 3,
        }.get(self.final_execution_status, 2)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": 1,
            "project_name": self.project_name,
            "inspection_status": self.inspection_status,
            "target": {"file": self.target.file.as_posix(), "function": self.target.function},
            "module_name": self.module_name,
            "project_profile_sha256": self.project_profile_sha256,
            "environment_plan_sha256": self.environment_plan_sha256,
            "environment_plan_status": self.environment_plan_status,
            "context_bundle_sha256": self.context_bundle_sha256,
            "context_status": self.context_status,
            "context_chars": self.context_chars,
            "context_files": self.context_files,
            "context_items": self.context_items,
            "unresolved_references": self.unresolved_references,
            "omitted_items": self.omitted_items,
            "context_warnings": self.context_warnings,
            "environment_status": self.environment_status,
            "environment_workspace": str(self.environment_workspace)
            if self.environment_workspace
            else None,
            "target_python": str(self.target_python) if self.target_python else None,
            "initial_execution_status": self.initial_execution_status.value
            if self.initial_execution_status
            else None,
            "final_execution_status": self.final_execution_status.value
            if self.final_execution_status
            else None,
            "repair_count": self.repair_count,
            "repair_success": self.repair_success,
            "initial_line_coverage": self.initial_line_coverage,
            "final_line_coverage": self.final_line_coverage,
            "line_coverage_gain": (
                self.final_line_coverage - self.initial_line_coverage
                if self.final_line_coverage is not None and self.initial_line_coverage is not None
                else None
            ),
            "initial_branch_coverage": self.initial_branch_coverage,
            "final_branch_coverage": self.final_branch_coverage,
            "branch_coverage_gain": (
                self.final_branch_coverage - self.initial_branch_coverage
                if self.final_branch_coverage is not None
                and self.initial_branch_coverage is not None
                else None
            ),
            "coverage_rounds": self.coverage_rounds,
            "accepted_coverage_rounds": self.accepted_coverage_rounds,
            "mutation_enabled": self.mutation_enabled,
            "mutation_feedback_enabled": self.mutation_feedback_enabled,
            "mutation_score_initial": self.mutation_score_initial,
            "mutation_score_final": self.mutation_score_final,
            "mutation_total": self.mutation_total,
            "mutation_killed": self.mutation_killed,
            "mutation_survived": self.mutation_survived,
            "mutation_score_gain": (
                self.mutation_score_final - self.mutation_score_initial
                if self.mutation_score_initial is not None and self.mutation_score_final is not None
                else None
            ),
            "accepted_test_files": list(self.accepted_test_files),
            "source_integrity_verified": self.source_integrity_verified,
            "accepted_test_integrity_verified": self.accepted_test_integrity_verified,
            "stop_reason": self.stop_reason,
            "llm_calls": self.llm_calls,
            "timings": self.timings,
            "error_message": self.error_message,
            "artifacts": {
                "context_bundle": "context/context_bundle.json",
                "target_environment": "target_environment.json",
                "coverage": "coverage/",
                "mutation": "mutation/" if self.mutation_enabled else None,
            },
        }
        stable = dict(payload)
        stable.pop("timings")
        stable.pop("environment_workspace")
        stable.pop("target_python")
        stable.pop("error_message")
        payload["result_sha256"] = hashlib.sha256(
            json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_new(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as file:
        file.write(text)


def _strict_source_manifest(root: Path) -> tuple[tuple[str, str], ...]:
    """Hash every prepared-copy entry, including caches created during a test."""
    entries: list[tuple[str, str]] = []
    total_bytes = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise RepositoryIntegrityError("EXECUTION_SOURCE_MODIFIED")
            if path.is_dir():
                entries.append((f"directory:{relative}", ""))
                stack.append(path)
            elif path.is_file():
                size = path.stat().st_size
                total_bytes += size
                if size > 32 * 1024 * 1024 or total_bytes > 512 * 1024 * 1024:
                    raise RepositoryIntegrityError("EXECUTION_SOURCE_MODIFIED")
                entries.append((relative, _sha256(path)))
            else:
                raise RepositoryIntegrityError("EXECUTION_SOURCE_MODIFIED")
            if len(entries) > 20_000:
                raise RepositoryIntegrityError("EXECUTION_SOURCE_MODIFIED")
    return tuple(sorted(entries))


class _IntegrityGuard:
    def __init__(self, source: Path, manifest: tuple[tuple[str, str], ...]):
        self.source = source
        self.manifest = manifest
        self.accepted: dict[Path, str] = {}

    def verify(self) -> None:
        try:
            current = _strict_source_manifest(self.source)
        except OSError as exc:
            raise RepositoryIntegrityError("EXECUTION_SOURCE_MODIFIED") from exc
        if current != self.manifest:
            raise RepositoryIntegrityError("EXECUTION_SOURCE_MODIFIED")
        for path, expected in self.accepted.items():
            if not path.is_file() or _sha256(path) != expected:
                raise RepositoryIntegrityError("ACCEPTED_TEST_MODIFIED")

    def accept(self, files: tuple[Path, ...]) -> None:
        self.verify()
        for path in files:
            self.accepted.setdefault(path.resolve(), _sha256(path))


class _IntegrityTestRunner(TestRunner):
    def __init__(self, context: RepositoryExecutionContext, guard: _IntegrityGuard, timeout: float):
        super().__init__(timeout=timeout, execution_context=context)
        self.guard = guard

    def run_suite(self, test_files, project_root) -> TestRunResult:
        self.guard.verify()
        try:
            result = super().run_suite(test_files, project_root)
        finally:
            self.guard.verify()
        if result.status is TestStatus.PASS:
            self.guard.accept(tuple(Path(path) for path in test_files))
        return result


class _IntegrityCoverageRunner(CoverageRunner):
    def __init__(self, context: RepositoryExecutionContext, guard: _IntegrityGuard, timeout: float):
        super().__init__(timeout=timeout, execution_context=context)
        self.guard = guard

    def run(self, function, test_files, output_dir) -> CoverageResult:
        self.guard.verify()
        try:
            result = super().run(function, test_files, output_dir)
        finally:
            self.guard.verify()
        self.guard.accept(tuple(Path(path) for path in test_files))
        return result


class _CountingProvider(LLMProvider):
    def __init__(self, inner: LLMProvider):
        self.inner = inner
        self.stage = "execution"
        self.counts = {
            "initial_generation": 0,
            "execution_repair": 0,
            "coverage_generation": 0,
            "coverage_candidate_repair": 0,
            "mutation_feedback": 0,
            "mutation_candidate_repair": 0,
        }

    def generate(self, prompt: str) -> str:
        repair = prompt.startswith("You are repairing")
        category = {
            ("execution", False): "initial_generation",
            ("execution", True): "execution_repair",
            ("coverage", False): "coverage_generation",
            ("coverage", True): "coverage_candidate_repair",
            ("mutation", False): "mutation_feedback",
            ("mutation", True): "mutation_candidate_repair",
        }[(self.stage, repair)]
        self.counts[category] += 1
        return self.inner.generate(prompt)


class RepositoryRunEngine:
    """Orchestrate a single target without changing frozen standalone engines."""

    def __init__(
        self,
        provider_factory: Callable[[], LLMProvider],
        *,
        inspector: ProjectInspector | None = None,
        selector: ContextSelector | None = None,
        provisioner: EnvironmentProvisioner | None = None,
        mutation_backend_factory: Callable[..., Any] | None = None,
        mutation_distro: str | None = None,
    ) -> None:
        self.provider_factory = provider_factory
        self.inspector = inspector or ProjectInspector()
        self.selector = selector or ContextSelector()
        self.provisioner = provisioner
        self.mutation_backend_factory = mutation_backend_factory
        self.mutation_distro = mutation_distro

    def run(
        self,
        project_root: Path,
        target: ContextTarget,
        options: RepositoryRunOptions = RepositoryRunOptions(),
    ) -> RepositoryRunResult:
        root = Path(project_root).resolve(strict=True)
        output = options.output_root.resolve()
        if output == root or output.is_relative_to(root):
            raise RepositoryRunError("Repository artifacts must be outside the original project.")
        store = ArtifactStore(output)
        run = store.create_run()
        started = time.perf_counter()
        state: dict[str, Any] = {
            "project_name": None,
            "inspection_status": "NOT_RUN",
            "target": target,
            "module_name": None,
            "run_directory": run.run_dir,
            "project_profile_sha256": None,
            "environment_plan_sha256": None,
            "environment_plan_status": None,
            "context_bundle_sha256": None,
            "context_status": None,
            "context_chars": 0,
            "context_files": 0,
            "context_items": 0,
            "unresolved_references": 0,
            "omitted_items": 0,
            "context_warnings": 0,
            "environment_status": None,
            "environment_workspace": None,
            "target_python": None,
            "initial_execution_status": None,
            "final_execution_status": None,
            "repair_count": 0,
            "repair_success": False,
            "initial_line_coverage": None,
            "final_line_coverage": None,
            "initial_branch_coverage": None,
            "final_branch_coverage": None,
            "coverage_rounds": 0,
            "accepted_coverage_rounds": 0,
            "mutation_enabled": options.mutation or options.mutation_feedback,
            "mutation_feedback_enabled": options.mutation_feedback,
            "mutation_score_initial": None,
            "mutation_score_final": None,
            "mutation_total": None,
            "mutation_killed": None,
            "mutation_survived": None,
            "accepted_test_files": (),
            "source_integrity_verified": False,
            "accepted_test_integrity_verified": False,
            "stop_reason": "PIPELINE_ERROR",
            "llm_calls": {},
            "timings": {},
            "error_message": None,
        }
        original = None
        guard = None
        provider = None
        try:
            profile = self.inspector.inspect(root)
            state["project_name"] = profile.project_name
            state["inspection_status"] = "OK"
            state["project_profile_sha256"] = profile.sha256
            _write_new(run.run_dir / "project_profile.json", profile.to_json())
            matches = [
                item
                for item in profile.modules
                if item.path.relative_to(profile.root) == target.file and not item.is_test_module
            ]
            if len(matches) != 1 or not matches[0].module_name:
                raise RepositoryRunError("INVALID_TARGET: no unambiguous production module name")
            module_name = matches[0].module_name
            if sum(item.module_name == module_name for item in profile.modules) != 1:
                raise RepositoryRunError("INVALID_TARGET: ambiguous module name")
            state["module_name"] = module_name
            bundle = self.selector.select(root, profile, target, options.context_policy)
            if bundle.project_profile_sha256 != profile.sha256:
                raise RepositoryRunError("STALE_CONTEXT: profile hash mismatch")
            state.update(
                context_bundle_sha256=bundle.bundle_sha256,
                context_status=bundle.status,
                context_chars=bundle.context_chars,
                context_files=len(bundle.selected_file_sha256),
                context_items=len(bundle.items),
                unresolved_references=len(bundle.unresolved_references),
                omitted_items=len(bundle.omitted_items),
                context_warnings=len(bundle.warnings),
            )
            _write_new(run.run_dir / "context/context_bundle.json", bundle.to_json())
            _write_new(run.run_dir / "context/context.txt", bundle.render())
            interpreter = probe_interpreter(options.target_python)
            plan = EnvironmentPlanner().plan(
                profile, interpreter, offline=options.environment_offline
            )
            state["environment_plan_sha256"] = plan.sha256
            state["environment_plan_status"] = plan.status.value
            _write_new(run.run_dir / "environment_plan.json", plan.to_json())
            if plan.status is EnvironmentPlanStatus.UNSUPPORTED:
                raise RepositoryRunError(
                    "UNSUPPORTED_ENVIRONMENT: " + "; ".join(plan.unsupported_reasons)
                )
            if options.mutation or options.mutation_feedback:
                if plan.dependency_strategy is not DependencyStrategy.NONE or any(
                    item.classification != "STDLIB" for item in bundle.external_references
                ):
                    raise RepositoryRunError(
                        "REPOSITORY_MUTATION_UNSUPPORTED: only local and stdlib runtime imports"
                    )
            original = EnvironmentProvisioner()._scan_manifest(root)
            provisioner = self.provisioner or EnvironmentProvisioner(
                timeout=options.environment_timeout
            )
            environment = provisioner.provision(
                root, profile, plan, options.environment_output_root
            )
            state["environment_status"] = environment.status.value
            state["environment_workspace"] = environment.workspace_root
            state["target_python"] = environment.python_executable
            state["timings"]["environment_preparation_seconds"] = environment.total_duration_seconds
            _write_new(run.run_dir / "target_environment.json", environment.to_json())
            if (
                environment.status is not EnvironmentProvisionStatus.READY
                or environment.profile_sha256 != profile.sha256
                or environment.plan_sha256 != plan.sha256
                or environment.source_root is None
                or environment.python_executable is None
                or not environment.copy_integrity_verified
            ):
                raise RepositoryRunError(
                    "ENVIRONMENT_ERROR: target environment is not verified READY"
                )
            copy = environment.source_root
            if self.inspector.inspect(copy).sha256 != profile.sha256:
                raise RepositoryRunError("STALE_CONTEXT: copied profile differs")
            self._verify_context(bundle, copy, preflight=True)
            source_manifest = _strict_source_manifest(copy)
            guard = _IntegrityGuard(copy, source_manifest)
            _write_new(
                run.run_dir / "integrity/source_before.json",
                json.dumps(dict(source_manifest), indent=2) + "\n",
            )
            function = replace(
                ProjectAnalyzer().analyze_function(copy / target.file, target.function),
                module_name=module_name,
            )
            source_roots = tuple(
                copy / path.relative_to(profile.root) for path in profile.source_roots
            )
            if not source_roots or not all(path.is_dir() for path in source_roots):
                raise RepositoryRunError("ENVIRONMENT_ERROR: copied source roots are missing")
            config = run.run_dir / "autotest-pytest.ini"
            _write_new(config, "[pytest]\naddopts =\n")
            context = RepositoryExecutionContext(
                environment.python_executable, run.run_dir, source_roots, config
            )
            runner = _IntegrityTestRunner(context, guard, options.test_timeout)
            coverage_runner = _IntegrityCoverageRunner(context, guard, options.test_timeout)
            prompts = RepositoryPromptBuilder(
                bundle, module_name, lambda: self._verify_context(bundle, copy)
            )
            provider = _CountingProvider(self.provider_factory())
            session = RepairEngine(
                provider,
                runner,
                store,
                max_repair_attempts=options.max_repair_attempts,
                prompt_builder=prompts,
            ).run(function, run)
            state.update(
                initial_execution_status=session.initial_status,
                final_execution_status=session.final_status,
                repair_count=session.repair_count,
                repair_success=session.repaired_successfully,
                accepted_test_files=(
                    (session.attempts[-1].test_file.relative_to(run.run_dir).as_posix(),)
                    if session.final_status is TestStatus.PASS
                    else ()
                ),
            )
            state["timings"].update(
                generation_seconds=session.total_generation_seconds,
                test_execution_seconds=session.total_execution_seconds,
            )
            if session.stopped_reason is StopReason.LLM_ERROR:
                raise RepositoryRunError("LLM_ERROR: execution repair failed")
            if session.final_status is TestStatus.PASS:
                provider.stage = "coverage"
                coverage = CoverageEngine(
                    provider,
                    runner,
                    coverage_runner,
                    store,
                    max_coverage_rounds=options.max_coverage_rounds,
                    coverage_target=options.coverage_target,
                    max_repair_attempts=options.max_repair_attempts,
                    prompt_builder=prompts,
                ).run(function, run, session)
                state["coverage_rounds"] = len(coverage.rounds)
                state["accepted_coverage_rounds"] = sum(item.accepted for item in coverage.rounds)
                state["accepted_test_files"] = tuple(
                    path.relative_to(run.run_dir).as_posix()
                    for path in coverage.accepted_test_files
                )
                if coverage.initial_coverage is not None and coverage.final_coverage is not None:
                    state.update(
                        initial_line_coverage=coverage.initial_coverage.line_coverage_percent,
                        final_line_coverage=coverage.final_coverage.line_coverage_percent,
                        initial_branch_coverage=coverage.initial_coverage.branch_coverage_percent,
                        final_branch_coverage=coverage.final_coverage.branch_coverage_percent,
                    )
                    state["timings"]["coverage_seconds"] = sum(
                        item.duration_seconds for item in coverage.history
                    )
                if coverage.stop_reason in (
                    CoverageStopReason.COVERAGE_ERROR,
                    CoverageStopReason.PIPELINE_ERROR,
                ):
                    raise RepositoryRunError("COVERAGE_ERROR: repository coverage failed")
                if options.mutation or options.mutation_feedback:
                    guard.verify()
                    backend = (
                        self.mutation_backend_factory(copy, profile, options)
                        if self.mutation_backend_factory is not None
                        else RepositoryWSLMutmutBackend(
                            copy,
                            profile,
                            timeout=options.mutation_timeout,
                            mutation_venv=options.mutation_venv,
                            distro=self.mutation_distro,
                        )
                    )
                    mutation_artifacts = store.create_mutation(run)
                    mutation = backend.run(
                        function, coverage.accepted_test_files, mutation_artifacts.mutation_dir
                    )
                    guard.verify()
                    store.save_mutation_result(mutation_artifacts, mutation)
                    state["mutation_score_initial"] = mutation.mutation_score_percent
                    state["mutation_score_final"] = mutation.mutation_score_percent
                    state["mutation_total"] = mutation.total_mutants
                    state["mutation_killed"] = mutation.killed_mutants
                    state["mutation_survived"] = mutation.survived_mutants
                    state["timings"]["mutation_seconds"] = mutation.duration_seconds
                    if mutation.status in (
                        MutationStatus.TOOL_UNAVAILABLE,
                        MutationStatus.TOOL_ERROR,
                        MutationStatus.TIMEOUT,
                    ):
                        raise RepositoryRunError(
                            "MUTATION_ERROR: " + (mutation.error_message or mutation.status.value)
                        )
                    if options.mutation_feedback and coverage.final_coverage is not None:
                        provider.stage = "mutation"
                        feedback = MutationFeedbackEngine(
                            provider,
                            runner,
                            coverage_runner,
                            backend,
                            store,
                            max_mutation_rounds=options.max_mutation_rounds,
                            max_mutants_per_round=options.max_mutants_per_round,
                            max_repair_attempts=options.max_repair_attempts,
                            prompt_builder=prompts,
                        ).run(
                            function,
                            run,
                            coverage.accepted_test_files,
                            mutation,
                            coverage.final_coverage,
                        )
                        guard.verify()
                        state["mutation_score_final"] = (
                            feedback.final_mutation.mutation_score_percent
                        )
                        state["mutation_total"] = feedback.final_mutation.total_mutants
                        state["mutation_killed"] = feedback.final_mutation.killed_mutants
                        state["mutation_survived"] = feedback.final_mutation.survived_mutants
                        state["accepted_test_files"] = tuple(
                            path.relative_to(run.run_dir).as_posix()
                            for path in feedback.accepted_test_files
                        )
                        if feedback.stop_reason in (
                            MutationFeedbackStopReason.MUTATION_ERROR,
                            MutationFeedbackStopReason.LLM_ERROR,
                            MutationFeedbackStopReason.PIPELINE_ERROR,
                        ):
                            raise RepositoryRunError(
                                "MUTATION_ERROR: repository mutation feedback failed"
                            )
                state["stop_reason"] = "PASS_REACHED"
            elif session.final_status is TestStatus.TIMEOUT:
                state["stop_reason"] = "FINAL_TEST_TIMEOUT"
            elif session.final_status is TestStatus.FAIL:
                state["stop_reason"] = "FINAL_TEST_FAILURE"
            else:
                state["stop_reason"] = "FINAL_TEST_ERROR"
        except RepositoryIntegrityError as exc:
            state["stop_reason"] = exc.reason
            state["error_message"] = str(exc)
        except (
            RepositoryRunError,
            ContextSelectionError,
            AutoTestError,
            OSError,
            ValueError,
        ) as exc:
            message = str(exc)
            prefix = message.split(":", 1)[0]
            if isinstance(exc, ContextSelectionError):
                state["stop_reason"] = "INVALID_TARGET"
            elif isinstance(exc, EnvironmentProvisionError):
                state["stop_reason"] = "ENVIRONMENT_ERROR"
            elif isinstance(exc, (GenerationError, LLMError)):
                state["stop_reason"] = "LLM_ERROR"
            elif isinstance(exc, RepositoryRunError) and prefix in {
                "INVALID_TARGET",
                "UNSUPPORTED_ENVIRONMENT",
                "ENVIRONMENT_ERROR",
                "REPOSITORY_MUTATION_UNSUPPORTED",
                "COVERAGE_ERROR",
                "MUTATION_ERROR",
                "LLM_ERROR",
            }:
                state["stop_reason"] = prefix
            else:
                state["stop_reason"] = "PIPELINE_ERROR"
            state["error_message"] = message
        finally:
            if provider is not None:
                state["llm_calls"] = provider.counts
            if original is not None:
                try:
                    state["source_integrity_verified"] = (
                        EnvironmentProvisioner()._scan_manifest(root) == original
                    )
                except (AutoTestError, OSError):
                    state["source_integrity_verified"] = False
                if not state["source_integrity_verified"]:
                    state["stop_reason"] = "SOURCE_INTEGRITY_ERROR"
                    state["error_message"] = "Original repository changed during run."
            if guard is not None:
                try:
                    guard.verify()
                    state["accepted_test_integrity_verified"] = True
                    _write_new(
                        run.run_dir / "integrity/source_after.json",
                        json.dumps(dict(_strict_source_manifest(guard.source)), indent=2) + "\n",
                    )
                    _write_new(
                        run.run_dir / "integrity/accepted_tests.json",
                        json.dumps(
                            {
                                str(path.relative_to(run.run_dir)): value
                                for path, value in guard.accepted.items()
                            },
                            indent=2,
                        )
                        + "\n",
                    )
                except RepositoryIntegrityError as exc:
                    state["stop_reason"] = exc.reason
                    state["error_message"] = str(exc)
            state["timings"]["total_seconds"] = time.perf_counter() - started
            result = RepositoryRunResult(**state)
            _write_new(run.result_file, result.to_json())
        return result

    @staticmethod
    def _verify_context(bundle: ContextBundle, copy: Path, *, preflight: bool = False) -> None:
        for path, expected in bundle.selected_file_sha256:
            source = copy / path
            if not source.is_file() or _sha256(source) != expected:
                reason = "STALE_CONTEXT" if preflight else "STALE_OR_MODIFIED_CONTEXT"
                raise RepositoryIntegrityError(reason)

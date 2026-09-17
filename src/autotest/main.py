"""Command-line entry point for the Phase 1 pipeline."""

from __future__ import annotations

import argparse
import json
import logging
import math
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from autotest.artifact_store import ArtifactStore
from autotest.context_selector import (
    ContextSelectionError,
    ContextSelectionPolicy,
    ContextSelector,
    ContextTarget,
)
from autotest.coverage_engine import CoverageEngine, CoverageSessionResult, CoverageStopReason
from autotest.coverage_runner import CoverageRunner
from autotest.environment_planner import (
    EnvironmentPlan,
    EnvironmentPlanner,
    EnvironmentPlanStatus,
    probe_interpreter,
)
from autotest.environment_provisioner import EnvironmentProvisioner, TargetEnvironment
from autotest.errors import AutoTestError
from autotest.llm.ollama_provider import OllamaConfig, OllamaProvider
from autotest.mutation_feedback import (
    MutationFeedbackEngine,
    MutationFeedbackSessionResult,
    MutationFeedbackStopReason,
)
from autotest.mutation_runner import (
    DEFAULT_MUTATION_VENV,
    MutationResult,
    MutationStatus,
    WSLMutmutBackend,
)
from autotest.project_analyzer import ProjectAnalyzer
from autotest.project_inspector import ProjectInspector, ProjectProfile
from autotest.repair_engine import RepairEngine, RepairSessionResult
from autotest.repository_runner import (
    RepositoryRunEngine,
    RepositoryRunError,
    RepositoryRunOptions,
)
from autotest.test_runner import TestRunner, TestRunResult, TestStatus

LOGGER = logging.getLogger(__name__)


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be greater than or equal to zero")
    return parsed


def _positive_int(value: str) -> int:
    parsed = _non_negative_int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be greater than or equal to one")
    return parsed


def _coverage_percentage(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or not 0 <= parsed <= 100:
        raise argparse.ArgumentTypeError("must be between zero and 100")
    return parsed


def _positive_number(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autotest",
        description="Generate and run pytest tests for one top-level Python function.",
    )
    parser.add_argument("--file", type=Path, help="Standalone Python source file")
    parser.add_argument("--function", help="Exact top-level function name")
    parser.add_argument(
        "--inspect-project", type=Path, help="Statically inspect a local project directory"
    )
    parser.add_argument(
        "--profile-output", type=Path, help="Write inspection JSON to this explicit path"
    )
    parser.add_argument(
        "--plan-environment", type=Path, help="Plan an isolated target environment without writes"
    )
    parser.add_argument(
        "--prepare-environment", type=Path, help="Provision a copied target environment"
    )
    parser.add_argument("--select-context", type=Path, help="Statically select target context")
    parser.add_argument("--run-project", type=Path, help="Run the repository-scale pilot")
    parser.add_argument("--project-target", help="Project-relative Python file:function")
    parser.add_argument(
        "--repository-output-root", type=Path, default=Path("workspace/repository_runs")
    )
    parser.add_argument("--context-target", help="Project-relative Python file:function")
    parser.add_argument(
        "--context-output-root", type=Path, default=Path("workspace/context_bundles")
    )
    parser.add_argument("--context-max-chars", type=_positive_int, default=32_000)
    parser.add_argument("--context-max-files", type=_positive_int, default=8)
    parser.add_argument("--context-max-items", type=_positive_int, default=24)
    parser.add_argument("--context-max-depth", type=_non_negative_int, default=2)
    parser.add_argument("--target-python", type=Path, help="Explicit local CPython interpreter")
    parser.add_argument(
        "--environment-output-root",
        type=Path,
        default=Path("workspace/target_environments"),
        help="Root for fresh prepared target environments",
    )
    parser.add_argument(
        "--environment-timeout",
        type=_positive_number,
        default=600.0,
        help="Per-command environment timeout in seconds (default: 600)",
    )
    parser.add_argument(
        "--environment-offline", action="store_true", help="Use uv's verified --offline mode"
    )
    parser.add_argument("--model", default="qwen2.5-coder:14b", help="Ollama model name")
    parser.add_argument("--ollama-url", default="http://localhost:11434", help="Ollama base URL")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("workspace/runs"),
        help="Root directory for immutable run artifacts",
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="Generated-test timeout in seconds"
    )
    parser.add_argument(
        "--ollama-timeout", type=float, default=120.0, help="Ollama request timeout in seconds"
    )
    parser.add_argument(
        "--temperature", type=float, default=0.0, help="Ollama sampling temperature"
    )
    parser.add_argument(
        "--max-repair-attempts",
        type=_non_negative_int,
        default=3,
        help="Maximum repairs after the initial execution (default: 3; 0 disables repair)",
    )
    parser.add_argument(
        "--max-coverage-rounds",
        type=_non_negative_int,
        default=3,
        help="Maximum coverage-guided additions after the baseline (default: 3; 0 disables them)",
    )
    parser.add_argument(
        "--coverage-target",
        type=_coverage_percentage,
        default=100.0,
        help="Target-function line and applicable branch coverage percentage (default: 100)",
    )
    parser.add_argument(
        "--mutation",
        action="store_true",
        help="Evaluate the final passing suite with the external WSL Mutmut backend",
    )
    parser.add_argument(
        "--mutation-feedback",
        action="store_true",
        help="Enable mutation evaluation and bounded mutation-guided test generation",
    )
    parser.add_argument(
        "--max-mutation-rounds",
        type=_non_negative_int,
        default=3,
        help="Maximum mutation-guided additions after the baseline (default: 3)",
    )
    parser.add_argument(
        "--max-mutants-per-round",
        type=_positive_int,
        default=5,
        help="Maximum deterministically selected survivor diffs per round (default: 5)",
    )
    parser.add_argument(
        "--mutation-timeout",
        type=_positive_number,
        default=300.0,
        help="Overall mutation evaluation timeout in seconds (default: 300)",
    )
    parser.add_argument(
        "--mutation-venv",
        default=DEFAULT_MUTATION_VENV,
        help="Absolute path to the isolated virtualenv inside WSL",
    )
    parser.add_argument("--debug", action="store_true", help="Show debug logging and tracebacks")
    return parser


def _print_result(
    target_file: Path,
    target_function: str,
    run_dir: Path,
    session: RepairSessionResult,
    coverage: CoverageSessionResult | None,
    mutation_requested: bool,
    mutation: MutationResult | None,
    mutation_feedback: MutationFeedbackSessionResult | None,
) -> None:
    final_attempt = session.attempts[-1]
    result = final_attempt.run_result
    print(f"Target file: {target_file}")
    print(f"Target function: {target_function}")
    print(f"Initial status: {session.initial_status.value}")
    for attempt in session.attempts[1:]:
        print(f"Repair attempt {attempt.attempt_index}: {attempt.run_result.status.value}")
    print(f"Generated test file: {final_attempt.test_file}")
    print(f"Run artifact directory: {run_dir}")
    print(f"Execution status: {session.final_status.value}")
    print(f"Final status: {session.final_status.value}")
    print(f"Repairs used: {session.repair_count}")
    print(f"Stop reason: {session.stopped_reason.value}")
    print(f"Exit code: {result.exit_code if result.exit_code is not None else 'N/A'}")
    print(f"Duration: {result.duration_seconds:.3f} seconds")
    if coverage is None:
        print("Coverage: not run because the Phase 2 suite did not pass")
    elif coverage.initial_coverage is None or coverage.final_coverage is None:
        print("Coverage: measurement error")
        print(f"Coverage stop reason: {coverage.stop_reason.value}")
    else:
        initial = coverage.initial_coverage
        final = coverage.final_coverage
        initial_branch = (
            "N/A"
            if initial.branch_coverage_percent is None
            else f"{initial.branch_coverage_percent:.2f}%"
        )
        final_branch = (
            "N/A"
            if final.branch_coverage_percent is None
            else f"{final.branch_coverage_percent:.2f}%"
        )
        print(
            f"Coverage baseline: line {initial.line_coverage_percent:.2f}%, branch {initial_branch}"
        )
        for item in coverage.rounds:
            candidate_status = (
                item.candidate_execution.final_status.value
                if item.candidate_execution is not None
                else "NOT EXECUTED"
            )
            print(
                f"Coverage round {item.round_index}: candidate {candidate_status}, "
                f"accepted {'YES' if item.accepted else 'NO'}"
            )
        print(f"Line coverage: {final.line_coverage_percent:.2f}%")
        print(f"Branch coverage: {final_branch}")
        print(f"Coverage rounds used: {len(coverage.rounds)}")
        print(f"Coverage target reached: {'YES' if coverage.target_reached else 'NO'}")
        print(f"Coverage stop reason: {coverage.stop_reason.value}")
    if mutation is not None:
        print("Mutation:")
        version = mutation.backend_version or "unavailable"
        print(f"Backend: {mutation.backend} {version}")
        print(f"Status: {mutation.status.value}")
        print(f"Mutants: {mutation.total_mutants}")
        print(f"Killed: {mutation.killed_mutants}")
        print(f"Survived: {mutation.survived_mutants}")
        score = (
            "N/A"
            if mutation.mutation_score_percent is None
            else f"{mutation.mutation_score_percent:.2f}%"
        )
        print(f"Mutation score: {score}")
        print(f"Mutation duration: {mutation.duration_seconds:.2f}s")
        if mutation.error_message:
            print(f"Mutation error: {mutation.error_message}")
    elif mutation_requested:
        print("Mutation: not run because the final accepted suite was not available and passing")
    if mutation_feedback is not None:
        baseline = mutation_feedback.baseline_mutation
        final = mutation_feedback.final_mutation
        print("Mutation feedback:")
        print(
            f"Baseline: {baseline.killed_mutants} killed, "
            f"{baseline.survived_mutants} survived, "
            f"score {_score_text(baseline.mutation_score_percent)}"
        )
        for item in mutation_feedback.rounds:
            status = (
                item.candidate_execution.final_status.value
                if item.candidate_execution is not None
                else "NOT EXECUTED"
            )
            after_score = (
                item.mutation_after.mutation_score_percent
                if item.mutation_after is not None
                else None
            )
            after_survivors = (
                item.mutation_after.survived_mutants if item.mutation_after is not None else "N/A"
            )
            print(
                f"Round {item.round_index}: selected "
                f"{len(item.evidence_before.selected_survivors)}, candidate {status}, "
                f"score {_score_text(after_score)}, survivors {after_survivors}, "
                f"accepted {'YES' if item.accepted else 'NO'}"
            )
        gain = mutation_feedback.mutation_score_gain
        gain_text = "N/A" if gain is None else f"{gain:+.2f}%"
        print(f"Final mutation score: {_score_text(final.mutation_score_percent)}")
        print(f"Mutation gain: {gain_text}")
        print(f"Mutation feedback stop reason: {mutation_feedback.stop_reason.value}")
    print("pytest stdout:")
    print(result.stdout.rstrip() or "(empty)")
    print("pytest stderr:")
    print(result.stderr.rstrip() or "(empty)")


def _score_text(score: float | None) -> str:
    return "N/A" if score is None else f"{score:.2f}%"


def _exit_code(result: TestRunResult) -> int:
    """Map the finite execution status set to the stable CLI contract."""
    return {
        TestStatus.PASS: 0,
        TestStatus.FAIL: 1,
        TestStatus.ERROR: 2,
        TestStatus.TIMEOUT: 3,
    }[result.status]


def _print_profile(profile: ProjectProfile, output: Path | None) -> None:
    def paths(items: tuple[Path, ...]) -> str:
        return (
            ", ".join(path.relative_to(profile.root).as_posix() or "." for path in items) or "none"
        )

    print(f"Project: {profile.project_name}")
    print(f"Python project: {'yes' if profile.is_python_project else 'no'}")
    print(f"Python requirement: {profile.python_requirement or 'unknown'}")
    print(f"Build backend: {profile.build_backend or 'unknown'}")
    print(f"Package/dependency workflow: {profile.package_manager or 'unknown'}")
    print(f"Metadata: {paths(profile.metadata_files)}")
    print(f"Dependency files: {paths(profile.dependency_files)}")
    print(f"Lock files: {paths(profile.lock_files)}")
    print(f"Source roots: {paths(profile.source_roots)}")
    print(f"Test roots: {paths(profile.test_roots)}")
    print(f"Python modules: {len(profile.modules)}")
    print(
        f"Top-level functions: {sum(len(module.top_level_functions) for module in profile.modules)}"
    )
    print(f"Test framework evidence: {profile.test_framework or 'none'}")
    for warning in profile.warnings:
        print(f"Warning: {warning}")
    if output is not None:
        print(f"Profile: {output}")


def _print_environment_plan(plan: EnvironmentPlan) -> None:
    print(f"Project: {plan.project_name}")
    print(f"Environment support: {plan.status.value}")
    print(f"Project profile: {plan.project_profile_sha256}")
    print(f"Python requirement: {plan.python_requirement or 'not declared'}")
    print(f"Selected Python: {plan.selected_python.version} ({plan.selected_python.executable})")
    print(f"Compatibility: {plan.compatibility.value}")
    print(f"Dependency strategy: {plan.dependency_strategy.value}")
    print(f"Runtime dependencies: {len(plan.dependencies)}")
    print(f"Runner tools: {', '.join(plan.runner_requirements)}")
    print(f"Network may be required: {'YES' if plan.network_may_be_required else 'NO'}")
    print("Target project installation: NO")
    print(f"Plan hash: {plan.sha256}")
    for warning in plan.warnings:
        print(f"Warning: {warning}")
    for reason in plan.unsupported_reasons:
        print(f"Unsupported: {reason}")


def _print_target_environment(result: TargetEnvironment) -> None:
    print(f"Environment: {result.status.value}")
    print(f"Workspace: {result.workspace_root or 'not created'}")
    print(f"Source copy: {'VERIFIED' if result.copy_integrity_verified else 'NOT VERIFIED'}")
    print(
        "Original repository: "
        + ("UNCHANGED" if result.original_integrity_verified else "NOT VERIFIED")
    )
    print(f"Python: {result.python_version or 'unknown'}")
    print(
        "Target dependencies: "
        + ("SATISFIED" if result.target_dependencies_installed else "NOT INSTALLED")
    )
    print(f"Runner tools: {'VERIFIED' if result.status.value == 'READY' else 'NOT VERIFIED'}")
    if result.workspace_root is not None:
        print(f"Environment plan: {result.workspace_root / 'environment_plan.json'}")
        print(f"Provision result: {result.workspace_root / 'provision_result.json'}")
    if result.error_message:
        print(f"Environment error: {result.error_message}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    static_modes = [
        args.inspect_project,
        args.plan_environment,
        args.prepare_environment,
        args.select_context,
        args.run_project,
    ]
    if sum(mode is not None for mode in static_modes) > 1:
        parser.error("Inspection, environment, and context-selection modes are exclusive")
    if args.inspect_project is None and args.profile_output is not None:
        parser.error("--profile-output requires --inspect-project")
    if args.inspect_project is not None:
        if args.file is not None or args.function is not None:
            parser.error("--inspect-project cannot be combined with --file or --function")
    elif (
        args.plan_environment is not None
        or args.prepare_environment is not None
        or args.select_context is not None
        or args.run_project is not None
    ):
        if args.file is not None or args.function is not None:
            parser.error("Static modes cannot be combined with --file or --function")
    else:
        if args.file is None or args.function is None:
            parser.error("--file and --function are required unless a static mode is used")
    if args.inspect_project is not None and (
        args.target_python is not None or args.environment_offline
    ):
        parser.error("Environment options require --plan-environment or --prepare-environment")
    if args.select_context is None and args.context_target is not None:
        parser.error("--context-target requires --select-context")
    if args.run_project is None and args.project_target is not None:
        parser.error("--project-target requires --run-project")
    if args.run_project is not None and args.project_target is None:
        parser.error("--project-target is required with --run-project")
    if args.select_context is not None:
        if args.context_target is None:
            parser.error("--context-target is required with --select-context")
        if args.target_python is not None or args.environment_offline:
            parser.error("Environment options cannot be used with --select-context")
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    try:
        if args.run_project is not None:
            target = ContextTarget.parse(args.project_target)
            options = RepositoryRunOptions(
                output_root=args.repository_output_root,
                environment_output_root=args.environment_output_root,
                target_python=args.target_python,
                environment_offline=args.environment_offline,
                environment_timeout=args.environment_timeout,
                test_timeout=args.timeout,
                max_repair_attempts=args.max_repair_attempts,
                max_coverage_rounds=args.max_coverage_rounds,
                coverage_target=args.coverage_target,
                context_policy=ContextSelectionPolicy(
                    args.context_max_chars,
                    args.context_max_files,
                    args.context_max_items,
                    args.context_max_depth,
                ),
                mutation=args.mutation,
                mutation_feedback=args.mutation_feedback,
                mutation_timeout=args.mutation_timeout,
                mutation_venv=args.mutation_venv,
                max_mutation_rounds=args.max_mutation_rounds,
                max_mutants_per_round=args.max_mutants_per_round,
            )
            result = RepositoryRunEngine(
                lambda: OllamaProvider(
                    OllamaConfig(
                        base_url=args.ollama_url,
                        model=args.model,
                        timeout=args.ollama_timeout,
                        temperature=args.temperature,
                    )
                )
            ).run(args.run_project, target, options)
            print(f"Project: {result.project_name or 'unknown'}")
            print(f"Target: {target.file.as_posix()}:{target.function}")
            print(f"Module: {result.module_name or 'unresolved'}")
            print(f"Context: {result.context_status or 'not selected'}")
            print(f"Environment: {result.environment_status or 'not prepared'}")
            print(f"Execution: {result.final_execution_status or 'not run'}")
            coverage_text = (
                result.final_line_coverage if result.final_line_coverage is not None else "N/A"
            )
            print(f"Coverage: {coverage_text}")
            print(f"Mutation: {'requested' if result.mutation_enabled else 'disabled'}")
            integrity_text = "UNCHANGED" if result.source_integrity_verified else "NOT VERIFIED"
            print(f"Original repository: {integrity_text}")
            print(f"Stop reason: {result.stop_reason}")
            print(f"Result: {result.run_directory / 'result.json'}")
            if result.error_message:
                print(f"Error: {result.error_message}")
            return result.exit_code
        if args.select_context is not None:
            profile = ProjectInspector().inspect(args.select_context)
            target = ContextTarget.parse(args.context_target)
            policy = ContextSelectionPolicy(
                args.context_max_chars,
                args.context_max_files,
                args.context_max_items,
                args.context_max_depth,
            )
            selection_started = time.perf_counter()
            bundle = ContextSelector().select(profile.root, profile, target, policy)
            selection_seconds = time.perf_counter() - selection_started
            output_root = args.context_output_root.resolve()
            if output_root == profile.root or output_root.is_relative_to(profile.root):
                raise ContextSelectionError(
                    "Context output root must be outside the target project."
                )
            output_root.mkdir(parents=True, exist_ok=True)
            for _ in range(10):
                name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                directory = output_root / f"{name}-{secrets.token_hex(3)}"
                try:
                    directory.mkdir(exist_ok=False)
                    break
                except FileExistsError:
                    continue
            else:
                raise ContextSelectionError("Could not reserve a fresh context directory.")
            with (directory / "context_bundle.json").open(
                "x", encoding="utf-8", newline="\n"
            ) as file:
                file.write(bundle.to_json())
            with (directory / "context.txt").open("x", encoding="utf-8", newline="\n") as file:
                file.write(bundle.render())
            with (directory / "selection_metrics.json").open(
                "x", encoding="utf-8", newline="\n"
            ) as file:
                file.write(
                    json.dumps({"selection_duration_seconds": selection_seconds}, indent=2) + "\n"
                )
            print(f"Project: {profile.project_name}")
            print(f"Target: {target.file.as_posix()}:{target.function}")
            print(f"Context selection: {bundle.status}")
            print(f"Selected files: {len(bundle.selected_file_sha256)} / {policy.max_files}")
            print(f"Selected items: {len(bundle.items)} / {policy.max_items}")
            print(f"Context characters: {bundle.context_chars} / {policy.max_chars}")
            local_dependencies = (
                bundle.direct_local_dependencies + bundle.recursive_local_dependencies
            )
            print(f"Local dependencies: {local_dependencies}")
            print(f"External references: {len(bundle.external_references)}")
            print(f"Unresolved references: {len(bundle.unresolved_references)}")
            print(f"Omitted context: {len(bundle.omitted_items)}")
            print(f"Bundle: {directory / 'context_bundle.json'}")
            print(f"Rendered context: {directory / 'context.txt'}")
            return 0
        if args.inspect_project is not None:
            profile = ProjectInspector().inspect(args.inspect_project)
            output = args.profile_output
            if output is not None:
                output = output.resolve()
                try:
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with output.open("x", encoding="utf-8", newline="\n") as target:
                        target.write(profile.to_json())
                except OSError as exc:
                    LOGGER.error("Could not save project profile: %s", exc)
                    return 2
            _print_profile(profile, output)
            return 0
        environment_root = args.plan_environment or args.prepare_environment
        if environment_root is not None:
            profile = ProjectInspector().inspect(environment_root)
            interpreter = probe_interpreter(args.target_python)
            plan = EnvironmentPlanner().plan(profile, interpreter, offline=args.environment_offline)
            _print_environment_plan(plan)
            if args.plan_environment is not None:
                return 0
            if plan.status is EnvironmentPlanStatus.UNSUPPORTED:
                return 2
            result = EnvironmentProvisioner(timeout=args.environment_timeout).provision(
                environment_root, profile, plan, args.environment_output_root
            )
            _print_target_environment(result)
            return 0 if result.status.value == "READY" else 2
        analyzer = ProjectAnalyzer()
        function = analyzer.analyze_function(args.file, args.function)
        LOGGER.info("Analyzed %s from %s", function.function_name, function.file_path)

        provider = OllamaProvider(
            OllamaConfig(
                base_url=args.ollama_url,
                model=args.model,
                timeout=args.ollama_timeout,
                temperature=args.temperature,
            )
        )
        artifact_store = ArtifactStore(args.output_dir)
        run = artifact_store.create_run()
        runner = TestRunner(timeout=args.timeout)
        session = RepairEngine(
            provider,
            runner,
            artifact_store,
            max_repair_attempts=args.max_repair_attempts,
        ).run(function, run)
        coverage_session = None
        if session.final_status is TestStatus.PASS:
            coverage_session = CoverageEngine(
                provider,
                runner,
                CoverageRunner(timeout=args.timeout),
                artifact_store,
                max_coverage_rounds=args.max_coverage_rounds,
                coverage_target=args.coverage_target,
                max_repair_attempts=args.max_repair_attempts,
            ).run(function, run, session)
        mutation_result = None
        mutation_feedback_session = None
        coverage_failed = coverage_session is not None and coverage_session.stop_reason in (
            CoverageStopReason.COVERAGE_ERROR,
            CoverageStopReason.PIPELINE_ERROR,
        )
        mutation_requested = args.mutation or args.mutation_feedback
        mutation_backend = None
        if mutation_requested and session.final_status is TestStatus.PASS and not coverage_failed:
            mutation_artifacts = artifact_store.create_mutation(run)
            accepted_tests = (
                coverage_session.accepted_test_files
                if coverage_session is not None
                else (session.attempts[-1].test_file,)
            )
            mutation_backend = WSLMutmutBackend(
                timeout=args.mutation_timeout,
                mutation_venv=args.mutation_venv,
            )
            mutation_result = mutation_backend.run(
                function,
                accepted_tests,
                mutation_artifacts.mutation_dir,
            )
            artifact_store.save_mutation_result(mutation_artifacts, mutation_result)
            if (
                args.mutation_feedback
                and coverage_session is not None
                and coverage_session.final_coverage is not None
            ):
                mutation_feedback_session = MutationFeedbackEngine(
                    provider,
                    runner,
                    CoverageRunner(timeout=args.timeout),
                    mutation_backend,
                    artifact_store,
                    max_mutation_rounds=args.max_mutation_rounds,
                    max_mutants_per_round=args.max_mutants_per_round,
                    max_repair_attempts=args.max_repair_attempts,
                ).run(
                    function,
                    run,
                    accepted_tests,
                    mutation_result,
                    coverage_session.final_coverage,
                )
        artifact_store.save_session_result(
            run,
            function,
            session,
            provider="ollama",
            model=provider.config.model,
            base_url=provider.config.base_url,
            http_timeout_seconds=provider.config.timeout,
            temperature=provider.config.temperature,
            max_repair_attempts=args.max_repair_attempts,
            execution_timeout_seconds=args.timeout,
            coverage_session=coverage_session,
            max_coverage_rounds=args.max_coverage_rounds,
            coverage_target=args.coverage_target,
            mutation_enabled=mutation_requested,
            mutation_timeout_seconds=args.mutation_timeout,
            mutation_result=mutation_result,
            mutation_skipped_reason=(
                "COVERAGE_PIPELINE_ERROR" if mutation_requested and coverage_failed else None
            ),
            mutation_feedback_enabled=args.mutation_feedback,
            mutation_feedback_session=mutation_feedback_session,
            max_mutation_rounds=args.max_mutation_rounds,
            max_mutants_per_round=args.max_mutants_per_round,
        )
        LOGGER.info("Final status: %s", session.final_status.value)
        LOGGER.info("Run artifacts: %s", run.run_dir)
        _print_result(
            function.file_path,
            function.function_name,
            run.run_dir,
            session,
            coverage_session,
            mutation_requested,
            mutation_result,
            mutation_feedback_session,
        )
        if coverage_failed:
            return 2
        if mutation_result is not None and mutation_result.status in (
            MutationStatus.TOOL_UNAVAILABLE,
            MutationStatus.TOOL_ERROR,
            MutationStatus.TIMEOUT,
        ):
            return 2
        if (
            mutation_feedback_session is not None
            and mutation_feedback_session.stop_reason
            in (
                MutationFeedbackStopReason.MUTATION_ERROR,
                MutationFeedbackStopReason.LLM_ERROR,
                MutationFeedbackStopReason.PIPELINE_ERROR,
            )
            and not (
                mutation_feedback_session.stop_reason is MutationFeedbackStopReason.MUTATION_ERROR
                and mutation_result is not None
                and mutation_result.status is MutationStatus.NO_MUTANTS
            )
        ):
            return 2
        return _exit_code(session.attempts[-1].run_result)
    except (AutoTestError, ContextSelectionError, RepositoryRunError, OSError) as exc:
        LOGGER.error("%s", exc, exc_info=args.debug)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

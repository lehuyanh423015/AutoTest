"""Command-line entry point for the Phase 1 pipeline."""

from __future__ import annotations

import argparse
import logging
import math
from pathlib import Path
from typing import Sequence

from autotest.artifact_store import ArtifactStore
from autotest.coverage_engine import CoverageEngine, CoverageSessionResult, CoverageStopReason
from autotest.coverage_runner import CoverageRunner
from autotest.errors import AutoTestError
from autotest.llm.ollama_provider import OllamaConfig, OllamaProvider
from autotest.project_analyzer import ProjectAnalyzer
from autotest.repair_engine import RepairEngine, RepairSessionResult
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


def _coverage_percentage(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or not 0 <= parsed <= 100:
        raise argparse.ArgumentTypeError("must be between zero and 100")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autotest",
        description="Generate and run pytest tests for one top-level Python function.",
    )
    parser.add_argument("--file", required=True, type=Path, help="Standalone Python source file")
    parser.add_argument("--function", required=True, help="Exact top-level function name")
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
    parser.add_argument("--debug", action="store_true", help="Show debug logging and tracebacks")
    return parser


def _print_result(
    target_file: Path,
    target_function: str,
    run_dir: Path,
    session: RepairSessionResult,
    coverage: CoverageSessionResult | None,
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
    print("pytest stdout:")
    print(result.stdout.rstrip() or "(empty)")
    print("pytest stderr:")
    print(result.stderr.rstrip() or "(empty)")


def _exit_code(result: TestRunResult) -> int:
    """Map the finite execution status set to the stable CLI contract."""
    return {
        TestStatus.PASS: 0,
        TestStatus.FAIL: 1,
        TestStatus.ERROR: 2,
        TestStatus.TIMEOUT: 3,
    }[result.status]


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    try:
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
        )
        LOGGER.info("Final status: %s", session.final_status.value)
        LOGGER.info("Run artifacts: %s", run.run_dir)
        _print_result(
            function.file_path,
            function.function_name,
            run.run_dir,
            session,
            coverage_session,
        )
        if coverage_session is not None and coverage_session.stop_reason in (
            CoverageStopReason.COVERAGE_ERROR,
            CoverageStopReason.PIPELINE_ERROR,
        ):
            return 2
        return _exit_code(session.attempts[-1].run_result)
    except AutoTestError as exc:
        LOGGER.error("%s", exc, exc_info=args.debug)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

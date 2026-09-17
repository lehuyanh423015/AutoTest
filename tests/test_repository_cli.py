"""Repository mode routing and provider-neutral prompt contracts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from autotest.context_selector import ContextSelector, ContextTarget
from autotest.coverage_runner import CoverageResult
from autotest.failure_analyzer import FailureAnalyzer
from autotest.main import main
from autotest.mutation_runner import SurvivingMutant
from autotest.project_analyzer import ProjectAnalyzer
from autotest.project_inspector import ProjectInspector
from autotest.repository_prompts import RepositoryPromptBuilder
from autotest.test_runner import TestRunResult as ExecutionResult
from autotest.test_runner import TestStatus as ExecutionStatus

FIXTURE = Path(__file__).parent / "fixtures/projects/repository_pilot"
TARGET = ContextTarget(Path("src/demo/pricing.py"), "calculate_total")


def test_repository_prompt_variants_are_deterministic_and_portable():
    profile = ProjectInspector().inspect(FIXTURE)
    bundle = ContextSelector().select(profile.root, profile, TARGET)
    function = replace(
        ProjectAnalyzer().analyze_function(FIXTURE / TARGET.file, TARGET.function),
        module_name="demo.pricing",
    )
    prompts = RepositoryPromptBuilder(bundle, "demo.pricing", lambda: None)
    initial = prompts.build(function)
    assert initial == prompts.build(function)
    assert "from demo.pricing import calculate_total" in initial
    assert "src/demo/tax.py" in initial
    assert "compute_tax" in initial
    assert "Do not modify" in initial or "do not modify" in initial
    assert str(FIXTURE.resolve()) not in initial
    assert "E:\\" not in initial
    failure = FailureAnalyzer().analyze(
        ExecutionResult(Path("generated_test.py"), 2, ExecutionStatus.ERROR, "", "ImportError", 0.1)
    )
    repair = prompts.build_repair(function, "def test_x(): pass", failure, 1)
    assert "ImportError" in repair
    assert "src/demo/tax.py" in repair
    coverage = CoverageResult(
        function.file_path,
        function.function_name,
        (8, 9, 10, 11, 12),
        (8, 9, 10, 12),
        (11,),
        ((10, 12),),
        ((10, 11),),
        80.0,
        50.0,
        None,
        0.1,
    )
    coverage_prompt = prompts.build_coverage(function, coverage, ("def test_low(): pass",), 1)
    assert "ADDITIONAL" in coverage_prompt
    assert "src/demo/tax.py" in coverage_prompt
    mutation_prompt = prompts.build_mutation_feedback(
        function,
        ("def test_low(): pass",),
        (SurvivingMutant("demo.pricing.x_calculate_total__mutmut_1", "calculate_total", "diff"),),
        1,
    )
    assert "surviving" in mutation_prompt.lower()
    assert "src/demo/tax.py" in mutation_prompt


@pytest.mark.parametrize(
    "args",
    [
        ["--run-project", str(FIXTURE)],
        ["--project-target", "src/demo/pricing.py:calculate_total"],
        ["--run-project", str(FIXTURE), "--project-target", "bad"],
        [
            "--run-project",
            str(FIXTURE),
            "--project-target",
            "src/demo/pricing.py:calculate_total",
            "--inspect-project",
            str(FIXTURE),
        ],
        [
            "--run-project",
            str(FIXTURE),
            "--project-target",
            "src/demo/pricing.py:calculate_total",
            "--file",
            "target.py",
        ],
    ],
)
def test_repository_cli_invalid_and_exclusive(args):
    try:
        result = main(args)
    except SystemExit as exc:
        result = exc.code
    assert result == 2


def test_repository_cli_routes_options_without_eager_provider(monkeypatch, tmp_path, capsys):
    seen = {}

    class FakeEngine:
        def __init__(self, factory):
            seen["factory"] = factory

        def run(self, root, target, options):
            seen["root"] = root
            seen["target"] = target
            seen["options"] = options
            return SimpleNamespace(
                project_name="demo",
                module_name="demo.pricing",
                context_status="COMPLETE",
                environment_status="READY",
                final_execution_status=ExecutionStatus.PASS,
                final_line_coverage=100.0,
                mutation_enabled=False,
                source_integrity_verified=True,
                stop_reason="PASS_REACHED",
                run_directory=tmp_path,
                error_message=None,
                exit_code=0,
            )

    monkeypatch.setattr("autotest.main.RepositoryRunEngine", FakeEngine)
    assert (
        main(
            [
                "--run-project",
                str(FIXTURE),
                "--project-target",
                "src/demo/pricing.py:calculate_total",
                "--context-max-depth",
                "1",
                "--max-coverage-rounds",
                "0",
                "--repository-output-root",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert seen["target"] == TARGET
    assert seen["options"].context_policy.max_dependency_depth == 1
    assert seen["options"].max_coverage_rounds == 0
    assert seen["options"].output_root == tmp_path
    assert "Context: COMPLETE" in capsys.readouterr().out

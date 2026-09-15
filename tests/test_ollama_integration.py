"""Opt-in end-to-end smoke test requiring local Ollama and the baseline model."""

import os
from pathlib import Path

import pytest

from autotest.artifact_store import ArtifactStore
from autotest.coverage_engine import CoverageEngine
from autotest.coverage_runner import CoverageRunner
from autotest.llm.base import LLMProvider
from autotest.llm.ollama_provider import OllamaProvider
from autotest.project_analyzer import ProjectAnalyzer
from autotest.repair_engine import RepairEngine
from autotest.test_generator import GeneratedTest
from autotest.test_generator import TestGenerator as Generator
from autotest.test_runner import TestRunner as Runner
from autotest.test_runner import TestStatus as RunStatus

pytestmark = [
    pytest.mark.ollama,
    pytest.mark.skipif(
        os.environ.get("AUTOTEST_RUN_OLLAMA") != "1",
        reason="set AUTOTEST_RUN_OLLAMA=1 to run the local Ollama smoke test",
    ),
]


class RecordingProvider(LLMProvider):
    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider
        self.prompts: list[str] = []
        self.responses: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        response = self.provider.generate(prompt)
        self.responses.append(response)
        return response


def test_end_to_end_with_ollama(tmp_path: Path) -> None:
    target = Path(__file__).parent / "fixtures" / "sample_project" / "calculator.py"
    info = ProjectAnalyzer().analyze_function(target, "divide")
    generated = Generator(OllamaProvider(), output_dir=tmp_path).generate(info)

    result = Runner().run(generated.test_file, project_root=target.parent)

    assert generated.test_file.is_file()
    assert result.status in {RunStatus.PASS, RunStatus.FAIL}, f"{result.stdout}\n{result.stderr}"
    assert result.exit_code in {0, 1}


def test_live_ollama_repair_attempt_is_persisted(tmp_path: Path) -> None:
    target = Path(__file__).parent / "fixtures" / "sample_project" / "calculator.py"
    function = ProjectAnalyzer().analyze_function(target, "divide")
    ollama = OllamaProvider()
    provider = RecordingProvider(ollama)
    store = ArtifactStore(tmp_path / "runs")
    run = store.create_run()
    broken_code = (
        "from calculator import invented_function\n\n"
        "def test_invented():\n"
        "    assert invented_function() == 1\n"
    )
    initial = GeneratedTest(
        target_function="divide",
        test_file=tmp_path / "unused.py",
        prompt="Intentional broken integration fixture.",
        raw_response=broken_code,
        test_code=broken_code,
    )

    session = RepairEngine(
        provider,
        Runner(),
        store,
        max_repair_attempts=1,
    ).run(function, run, initial_test=initial)
    store.save_session_result(
        run,
        function,
        session,
        provider="ollama",
        model=ollama.config.model,
        base_url=ollama.config.base_url,
        http_timeout_seconds=ollama.config.timeout,
        temperature=ollama.config.temperature,
        max_repair_attempts=1,
        execution_timeout_seconds=30.0,
    )

    assert session.initial_status is RunStatus.ERROR
    assert len(provider.prompts) == 1
    assert len(provider.responses) == 1
    assert len(session.attempts) == 2
    assert "You are repairing" in provider.prompts[0]
    assert (run.run_dir / "attempt-001" / "prompt.txt").is_file()
    assert (run.run_dir / "attempt-001" / "raw_response.txt").is_file()
    assert (run.run_dir / "attempt-001" / "result.json").is_file()
    assert run.result_file.is_file()


def test_live_ollama_coverage_round_is_persisted(tmp_path: Path) -> None:
    target = Path(__file__).parent / "fixtures" / "sample_project" / "branching.py"
    function = ProjectAnalyzer().analyze_function(target, "classify_number")
    ollama = OllamaProvider()
    provider = RecordingProvider(ollama)
    store = ArtifactStore(tmp_path / "runs")
    run = store.create_run()
    baseline_code = (
        "from branching import classify_number\n\n"
        "def test_positive():\n    assert classify_number(7) == 'positive'\n"
    )
    initial = GeneratedTest(
        "classify_number",
        tmp_path / "unused.py",
        "Deterministic partial-coverage integration baseline.",
        baseline_code,
        baseline_code,
    )
    runner = Runner()
    phase2 = RepairEngine(provider, runner, store, max_repair_attempts=1).run(
        function, run, initial_test=initial
    )

    coverage = CoverageEngine(
        provider,
        runner,
        CoverageRunner(),
        store,
        max_coverage_rounds=1,
        max_repair_attempts=1,
    ).run(function, run, phase2)

    assert coverage.initial_coverage is not None
    assert coverage.initial_coverage.line_coverage_percent < 100
    assert len(provider.prompts) >= 1
    assert "generating ADDITIONAL pytest tests" in provider.prompts[0]
    assert len(provider.responses) >= 1
    assert len(coverage.rounds) == 1
    assert coverage.rounds[0].candidate_test is not None
    assert coverage.rounds[0].candidate_execution is not None
    round_dir = run.run_dir / "coverage" / "round-001"
    assert (round_dir / "raw_response.txt").is_file()
    assert (round_dir / "generated_test.py").is_file()
    assert (round_dir / "result.json").is_file()
    assert (round_dir / "coverage_raw.json").is_file() or not coverage.rounds[0].accepted

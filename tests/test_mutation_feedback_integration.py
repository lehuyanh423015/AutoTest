"""Opt-in real Mutmut Phase 4B integration without an LLM service."""

import hashlib
import os
from pathlib import Path

import pytest

from autotest.artifact_store import ArtifactStore
from autotest.coverage_runner import CoverageRunner
from autotest.errors import MutationEnvironmentUnavailableError
from autotest.llm.base import LLMProvider
from autotest.llm.ollama_provider import OllamaProvider
from autotest.mutation_feedback import MutationFeedbackEngine
from autotest.mutation_runner import MutationStatus, WSLMutmutBackend
from autotest.project_analyzer import ProjectAnalyzer
from autotest.test_runner import TestRunner as Runner


class BoundaryProvider(LLMProvider):
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return (
            "from boundary import classify_boundary\n\n"
            "def test_inclusive_boundary():\n"
            '    assert classify_boundary(10) == "large"\n'
        )


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


@pytest.mark.mutation
def test_live_mutmut_feedback_improves_boundary_suite(tmp_path: Path) -> None:
    backend = WSLMutmutBackend(timeout=300.0)
    try:
        backend.check_environment()
    except MutationEnvironmentUnavailableError as exc:
        pytest.skip(f"live WSL/Mutmut environment unavailable: {exc}")

    fixture_dir = Path(__file__).parent / "fixtures" / "mutation_feedback_suite"
    function = ProjectAnalyzer().analyze_function(fixture_dir / "boundary.py", "classify_boundary")
    accepted = tmp_path / "baseline_suite.py"
    accepted.write_bytes((fixture_dir / "baseline_suite.py").read_bytes())
    source_hash = hashlib.sha256(function.file_path.read_bytes()).hexdigest()
    test_hash = hashlib.sha256(accepted.read_bytes()).hexdigest()
    store = ArtifactStore(tmp_path / "runs")
    run = store.create_run()
    mutation_artifacts = store.create_mutation(run)
    baseline = backend.run(function, [accepted], mutation_artifacts.mutation_dir)
    store.save_mutation_result(mutation_artifacts, baseline)
    assert baseline.status is MutationStatus.COMPLETE
    assert baseline.survived_mutants > 0

    coverage = CoverageRunner().run(
        function, [accepted], run.run_dir / "integration-baseline-coverage"
    )
    provider = BoundaryProvider()
    session = MutationFeedbackEngine(
        provider,
        Runner(),
        CoverageRunner(),
        backend,
        store,
        max_mutation_rounds=1,
        max_mutants_per_round=5,
        max_repair_attempts=0,
    ).run(function, run, [accepted], baseline, coverage)

    assert len(provider.prompts) == 1
    assert session.rounds[0].accepted
    assert session.final_mutation.survived_mutants < baseline.survived_mutants
    assert session.final_mutation.mutation_score_percent > baseline.mutation_score_percent
    assert len(session.accepted_test_files) == 2
    assert hashlib.sha256(function.file_path.read_bytes()).hexdigest() == source_hash
    assert hashlib.sha256(accepted.read_bytes()).hexdigest() == test_hash
    assert (run.run_dir / "mutation-feedback" / "round-001" / "result.json").is_file()
    assert (
        run.run_dir / "mutation-feedback" / "round-001" / "mutation" / "mutation_result.json"
    ).is_file()


@pytest.mark.mutation
@pytest.mark.ollama
@pytest.mark.skipif(
    os.environ.get("AUTOTEST_RUN_OLLAMA_MUTATION_FEEDBACK") != "1",
    reason="set AUTOTEST_RUN_OLLAMA_MUTATION_FEEDBACK=1 for live Ollama plus Mutmut",
)
def test_live_ollama_and_mutmut_feedback_is_transactional(tmp_path: Path) -> None:
    backend = WSLMutmutBackend(timeout=300.0)
    fixture_dir = Path(__file__).parent / "fixtures" / "mutation_feedback_suite"
    function = ProjectAnalyzer().analyze_function(fixture_dir / "boundary.py", "classify_boundary")
    accepted = tmp_path / "baseline_suite.py"
    accepted.write_bytes((fixture_dir / "baseline_suite.py").read_bytes())
    store = ArtifactStore(tmp_path / "runs")
    run = store.create_run()
    mutation_artifacts = store.create_mutation(run)
    baseline = backend.run(function, [accepted], mutation_artifacts.mutation_dir)
    store.save_mutation_result(mutation_artifacts, baseline)
    assert baseline.status is MutationStatus.COMPLETE
    assert baseline.survived_mutants > 0
    coverage = CoverageRunner().run(
        function, [accepted], run.run_dir / "integration-baseline-coverage"
    )
    provider = RecordingProvider(OllamaProvider())

    session = MutationFeedbackEngine(
        provider,
        Runner(),
        CoverageRunner(),
        backend,
        store,
        max_mutation_rounds=1,
        max_repair_attempts=1,
    ).run(function, run, [accepted], baseline, coverage)

    round_dir = run.run_dir / "mutation-feedback" / "round-001"
    assert len(provider.prompts) >= 1
    assert len(provider.responses) >= 1
    assert session.rounds
    assert (round_dir / "prompt.txt").is_file()
    assert (round_dir / "raw_response.txt").is_file()
    assert (round_dir / "generated_test.py").is_file()
    assert (round_dir / "result.json").is_file()
    assert len(session.accepted_test_files) in (1, 2)

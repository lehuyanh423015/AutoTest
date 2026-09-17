"""Offline repository-pilot tests with real copied-source pytest and coverage."""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from autotest.context_selector import ContextSelector, ContextTarget
from autotest.environment_provisioner import EnvironmentProvisioner, EnvironmentProvisionStatus
from autotest.llm.ollama_provider import OllamaConfig, OllamaProvider
from autotest.mutation_runner import (
    MutantOutcome,
    MutationResult,
    MutationStatus,
    SurvivingMutant,
    SurvivorEvidence,
)
from autotest.project_analyzer import ProjectAnalyzer
from autotest.project_inspector import ProjectInspector
from autotest.repository_mutation import RepositoryWSLMutmutBackend
from autotest.repository_runner import RepositoryRunEngine, RepositoryRunOptions
from autotest.test_runner import TestStatus as ExecutionStatus

FIXTURE = Path(__file__).parent / "fixtures/projects/repository_pilot"
TARGET = ContextTarget(Path("src/demo/pricing.py"), "calculate_total")


class _ScriptedProvider:
    def __init__(self, responses: list[str]):
        self.responses = responses
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.responses.pop(0)


class _CopiedProvisioner(EnvironmentProvisioner):
    """Simulate Phase 5B READY offline while still using real child pytest/coverage."""

    def provision(self, project_root, profile, plan, output_root):
        workspace = Path(output_root) / "fake-ready"
        workspace.mkdir(parents=True, exist_ok=False)
        source = workspace / "source"
        shutil.copytree(project_root, source)
        return replace(
            self._unsupported(plan),
            status=EnvironmentProvisionStatus.READY,
            workspace_root=workspace,
            source_root=source,
            python_executable=Path(sys.executable),
            copy_integrity_verified=True,
            original_integrity_verified=True,
            profile_copy_verified=True,
            target_dependencies_installed=True,
            runner_tools_installed=True,
            error_message=None,
        )


def _options(tmp_path: Path, **changes) -> RepositoryRunOptions:
    return replace(
        RepositoryRunOptions(
            output_root=tmp_path / "repository_runs",
            environment_output_root=tmp_path / "environments",
            max_repair_attempts=1,
            max_coverage_rounds=1,
        ),
        **changes,
    )


def _engine(provider: _ScriptedProvider, **kwargs) -> RepositoryRunEngine:
    return RepositoryRunEngine(lambda: provider, provisioner=_CopiedProvisioner(), **kwargs)


def test_end_to_end_repair_coverage_copy_and_existing_test_isolation(tmp_path):
    provider = _ScriptedProvider(
        [
            "from demo.pricing import missing\ndef test_bad(): assert missing(5) == 5\n",
            "import demo.pricing as pricing\n"
            "from demo.pricing import calculate_total\n"
            "def test_low():\n"
            "    assert calculate_total(5) == 5\n"
            "    assert '/source/' in pricing.__file__.replace('\\\\', '/')\n",
            "import pytest\nfrom demo.pricing import calculate_total\n"
            "def test_high(): assert calculate_total(10) == pytest.approx(11.0)\n",
        ]
    )
    result = _engine(provider).run(FIXTURE, TARGET, _options(tmp_path))
    assert result.exit_code == 0, result.error_message
    assert result.initial_execution_status is ExecutionStatus.ERROR
    assert result.final_execution_status is ExecutionStatus.PASS
    assert result.repair_count == 1
    assert result.repair_success
    assert result.initial_line_coverage is not None
    assert result.final_line_coverage == 100
    assert result.final_line_coverage > result.initial_line_coverage
    assert result.accepted_coverage_rounds == 1
    assert len(result.accepted_test_files) == 2
    assert result.source_integrity_verified
    assert result.accepted_test_integrity_verified
    assert result.environment_workspace is not None
    assert result.environment_workspace != FIXTURE
    assert result.context_status == "COMPLETE"
    assert all("from demo.pricing import calculate_total" in prompt for prompt in provider.prompts)
    assert "compute_tax" in provider.prompts[0]
    assert "Missing executable lines" in provider.prompts[2]
    assert str(FIXTURE.resolve()) not in provider.prompts[0]
    data = json.loads((result.run_directory / "result.json").read_text())
    assert data["project_profile_sha256"]
    assert data["environment_plan_sha256"]
    assert data["context_bundle_sha256"]
    assert (result.run_directory / "context/context.txt").is_file()
    assert (result.run_directory / "integrity/source_after.json").is_file()
    assert (result.run_directory / "integrity/accepted_tests.json").is_file()


def test_generated_test_source_modification_stops_pipeline(tmp_path):
    provider = _ScriptedProvider(
        [
            "import demo.pricing as p\n"
            "def test_tamper():\n"
            "    from pathlib import Path\n"
            "    Path(p.__file__).write_text('tampered')\n"
            "    assert True\n"
        ]
    )
    result = _engine(provider).run(FIXTURE, TARGET, _options(tmp_path))
    assert result.exit_code == 2
    assert result.stop_reason == "EXECUTION_SOURCE_MODIFIED"
    assert result.source_integrity_verified
    assert not result.accepted_test_integrity_verified
    assert len(provider.prompts) == 1


def test_source_cache_directory_creation_is_detected(tmp_path):
    provider = _ScriptedProvider(
        [
            "import demo.pricing as p\n"
            "def test_cache_write():\n"
            "    from pathlib import Path\n"
            "    cache = Path(p.__file__).parent / '.pytest_cache'\n"
            "    cache.mkdir()\n"
            "    (cache / 'payload').write_text('changed')\n"
            "    assert True\n"
        ]
    )
    result = _engine(provider).run(FIXTURE, TARGET, _options(tmp_path))
    assert result.stop_reason == "EXECUTION_SOURCE_MODIFIED"
    assert result.exit_code == 2


def test_candidate_modifying_accepted_test_stops_pipeline(tmp_path):
    provider = _ScriptedProvider(
        [
            "from demo.pricing import calculate_total\n"
            "def test_low(): assert calculate_total(5) == 5\n",
            "from pathlib import Path\nfrom demo.pricing import calculate_total\n"
            "def test_attack():\n"
            "    for parent in Path(__file__).resolve().parents:\n"
            "        accepted = parent / 'attempt-000/generated_test.py'\n"
            "        if accepted.is_file() and accepted.resolve() != Path(__file__).resolve():\n"
            "            accepted.write_text('def test_erased(): pass\\n')\n"
            "            break\n"
            "    assert calculate_total(10) == 11\n",
        ]
    )
    result = _engine(provider).run(FIXTURE, TARGET, _options(tmp_path, max_repair_attempts=0))
    assert result.exit_code == 2
    assert result.stop_reason == "ACCEPTED_TEST_MODIFIED"
    assert result.source_integrity_verified


def test_final_failure_and_timeout_keep_execution_status_contract(tmp_path):
    fail = _ScriptedProvider(
        [
            "from demo.pricing import calculate_total\n"
            "def test_wrong(): assert calculate_total(5) == 999\n"
        ]
    )
    failure = _engine(fail).run(FIXTURE, TARGET, _options(tmp_path, max_repair_attempts=0))
    assert failure.exit_code == 1
    assert failure.final_execution_status is ExecutionStatus.FAIL
    assert "assert" in fail.prompts[0]
    slow_root = tmp_path / "slow"
    slow_root.mkdir()
    timeout = _ScriptedProvider(["def test_wait():\n    while True: pass\n"])
    timed = _engine(timeout).run(
        FIXTURE,
        TARGET,
        replace(_options(slow_root, max_repair_attempts=0), test_timeout=1.0),
    )
    assert timed.exit_code == 3
    assert timed.final_execution_status is ExecutionStatus.TIMEOUT


def test_assertion_failure_repair_keeps_conservative_guidance(tmp_path):
    provider = _ScriptedProvider(
        [
            "from demo.pricing import calculate_total\n"
            "def test_wrong(): assert calculate_total(5) == 999\n",
            "from demo.pricing import calculate_total\n"
            "def test_correct(): assert calculate_total(5) == 5\n",
        ]
    )
    result = _engine(provider).run(FIXTURE, TARGET, _options(tmp_path, max_coverage_rounds=0))
    assert result.exit_code == 0
    assert result.initial_execution_status is ExecutionStatus.FAIL
    assert result.repair_success
    assert "Do not delete or weaken" in provider.prompts[1]
    assert "src/demo/tax.py" in provider.prompts[1]


def test_stale_context_stops_before_provider(tmp_path):
    class TamperedSelector(ContextSelector):
        def select(self, project_root, profile, target, policy):
            bundle = super().select(project_root, profile, target, policy)
            selected = list(bundle.selected_file_sha256)
            selected[0] = (selected[0][0], "0" * 64)
            return replace(bundle, selected_file_sha256=tuple(selected))

    called = False

    def provider_factory():
        nonlocal called
        called = True
        return _ScriptedProvider([])

    engine = RepositoryRunEngine(
        provider_factory, selector=TamperedSelector(), provisioner=_CopiedProvisioner()
    )
    result = engine.run(FIXTURE, TARGET, _options(tmp_path))
    assert result.exit_code == 2
    assert result.stop_reason == "STALE_CONTEXT"
    assert not called


def test_ambiguous_module_name_stops_before_provider(tmp_path):
    class AmbiguousInspector(ProjectInspector):
        def inspect(self, project_root):
            profile = super().inspect(project_root)
            modules = list(profile.modules)
            pricing = next(item for item in modules if item.path.name == "pricing.py")
            tax_index = next(
                index for index, item in enumerate(modules) if item.path.name == "tax.py"
            )
            modules[tax_index] = replace(modules[tax_index], module_name=pricing.module_name)
            return replace(profile, modules=tuple(modules))

    called = False

    def provider_factory():
        nonlocal called
        called = True
        return _ScriptedProvider([])

    result = RepositoryRunEngine(provider_factory, inspector=AmbiguousInspector()).run(
        FIXTURE, TARGET, _options(tmp_path)
    )
    assert result.exit_code == 2
    assert result.stop_reason == "INVALID_TARGET"
    assert not called


def test_mismatched_environment_identity_stops_before_provider(tmp_path):
    class WrongPlanProvisioner(_CopiedProvisioner):
        def provision(self, project_root, profile, plan, output_root):
            return replace(
                super().provision(project_root, profile, plan, output_root),
                plan_sha256="0" * 64,
            )

    called = False

    def provider_factory():
        nonlocal called
        called = True
        return _ScriptedProvider([])

    result = RepositoryRunEngine(provider_factory, provisioner=WrongPlanProvisioner()).run(
        FIXTURE, TARGET, _options(tmp_path)
    )
    assert result.exit_code == 2
    assert result.stop_reason == "ENVIRONMENT_ERROR"
    assert not called


def test_unexpected_original_checkout_change_is_reported(tmp_path):
    project = tmp_path / "original"
    shutil.copytree(FIXTURE, project)
    target_file = project / TARGET.file
    code = (
        "from pathlib import Path\n"
        f"def test_modify_original(): Path({str(target_file)!r}).write_text('changed')\n"
    )
    result = _engine(_ScriptedProvider([code])).run(
        project, TARGET, _options(tmp_path, max_repair_attempts=0, max_coverage_rounds=0)
    )
    assert result.exit_code == 2
    assert result.stop_reason == "SOURCE_INTEGRITY_ERROR"
    assert not result.source_integrity_verified


def test_unsupported_mutation_is_explicit_and_before_llm(tmp_path):
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)
    pyproject = project / "pyproject.toml"
    pyproject.write_text(pyproject.read_text() + 'dependencies = ["requests==2.32.3"]\n')
    provider = _ScriptedProvider([])
    result = _engine(provider).run(project, TARGET, _options(tmp_path, mutation=True))
    assert result.exit_code == 2
    assert result.stop_reason == "REPOSITORY_MUTATION_UNSUPPORTED"
    assert not provider.prompts


class _FakeMutationBackend:
    def __init__(self):
        self.calls = 0

    def run(self, function, tests, mutation_dir):
        self.calls += 1
        assert tests
        assert function.module_name == "demo.pricing"
        return MutationResult(
            function.file_path,
            function.function_name,
            MutationStatus.COMPLETE,
            total_mutants=1,
            killed_mutants=1,
            mutation_score_percent=100.0,
            workspace=Path(mutation_dir) / "workspace",
        )

    def extract_survivors(self, result, function, max_mutants):
        return SurvivorEvidence((), (), "")


def test_fake_repository_mutation_and_feedback(tmp_path):
    backend = _FakeMutationBackend()
    provider = _ScriptedProvider(
        [
            "from demo.pricing import calculate_total\n"
            "def test_low(): assert calculate_total(5) == 5\n"
        ]
    )
    engine = _engine(
        provider,
        mutation_backend_factory=lambda copy, profile, options: backend,
    )
    result = engine.run(
        FIXTURE,
        TARGET,
        _options(tmp_path, mutation_feedback=True, max_coverage_rounds=0),
    )
    assert result.exit_code == 0, result.error_message
    assert result.mutation_enabled and result.mutation_feedback_enabled
    assert result.mutation_score_initial == result.mutation_score_final == 100.0
    assert backend.calls == 1
    assert (result.run_directory / "mutation/mutation_result.json").is_file()


class _ImprovingMutationBackend:
    def __init__(self):
        self.calls = 0

    def run(self, function, tests, mutation_dir):
        self.calls += 1
        assert len(tests) == self.calls
        return MutationResult(
            function.file_path,
            function.function_name,
            MutationStatus.COMPLETE,
            total_mutants=1,
            killed_mutants=int(self.calls > 1),
            survived_mutants=int(self.calls == 1),
            mutation_score_percent=100.0 if self.calls > 1 else 0.0,
            backend_version="3.7.0",
            hashes={"target_source_sha256": "stable", "mutation_config_sha256": "stable"},
            workspace=Path(mutation_dir) / "workspace",
        )

    def extract_survivors(self, result, function, max_mutants):
        mutant_id = "demo.pricing.x_calculate_total__mutmut_1"
        status = "survived" if result.survived_mutants else "killed"
        selected = (
            (SurvivingMutant(mutant_id, function.function_name, "- original\n+ mutant\n"),)
            if result.survived_mutants
            else ()
        )
        return SurvivorEvidence((MutantOutcome(mutant_id, status),), selected, status)


def test_repository_mutation_feedback_accepts_improving_additional_test(tmp_path):
    backend = _ImprovingMutationBackend()
    provider = _ScriptedProvider(
        [
            "from demo.pricing import calculate_total\n"
            "def test_low(): assert calculate_total(5) == 5\n",
            "from demo.pricing import calculate_total\n"
            "def test_high(): assert calculate_total(10) == 11.0\n",
        ]
    )
    result = _engine(provider, mutation_backend_factory=lambda copy, profile, options: backend).run(
        FIXTURE,
        TARGET,
        _options(tmp_path, mutation_feedback=True, max_coverage_rounds=0),
    )
    assert result.exit_code == 0, result.error_message
    assert result.mutation_score_initial == 0.0
    assert result.mutation_score_final == 100.0
    assert result.mutation_killed == 1 and result.mutation_survived == 0
    assert len(result.accepted_test_files) == 2
    assert result.llm_calls["mutation_feedback"] == 1
    assert backend.calls == 2
    assert (result.run_directory / "mutation-feedback/round-001/result.json").is_file()


def test_repository_mutation_workspace_preserves_local_layout_only(tmp_path):
    project = tmp_path / "source"
    shutil.copytree(FIXTURE, project)
    profile = ProjectInspector().inspect(project)
    backend = RepositoryWSLMutmutBackend(project, profile)
    generated = tmp_path / "generated_test.py"
    generated.write_text("def test_example(): assert True\n")
    copied, tests, config = backend._prepare_workspace(
        replace(
            ProjectAnalyzer().analyze_function(project / TARGET.file, TARGET.function),
            module_name="demo.pricing",
        ),
        (generated,),
        tmp_path / "mutation" / "workspace",
    )
    assert copied.is_file()
    assert (copied.parent / "tax.py").is_file()
    assert not (copied.parents[2] / "tests/conftest.py").exists()
    assert len(tests) == 1
    assert 'source_paths = ["src/demo/pricing.py"]' in config.read_text()
    assert "src/demo/tax.py" in config.read_text()


def test_real_target_venv_executes_generated_pytest_and_coverage(tmp_path):
    if os.environ.get("AUTOTEST_RUN_REPOSITORY_VENV") != "1":
        pytest.skip("set AUTOTEST_RUN_REPOSITORY_VENV=1 for the real Phase 5B integration")
    provider = _ScriptedProvider(
        [
            "import demo.pricing as pricing\n"
            "from demo.pricing import calculate_total\n"
            "def test_low():\n"
            "    assert calculate_total(5) == 5\n"
            "    assert '/source/' in pricing.__file__.replace('\\\\', '/')\n",
            "import pytest\nfrom demo.pricing import calculate_total\n"
            "def test_high(): assert calculate_total(10) == pytest.approx(11.0)\n",
        ]
    )
    engine = RepositoryRunEngine(lambda: provider)
    result = engine.run(FIXTURE, TARGET, _options(tmp_path, max_repair_attempts=0))
    assert result.exit_code == 0, result.error_message
    assert result.environment_status == "READY"
    assert result.target_python is not None
    assert result.target_python.resolve() != Path(sys.executable).resolve()
    assert result.initial_line_coverage is not None
    assert result.final_line_coverage == 100
    assert result.source_integrity_verified and result.accepted_test_integrity_verified


def test_real_wsl_repository_mutation(tmp_path):
    if os.environ.get("AUTOTEST_RUN_REPOSITORY_MUTATION") != "1":
        pytest.skip("set AUTOTEST_RUN_REPOSITORY_MUTATION=1 for WSL Mutmut integration")
    provider = _ScriptedProvider(
        [
            "import pytest\nfrom demo.pricing import calculate_total\n"
            "def test_low(): assert calculate_total(5) == 5\n"
            "def test_high(): assert calculate_total(10) == pytest.approx(11.0)\n",
        ]
    )
    result = RepositoryRunEngine(lambda: provider).run(
        FIXTURE,
        TARGET,
        _options(tmp_path, mutation=True, max_repair_attempts=0, max_coverage_rounds=0),
    )
    assert result.exit_code == 0, result.error_message
    assert result.mutation_score_initial is not None
    assert result.mutation_score_final is not None
    assert result.source_integrity_verified
    assert (result.run_directory / "mutation/mutmut_raw_stats.json").is_file()


def test_real_ollama_repository_pilot(tmp_path):
    if os.environ.get("AUTOTEST_RUN_REPOSITORY_OLLAMA") != "1":
        pytest.skip("set AUTOTEST_RUN_REPOSITORY_OLLAMA=1 for the live model pilot")
    engine = RepositoryRunEngine(
        lambda: OllamaProvider(OllamaConfig(model="qwen2.5-coder:14b", timeout=120.0))
    )
    result = engine.run(
        FIXTURE,
        TARGET,
        _options(tmp_path, max_repair_attempts=1, max_coverage_rounds=0),
    )
    assert result.environment_status == "READY"
    assert result.initial_execution_status is not None
    assert (result.run_directory / "attempt-000/raw_response.txt").is_file()
    assert result.final_execution_status is ExecutionStatus.PASS, result.error_message
    assert result.final_line_coverage is not None

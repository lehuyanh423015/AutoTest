"""Offline Phase 6 matrix, statistics, and checkpoint tests."""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from autotest.environment_provisioner import EnvironmentProvisioner, EnvironmentProvisionStatus
from autotest.experiment import ExperimentError, load_definition, plan_experiment
from autotest.experiment_metrics import ExperimentRunRecord, aggregate, describe
from autotest.experiment_runner import ExperimentRunner
from autotest.main import main
from autotest.mutation_runner import MutationResult, MutationStatus, SurvivorEvidence
from autotest.repository_runner import RepositoryRunEngine

FIXTURE = Path(__file__).parent / "fixtures/projects/repository_pilot"


class CopiedProvisioner(EnvironmentProvisioner):
    def provision(self, project_root, profile, plan, output_root):
        workspace = Path(output_root) / "ready"
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


class ScriptedProvider:
    def __init__(self, target: str):
        self.target = target
        self.calls = []

    def generate(self, prompt):
        self.calls.append(prompt)
        name = self.target
        if "Missing executable lines" in prompt:
            return f"from demo.pricing import {name}\ndef test_high(): assert {name}(10) >= 10\n"
        return f"from demo.pricing import {name}\ndef test_low(): assert {name}(5) == 5\n"


class MutationBackend:
    def run(self, function, tests, mutation_dir):
        return MutationResult(
            function.file_path,
            function.function_name,
            MutationStatus.COMPLETE,
            total_mutants=1,
            killed_mutants=1,
            tool_total_mutants=2,
            unreported_mutants=1,
            mutation_score_percent=100.0,
        )

    def extract_survivors(self, result, function, max_mutants):
        return SurvivorEvidence((), (), "")


def manifest(tmp_path, *, repetitions=2, fail_fast=False):
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "experiment_name": "offline",
                "repetitions": repetitions,
                "model": "scripted",
                "temperature": 0,
                "evaluate_mutation": True,
                "fail_fast": fail_fast,
                "max_repair_attempts": 1,
                "max_coverage_rounds": 1,
                "repositories": [
                    {
                        "id": "pilot",
                        "path": str(FIXTURE),
                        "targets": [
                            {"id": "total", "target": "src/demo/pricing.py:calculate_total"},
                            {"id": "normal", "target": "src/demo/pricing.py:normalize_amount"},
                        ],
                    }
                ],
                "configurations": ["direct", "execution", "coverage", "mutation"],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_manifest_validation_and_canonical_options(tmp_path):
    path = manifest(tmp_path)
    definition = load_definition(path)
    plan = plan_experiment(definition)
    assert len(plan.specs) == 16
    assert len({spec.run_id for spec in plan.specs}) == 16
    assert all(spec.support_status == "SUPPORTED" for spec in plan.specs)
    by_id = {item.id: item.options(definition, tmp_path) for item in definition.configurations}
    assert by_id["direct"].max_repair_attempts == 0
    assert by_id["direct"].max_coverage_rounds == 0
    assert by_id["direct"].mutation and not by_id["direct"].mutation_feedback
    assert by_id["execution"].max_repair_attempts == 1
    assert by_id["execution"].max_coverage_rounds == 0
    assert by_id["coverage"].max_coverage_rounds == 1
    assert not by_id["coverage"].mutation_feedback
    assert by_id["mutation"].mutation_feedback
    assert {spec.profile_sha256 for spec in plan.specs} == {plan.specs[0].profile_sha256}


@pytest.mark.parametrize(
    "change",
    [
        {"repetitions": 0},
        {"experiment_name": ""},
        {"configurations": []},
        {"configurations": ["unknown"]},
        {"configurations": ["direct", "direct"]},
        {"repositories": []},
        {"max_repair_attempts": 0},
        {"max_coverage_rounds": 0},
        {"max_mutation_rounds": 0},
    ],
)
def test_invalid_manifest(tmp_path, change):
    path = manifest(tmp_path)
    value = json.loads(path.read_text())
    value.update(change)
    path.write_text(json.dumps(value))
    with pytest.raises(ExperimentError):
        load_definition(path)


@pytest.mark.parametrize("mutation", ["repository", "target", "syntax"])
def test_ambiguous_ids_and_target_syntax_are_rejected(tmp_path, mutation):
    path = manifest(tmp_path)
    value = json.loads(path.read_text())
    if mutation == "repository":
        value["repositories"].append(value["repositories"][0])
    elif mutation == "target":
        value["repositories"][0]["targets"].append(value["repositories"][0]["targets"][0])
    else:
        value["repositories"][0]["targets"][0]["target"] = "../outside.py:run"
    path.write_text(json.dumps(value))
    with pytest.raises(ExperimentError):
        load_definition(path)


def test_missingness_and_exact_descriptive_statistics():
    assert describe([80, None, 100]) == {
        "n": 2,
        "mean": 90,
        "median": 90,
        "sample_stddev": pytest.approx(14.142135623730951),
        "min": 80,
        "max": 100,
    }
    rows = []
    for score in (80, None, 100):
        rows.append(
            ExperimentRunRecord(
                {
                    "run_status": "COMPLETED",
                    "initial_execution_status": "FAIL",
                    "final_execution_status": "FAIL",
                    "final_mutation_score": score,
                    "repair_attempts": 0,
                    "repair_success": False,
                    "final_line_coverage": None,
                    "final_branch_coverage": None,
                    "mutation_survived": 0 if score is not None else None,
                    **{
                        key: None
                        for key in (
                            "initial_line_coverage",
                            "line_coverage_gain",
                            "initial_branch_coverage",
                            "branch_coverage_gain",
                            "initial_mutation_score",
                            "mutation_score_gain",
                            "mutation_killed",
                            "llm_calls_total",
                            "llm_calls_initial_generation",
                            "llm_calls_execution_repair",
                            "llm_calls_coverage_generation",
                            "llm_calls_coverage_repair",
                            "llm_calls_mutation_feedback",
                            "llm_calls_mutation_repair",
                            "environment_duration",
                            "generation_duration",
                            "execution_duration",
                            "coverage_duration",
                            "mutation_duration",
                            "total_duration",
                        )
                    },
                }
            )
        )
    summary = aggregate(rows)
    assert summary["metrics"]["final_mutation_score"]["n"] == 2
    assert summary["metrics"]["final_mutation_score"]["mean"] == 90
    assert summary["final_pass"] == {"n": 3, "rate": 0}
    assert summary["final_executable"] == {"n": 3, "rate": 1}


def test_offline_matrix_resume_and_isolation(tmp_path):
    path = manifest(tmp_path)
    providers = []
    invoked = []

    def factory(spec):
        invoked.append(spec.run_id)

        def new_provider():
            provider = ScriptedProvider(spec.target.split(":")[1])
            providers.append((spec.run_id, provider))
            return provider

        return RepositoryRunEngine(
            new_provider,
            provisioner=CopiedProvisioner(),
            mutation_backend_factory=lambda copy, profile, options: MutationBackend(),
        )

    def progress(message):
        if message.startswith("[7/16]"):
            raise KeyboardInterrupt

    runner = ExperimentRunner(factory, progress=progress)
    with pytest.raises(KeyboardInterrupt):
        runner.run(load_definition(path), tmp_path / "outputs", manifest=path)
    directory = tmp_path / "outputs" / "offline"
    checkpoint = json.loads((directory / "experiment_state.json").read_text())
    assert checkpoint["completed"] == 6
    assert checkpoint["remaining"] == 10
    first_six = invoked[:]
    result = ExperimentRunner(factory, progress=lambda message: None).resume(directory)
    assert result.complete
    assert len(result.records) == 16
    assert len(invoked) == 16
    assert invoked[:6] == first_six
    assert len({item.values["run_id"] for item in result.records}) == 16
    assert all(item.values["run_status"] == "COMPLETED" for item in result.records)
    assert all(item.values["repository_run_artifact"] for item in result.records)
    for run_id, provider in providers:
        assert provider.calls
        if "::direct::" in run_id:
            assert len(provider.calls) == 1
    for name in ("runs.json", "runs.csv", "aggregates.json", "aggregates.csv", "failures.csv"):
        assert (directory / "results" / name).is_file()
    assert (directory / "summary.json").is_file()
    data = json.loads((directory / "results" / "runs.json").read_text())
    assert len(data["records"]) == 16
    assert all(item["mutation_raw_total"] == 2 for item in data["records"])
    assert all(item["mutation_applicable_total"] == 1 for item in data["records"])
    assert all(item["mutation_other_or_unreported"] == 1 for item in data["records"])
    direct = next(
        item
        for item in data["records"]
        if item["target_id"] == "total" and item["configuration"] == "direct"
    )
    assert direct["initial_execution_status"] == direct["final_execution_status"] == "PASS"
    assert direct["repair_attempts"] == 0
    assert direct["initial_line_coverage"] == direct["final_line_coverage"]
    assert direct["line_coverage_gain"] == 0
    assert direct["initial_mutation_score"] == direct["final_mutation_score"] == 100
    assert direct["llm_calls_total"] == 1
    assert direct["target_python_version"]
    assert direct["total_duration"] > 0
    assert direct["source_integrity_verified"] and direct["accepted_test_integrity_verified"]
    assert Path(direct["repository_run_artifact"]).is_file()
    for item in data["records"]:
        if item["configuration"] == "direct":
            assert item["llm_calls_execution_repair"] == 0
            assert item["llm_calls_coverage_generation"] == 0
            assert item["llm_calls_mutation_feedback"] == 0
        if item["configuration"] == "execution":
            assert item["llm_calls_coverage_generation"] == 0
            assert item["llm_calls_mutation_feedback"] == 0
        if item["configuration"] == "coverage":
            assert item["llm_calls_mutation_feedback"] == 0
    assert main(["--resume-experiment", str(directory)]) == 0
    value = json.loads(path.read_text())
    value["temperature"] = 0.5
    path.write_text(json.dumps(value))
    with pytest.raises(ExperimentError, match="RESUME_IDENTITY_MISMATCH"):
        ExperimentRunner(factory, progress=lambda message: None).resume(directory)


def test_unsupported_target_continues_and_fail_fast_checkpoints(tmp_path):
    path = manifest(tmp_path, repetitions=1)
    value = json.loads(path.read_text())
    value["configurations"] = ["direct"]
    value["repositories"][0]["targets"].insert(
        0, {"id": "absent", "target": "src/demo/pricing.py:missing_function"}
    )
    path.write_text(json.dumps(value))
    calls = []

    def factory(spec):
        calls.append(spec.run_id)
        return RepositoryRunEngine(
            lambda: ScriptedProvider(spec.target.split(":")[1]),
            provisioner=CopiedProvisioner(),
        )

    complete = ExperimentRunner(factory, progress=lambda message: None).run(
        load_definition(path), tmp_path / "results", manifest=path
    )
    assert len(complete.records) == 3
    assert sum(record.values["run_status"] == "UNSUPPORTED" for record in complete.records) == 1
    assert len(calls) == 2
    failures = (complete.directory / "results/failures.csv").read_text()
    assert "UNSUPPORTED" in failures

    value["experiment_name"] = "failfast"
    value["fail_fast"] = True
    path.write_text(json.dumps(value))
    calls.clear()
    stopped = ExperimentRunner(factory, progress=lambda message: None).run(
        load_definition(path), tmp_path / "results", manifest=path
    )
    assert len(stopped.records) == 1
    assert stopped.records[0].values["run_status"] == "UNSUPPORTED"
    assert not calls
    state = json.loads((stopped.directory / "experiment_state.json").read_text())
    assert state["remaining"] == 2


def test_direct_failing_suite_is_completed_research_data(tmp_path):
    path = manifest(tmp_path, repetitions=1)
    value = json.loads(path.read_text())
    value["configurations"] = ["direct"]
    value["repositories"][0]["targets"] = value["repositories"][0]["targets"][:1]
    path.write_text(json.dumps(value))

    class WrongProvider:
        def generate(self, prompt):
            return (
                "from demo.pricing import calculate_total\n"
                "def test_wrong(): assert calculate_total(5) == 999\n"
            )

    result = ExperimentRunner(
        lambda spec: RepositoryRunEngine(lambda: WrongProvider(), provisioner=CopiedProvisioner()),
        progress=lambda message: None,
    ).run(load_definition(path), tmp_path / "results", manifest=path)
    assert result.complete
    assert result.records[0].values["run_status"] == "COMPLETED"
    assert result.records[0].values["final_execution_status"] == "FAIL"
    assert "FAIL" in (result.directory / "results/runs.csv").read_text()


def test_experiment_validation_cli_has_no_run_side_effects(tmp_path):
    path = manifest(tmp_path)
    assert main(["--validate-experiment", str(path)]) == 0
    assert not (tmp_path / "workspace").exists()
    value = json.loads(path.read_text())
    value["repetitions"] = 0
    path.write_text(json.dumps(value))
    assert main(["--validate-experiment", str(path)]) == 2
    with pytest.raises(SystemExit):
        main(["--validate-experiment", str(path), "--run-experiment", str(path)])


def test_experiment_cli_passes_host_distro_without_changing_plan(tmp_path, monkeypatch):
    path = manifest(tmp_path, repetitions=1)
    definition = load_definition(path)
    monkeypatch.delenv("AUTOTEST_MUTATION_WSL_DISTRO", raising=False)
    baseline = plan_experiment(definition).sha256
    monkeypatch.setenv("AUTOTEST_MUTATION_WSL_DISTRO", "Ubuntu")
    assert plan_experiment(definition).sha256 == baseline
    seen = {}

    class FakeEngine:
        def __init__(self, provider_factory, **kwargs):
            seen.update(kwargs)

    class FakeRunner:
        def __init__(self, factory):
            factory(SimpleNamespace(model="scripted", temperature=0))

        def run(self, definition, output_root, *, manifest):
            (tmp_path / "summary.json").write_text(
                json.dumps(
                    {
                        "planned_runs": 0,
                        "completed_runs": 0,
                        "unsupported_runs": 0,
                        "pipeline_failures": 0,
                        "timeouts": 0,
                        "primary_aggregate": {},
                    }
                )
            )
            return SimpleNamespace(
                directory=tmp_path, records=(), plan=SimpleNamespace(specs=()), complete=True
            )

    monkeypatch.setattr("autotest.main.RepositoryRunEngine", FakeEngine)
    monkeypatch.setattr("autotest.main.ExperimentRunner", FakeRunner)
    assert (
        main(
            [
                "--run-experiment",
                str(path),
                "--mutation-wsl-distribution",
                "Ubuntu-22.04",
            ]
        )
        == 0
    )
    assert seen == {"mutation_distro": "Ubuntu-22.04"}

from __future__ import annotations

from pathlib import Path

from autotest.artifact_store import ArtifactStore
from autotest.coverage_runner import CoverageResult
from autotest.llm.base import LLMProvider
from autotest.mutation_feedback import MutationFeedbackEngine, MutationFeedbackStopReason
from autotest.mutation_runner import (
    MutantOutcome,
    MutationResult,
    MutationStatus,
    SurvivingMutant,
    SurvivorEvidence,
    calculate_mutation_score,
)
from autotest.project_analyzer import FunctionInfo
from autotest.test_runner import TestRunResult as RunResult
from autotest.test_runner import TestStatus as RunStatus


class ScriptedProvider(LLMProvider):
    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = responses
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class MutatingProvider(LLMProvider):
    def __init__(self, path: Path) -> None:
        self.path = path

    def generate(self, prompt: str) -> str:
        self.path.write_text("changed\n", encoding="utf-8")
        return "def test_candidate():\n    assert 1 == 1\n"


class PassingRunner:
    timeout = 1.0

    def __init__(self, statuses: list[RunStatus] | None = None) -> None:
        self.statuses = statuses or []
        self.suites: list[tuple[Path, ...]] = []

    def run(self, test_file: Path | str, project_root: Path | str) -> RunResult:
        return self.run_suite((test_file,), project_root)

    def run_suite(self, test_files, project_root) -> RunResult:
        paths = tuple(Path(path).resolve() for path in test_files)
        self.suites.append(paths)
        status = self.statuses.pop(0) if self.statuses else RunStatus.PASS
        code = 0 if status is RunStatus.PASS else 1
        return RunResult(paths[-1], code, status, "pytest", "", 0.1)


class FakeCoverageRunner:
    def __init__(self, results: list[CoverageResult]) -> None:
        self.results = results

    def run(self, function, test_files, output_dir) -> CoverageResult:
        Path(output_dir).mkdir(parents=True, exist_ok=False)
        return self.results.pop(0)


class FakeMutationBackend:
    def __init__(
        self,
        outcomes: dict[int, tuple[tuple[str, str], ...]],
        reruns: list[MutationResult],
    ) -> None:
        self.outcomes = outcomes
        self.reruns = reruns
        self.suites: list[tuple[Path, ...]] = []

    def run(self, function, test_files, mutation_dir) -> MutationResult:
        self.suites.append(tuple(Path(path).resolve() for path in test_files))
        return self.reruns.pop(0)

    def extract_survivors(self, result, function, max_mutants) -> SurvivorEvidence:
        raw_outcomes = self.outcomes[id(result)]
        outcomes = tuple(MutantOutcome(mutant_id, status) for mutant_id, status in raw_outcomes)
        selected = tuple(
            SurvivingMutant(mutant_id, function.function_name, f"diff for {mutant_id}\n")
            for mutant_id, status in raw_outcomes
            if status == "survived"
        )[:max_mutants]
        raw = "".join(f"    {mutant_id}: {status}\n" for mutant_id, status in raw_outcomes)
        return SurvivorEvidence(outcomes, selected, raw)


class FailingRerunBackend(FakeMutationBackend):
    def run(self, function, test_files, mutation_dir) -> MutationResult:
        self.suites.append(tuple(Path(path).resolve() for path in test_files))
        return MutationResult(
            function.file_path,
            function.function_name,
            MutationStatus.TOOL_ERROR,
            error_message="rerun failed",
        )


def fixture(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "boundary.py"
    source.write_text(
        'def classify(value):\n    if value >= 10:\n        return "large"\n    return "small"\n',
        encoding="utf-8",
    )
    accepted = tmp_path / "test_baseline.py"
    accepted.write_text(
        "from boundary import classify\n\n"
        'def test_baseline():\n    assert classify(9) == "small"\n',
        encoding="utf-8",
    )
    function = FunctionInfo(
        source.resolve(),
        "boundary",
        "classify",
        source.read_text(encoding="utf-8").rstrip(),
        1,
        4,
        None,
    )
    coverage = CoverageResult(
        source.resolve(),
        "classify",
        (1, 2, 3, 4),
        (1, 2, 3, 4),
        (),
        (),
        (),
        100.0,
        None,
        None,
        0.1,
    )
    return function, accepted, coverage


def mutation(function: FunctionInfo, statuses: tuple[str, ...]) -> MutationResult:
    killed = statuses.count("killed")
    survived = statuses.count("survived")
    return MutationResult(
        function.file_path,
        function.function_name,
        MutationStatus.COMPLETE,
        total_mutants=len(statuses),
        killed_mutants=killed,
        survived_mutants=survived,
        tool_total_mutants=len(statuses),
        mutation_score_percent=calculate_mutation_score(killed, survived),
        backend_version="3.7.0",
        hashes={"target_source_sha256": "source", "mutation_config_sha256": "config"},
    )


def ids(statuses: tuple[str, ...], *, prefix: str = "boundary.x_classify__mutmut_"):
    return tuple((f"{prefix}{index}", status) for index, status in enumerate(statuses, 1))


def run_engine(
    tmp_path: Path,
    baseline_statuses: tuple[str, ...],
    rerun_statuses: list[tuple[str, ...]],
    *,
    coverage_per_round: list[CoverageResult] | None = None,
    responses: list[str | Exception] | None = None,
    runner_statuses: list[RunStatus] | None = None,
    max_rounds: int = 3,
    rerun_ids: list[tuple[tuple[str, str], ...]] | None = None,
):
    function, accepted, coverage = fixture(tmp_path)
    baseline = mutation(function, baseline_statuses)
    reruns = [mutation(function, statuses) for statuses in rerun_statuses]
    evidence = {id(baseline): ids(baseline_statuses)}
    for index, item in enumerate(reruns):
        evidence[id(item)] = (
            rerun_ids[index] if rerun_ids is not None else ids(rerun_statuses[index])
        )
    backend = FakeMutationBackend(evidence, reruns)
    provider = ScriptedProvider(
        responses
        or [
            "from boundary import classify\n\n"
            'def test_boundary():\n    assert classify(10) == "large"\n'
            for _ in rerun_statuses
        ]
    )
    runner = PassingRunner(runner_statuses)
    store = ArtifactStore(tmp_path / "runs")
    run = store.create_run()
    coverages = coverage_per_round or [coverage for _ in rerun_statuses]
    result = MutationFeedbackEngine(
        provider,
        runner,
        FakeCoverageRunner(coverages),
        backend,
        store,
        max_mutation_rounds=max_rounds,
        max_repair_attempts=1,
    ).run(function, run, [accepted], baseline, coverage)
    return result, provider, runner, backend, run, accepted


def test_all_killed_and_zero_round_modes_make_zero_feedback_calls(tmp_path: Path) -> None:
    result, provider, *_ = run_engine(tmp_path / "all", ("killed",), [], responses=[])
    assert result.stop_reason is MutationFeedbackStopReason.ALL_MUTANTS_KILLED
    assert provider.prompts == []

    result, provider, *_ = run_engine(
        tmp_path / "zero", ("killed", "survived"), [], responses=[], max_rounds=0
    )
    assert result.stop_reason is MutationFeedbackStopReason.MAX_ROUNDS_REACHED
    assert provider.prompts == []


def test_passing_candidate_with_flat_coverage_and_mutation_gain_is_accepted(
    tmp_path: Path,
) -> None:
    result, provider, _, _, run, accepted = run_engine(
        tmp_path, ("killed", "survived"), [("killed", "killed")]
    )

    assert result.stop_reason is MutationFeedbackStopReason.ALL_MUTANTS_KILLED
    assert result.accepted_round_count == 1
    assert result.mutation_score_gain == 50.0
    assert result.final_coverage.line_coverage_percent == 100.0
    assert len(result.accepted_test_files) == 2
    assert accepted.read_text(encoding="utf-8").startswith("from boundary")
    assert len(provider.prompts) == 1
    assert (
        run.run_dir / "mutation-feedback" / "baseline" / "survivors" / "survivors_raw.txt"
    ).is_file()
    assert (run.run_dir / "mutation-feedback" / "round-001" / "result.json").is_file()


def test_non_improving_candidate_is_rejected(tmp_path: Path) -> None:
    result, _, _, _, _, _ = run_engine(tmp_path, ("killed", "survived"), [("killed", "survived")])
    assert result.stop_reason is MutationFeedbackStopReason.NO_MUTATION_IMPROVEMENT
    assert result.accepted_round_count == 0
    assert len(result.accepted_test_files) == 1


def test_mutation_regression_is_rejected(tmp_path: Path) -> None:
    result, *_ = run_engine(tmp_path, ("killed", "survived"), [("survived", "killed")])
    assert result.stop_reason is MutationFeedbackStopReason.MUTATION_REGRESSION
    assert result.rounds[0].regressed_ids == ("boundary.x_classify__mutmut_1",)


def test_mutant_universe_change_is_rejected(tmp_path: Path) -> None:
    changed = (("boundary.x_classify__mutmut_1", "killed"), ("different", "killed"))
    result, *_ = run_engine(
        tmp_path,
        ("killed", "survived"),
        [("killed", "killed")],
        rerun_ids=[changed],
    )
    assert result.stop_reason is MutationFeedbackStopReason.MUTANT_SET_CHANGED


def test_coverage_regression_is_rejected(tmp_path: Path) -> None:
    function, _, _ = fixture(tmp_path / "data")
    lower = CoverageResult(
        function.file_path,
        "classify",
        (1, 2, 3, 4),
        (1, 2, 3),
        (4,),
        (),
        (),
        75.0,
        None,
        None,
        0.1,
    )
    result, *_ = run_engine(
        tmp_path / "run",
        ("killed", "survived"),
        [("killed", "killed")],
        coverage_per_round=[lower],
    )
    assert result.stop_reason is MutationFeedbackStopReason.COVERAGE_REGRESSION


def test_candidate_repair_is_bounded_and_exhaustion_rejects(tmp_path: Path) -> None:
    result, provider, runner, *_ = run_engine(
        tmp_path,
        ("killed", "survived"),
        [("killed", "killed")],
        responses=[
            "def test_bad():\n    assert False\n",
            "def test_still_bad():\n    assert False\n",
        ],
        runner_statuses=[RunStatus.FAIL, RunStatus.FAIL],
    )
    assert result.stop_reason is MutationFeedbackStopReason.CANDIDATE_NOT_ACCEPTED
    assert len(provider.prompts) == 2
    assert len(runner.suites) == 2


def test_candidate_failure_can_be_repaired_then_accepted(tmp_path: Path) -> None:
    result, provider, runner, *_ = run_engine(
        tmp_path,
        ("killed", "survived"),
        [("killed", "killed")],
        responses=[
            "def test_bad():\n    assert False\n",
            "from boundary import classify\n\n"
            'def test_boundary():\n    assert classify(10) == "large"\n',
        ],
        runner_statuses=[RunStatus.FAIL, RunStatus.PASS],
    )
    assert result.stop_reason is MutationFeedbackStopReason.ALL_MUTANTS_KILLED
    assert result.rounds[0].accepted
    assert result.candidate_repair_count == 1
    assert len(provider.prompts) == 2
    assert len(runner.suites) == 2


def test_two_rounds_are_cumulative_and_never_produce_a_third(tmp_path: Path) -> None:
    result, provider, _, backend, _, _ = run_engine(
        tmp_path,
        ("killed", "survived", "survived"),
        [("killed", "killed", "survived"), ("killed", "killed", "killed")],
        max_rounds=2,
    )
    assert result.stop_reason is MutationFeedbackStopReason.ALL_MUTANTS_KILLED
    assert len(provider.prompts) == 2
    assert [len(suite) for suite in backend.suites] == [2, 3]


def test_llm_failure_stops_safely(tmp_path: Path) -> None:
    result, *_ = run_engine(
        tmp_path,
        ("killed", "survived"),
        [],
        responses=[RuntimeError("offline")],
    )
    assert result.stop_reason is MutationFeedbackStopReason.LLM_ERROR
    assert result.feedback_llm_calls == 1


def test_baseline_mutation_failure_makes_no_feedback_call(tmp_path: Path) -> None:
    function, accepted, coverage = fixture(tmp_path)
    baseline = MutationResult(
        function.file_path,
        function.function_name,
        MutationStatus.TOOL_ERROR,
        error_message="backend failed",
    )
    provider = ScriptedProvider([])
    store = ArtifactStore(tmp_path / "runs")

    result = MutationFeedbackEngine(
        provider,
        PassingRunner(),
        FakeCoverageRunner([]),
        FakeMutationBackend({}, []),
        store,
    ).run(function, store.create_run(), [accepted], baseline, coverage)

    assert result.stop_reason is MutationFeedbackStopReason.MUTATION_ERROR
    assert provider.prompts == []


def test_mutation_rerun_failure_rejects_candidate_and_stops(tmp_path: Path) -> None:
    function, accepted, coverage = fixture(tmp_path)
    baseline = mutation(function, ("killed", "survived"))
    backend = FailingRerunBackend({id(baseline): ids(("killed", "survived"))}, [])
    provider = ScriptedProvider(
        [
            "from boundary import classify\n\n"
            'def test_boundary():\n    assert classify(10) == "large"\n'
        ]
    )
    store = ArtifactStore(tmp_path / "runs")

    result = MutationFeedbackEngine(
        provider,
        PassingRunner(),
        FakeCoverageRunner([coverage]),
        backend,
        store,
    ).run(function, store.create_run(), [accepted], baseline, coverage)

    assert result.stop_reason is MutationFeedbackStopReason.MUTATION_ERROR
    assert result.accepted_round_count == 0
    assert len(result.accepted_test_files) == 1


def test_source_integrity_change_stops_as_pipeline_error(tmp_path: Path) -> None:
    function, accepted, coverage = fixture(tmp_path)
    baseline = mutation(function, ("killed", "survived"))
    backend = FakeMutationBackend({id(baseline): ids(("killed", "survived"))}, [])
    store = ArtifactStore(tmp_path / "runs")

    result = MutationFeedbackEngine(
        MutatingProvider(function.file_path),
        PassingRunner(),
        FakeCoverageRunner([]),
        backend,
        store,
    ).run(function, store.create_run(), [accepted], baseline, coverage)

    assert result.stop_reason is MutationFeedbackStopReason.PIPELINE_ERROR
    assert result.accepted_round_count == 0

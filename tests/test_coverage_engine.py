import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from autotest.artifact_store import ArtifactStore
from autotest.coverage_engine import CoverageEngine, CoverageStopReason
from autotest.coverage_runner import CoverageResult
from autotest.failure_analyzer import FailureAnalyzer
from autotest.llm.base import LLMProvider
from autotest.project_analyzer import FunctionInfo
from autotest.repair_engine import AttemptKind, RepairSessionResult, StopReason
from autotest.repair_engine import TestAttempt as Attempt
from autotest.test_quality import analyze_test_structure
from autotest.test_runner import TestRunResult as RunResult
from autotest.test_runner import TestStatus as RunStatus


class ScriptedProvider(LLMProvider):
    def __init__(self, responses: Sequence[str | Exception]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ScriptedRunner:
    def __init__(self, statuses: Sequence[RunStatus]) -> None:
        self.statuses = list(statuses)
        self.suites: list[tuple[Path, ...]] = []
        self.timeout = 30.0

    def run(self, test_file: Path | str, project_root: Path | str) -> RunResult:
        return self.run_suite((test_file,), project_root)

    def run_suite(self, test_files: Sequence[Path | str], project_root: Path | str) -> RunResult:
        paths = tuple(Path(path) for path in test_files)
        self.suites.append(paths)
        status = self.statuses.pop(0)
        code = {RunStatus.PASS: 0, RunStatus.FAIL: 1, RunStatus.ERROR: 2}.get(status)
        return RunResult(paths[-1], code, status, status.value, "", 0.1)


class ScriptedCoverageRunner:
    def __init__(self, results: Sequence[CoverageResult]) -> None:
        self.results = list(results)
        self.suites: list[tuple[Path, ...]] = []

    def run(
        self,
        function: FunctionInfo,
        test_files: Sequence[Path | str],
        output_dir: Path | str,
    ) -> CoverageResult:
        paths = tuple(Path(path) for path in test_files)
        self.suites.append(paths)
        result = self.results.pop(0)
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        raw = directory / "coverage_raw.json"
        normalized = directory / "coverage_result.json"
        raw.write_text('{"meta": {"branch_coverage": true}, "files": {}}\n', encoding="utf-8")
        normalized.write_text(
            json.dumps({"line_coverage_percent": result.line_coverage_percent}),
            encoding="utf-8",
        )
        (directory / "stdout.txt").write_text("coverage output", encoding="utf-8")
        (directory / "stderr.txt").write_text("", encoding="utf-8")
        return CoverageResult(
            result.target_file,
            result.function_name,
            result.executable_lines,
            result.executed_lines,
            result.missing_lines,
            result.executed_branches,
            result.missing_branches,
            result.line_coverage_percent,
            result.branch_coverage_percent,
            raw,
            result.duration_seconds,
        )


def make_function(tmp_path: Path) -> FunctionInfo:
    source = tmp_path / "branching.py"
    source.write_text(
        "def classify(value):\n"
        "    if value < 0:\n"
        "        return 'negative'\n"
        "    if value == 0:\n"
        "        return 'zero'\n"
        "    return 'positive'\n",
        encoding="utf-8",
    )
    return FunctionInfo(
        source.resolve(),
        "branching",
        "classify",
        source.read_text(encoding="utf-8").rstrip(),
        1,
        6,
        None,
    )


def coverage(function: FunctionInfo, percent: float, branch: float | None = None) -> CoverageResult:
    if percent >= 100:
        executed, missing = (1, 2, 3, 4), ()
    elif percent >= 75:
        executed, missing = (1, 2, 3), (4,)
    else:
        executed, missing = (1, 2), (3, 4)
    if branch is None:
        executed_branches = missing_branches = ()
    elif branch >= 100:
        executed_branches, missing_branches = ((2, 3), (2, 4)), ()
    elif branch >= 75:
        executed_branches, missing_branches = ((2, 3),), ((4, 5),)
    else:
        executed_branches, missing_branches = ((2, 3),), ((2, 4), (4, 5))
    return CoverageResult(
        function.file_path,
        function.function_name,
        tuple(sorted(set(executed) | set(missing))),
        executed,
        missing,
        executed_branches,
        missing_branches,
        percent,
        branch,
        None,
        0.2,
    )


def make_phase2_session(tmp_path: Path) -> RepairSessionResult:
    test_file = tmp_path / "phase2" / "generated_test.py"
    test_file.parent.mkdir()
    code = (
        "from branching import classify\n\n"
        "def test_positive():\n    assert classify(1) == 'positive'\n"
    )
    test_file.write_text(code, encoding="utf-8")
    result = RunResult(test_file, 0, RunStatus.PASS, "1 passed", "", 0.1)
    attempt = Attempt(
        0,
        AttemptKind.INITIAL,
        code,
        test_file,
        "initial prompt",
        code,
        result,
        FailureAnalyzer().analyze(result),
        analyze_test_structure(code),
        (),
        0.2,
    )
    return RepairSessionResult(
        (attempt,),
        RunStatus.PASS,
        RunStatus.PASS,
        0,
        False,
        StopReason.PASS_REACHED,
        0.2,
        0.1,
    )


def candidate(name: str, value: int, expected: str) -> str:
    return (
        "from branching import classify\n\n"
        f"def test_{name}():\n    assert classify({value}) == {expected!r}\n"
    )


def execute(
    tmp_path: Path,
    percentages: Sequence[tuple[float, float | None]],
    responses: Sequence[str | Exception],
    statuses: Sequence[RunStatus],
    *,
    max_rounds: int = 3,
):
    function = make_function(tmp_path)
    provider = ScriptedProvider(responses)
    runner = ScriptedRunner(statuses)
    measurer = ScriptedCoverageRunner(
        [coverage(function, line, branch) for line, branch in percentages]
    )
    store = ArtifactStore(tmp_path / "runs")
    run = store.create_run()
    phase2 = make_phase2_session(tmp_path)
    session = CoverageEngine(
        provider,
        runner,
        measurer,
        store,
        max_coverage_rounds=max_rounds,
        max_repair_attempts=1,
    ).run(function, run, phase2)
    return session, provider, runner, measurer, store, run, phase2, function


def test_two_improvements_reach_target_and_preserve_cumulative_suite(tmp_path: Path) -> None:
    session, provider, runner, measurer, *_ = execute(
        tmp_path,
        [(50, 50), (75, 75), (100, 100)],
        [candidate("zero", 0, "zero"), candidate("negative", -1, "negative")],
        [RunStatus.PASS, RunStatus.PASS],
    )

    assert session.stop_reason is CoverageStopReason.TARGET_REACHED
    assert session.target_reached is True
    assert len(session.rounds) == 2
    assert all(item.accepted for item in session.rounds)
    assert [item.line_coverage_percent for item in session.history] == [50, 75, 100]
    assert len(provider.prompts) == 2
    assert [len(suite) for suite in runner.suites] == [2, 3]
    assert [len(suite) for suite in measurer.suites] == [1, 2, 3]
    assert measurer.suites[-1][0] == session.accepted_test_files[0]
    assert measurer.suites[-1][1] == session.accepted_test_files[1]


def test_baseline_target_short_circuits_without_llm_call(tmp_path: Path) -> None:
    session, provider, runner, measurer, *_ = execute(tmp_path, [(100, 100)], [], [], max_rounds=3)

    assert session.stop_reason is CoverageStopReason.TARGET_REACHED
    assert session.rounds == ()
    assert provider.prompts == []
    assert runner.suites == []
    assert len(measurer.suites) == 1


def test_zero_round_mode_measures_only_baseline(tmp_path: Path) -> None:
    session, provider, _, measurer, *_ = execute(tmp_path, [(50, 50)], [], [], max_rounds=0)

    assert session.stop_reason is CoverageStopReason.MAX_ROUNDS_REACHED
    assert session.target_reached is False
    assert provider.prompts == []
    assert len(measurer.suites) == 1


def test_no_improvement_rejects_candidate_and_stops(tmp_path: Path) -> None:
    session, _, _, _, *_ = execute(
        tmp_path,
        [(50, 50), (50, 50)],
        [candidate("duplicate", 1, "positive")],
        [RunStatus.PASS],
    )

    assert session.stop_reason is CoverageStopReason.NO_COVERAGE_IMPROVEMENT
    assert session.rounds[0].accepted is False
    assert len(session.accepted_test_files) == 1
    assert len(session.history) == 1


def test_candidate_can_repair_against_cumulative_suite(tmp_path: Path) -> None:
    repaired = candidate("zero", 0, "zero")
    session, provider, runner, *_ = execute(
        tmp_path,
        [(50, 50), (100, 100)],
        ["def test_broken(:\n", repaired],
        [RunStatus.ERROR, RunStatus.PASS],
    )

    assert session.target_reached is True
    assert session.rounds[0].accepted is True
    assert session.rounds[0].candidate_execution is not None
    assert session.rounds[0].candidate_execution.repair_count == 1
    assert len(provider.prompts) == 2
    assert all(len(suite) == 2 for suite in runner.suites)


def test_exhausted_candidate_is_rejected_without_polluting_suite(tmp_path: Path) -> None:
    session, _, _, measurer, *_ = execute(
        tmp_path,
        [(50, 50)],
        ["def test_broken(:\n", "def still_broken(:\n"],
        [RunStatus.ERROR, RunStatus.ERROR],
    )

    assert session.stop_reason is CoverageStopReason.CANDIDATE_NOT_ACCEPTED
    assert session.rounds[0].accepted is False
    assert len(session.accepted_test_files) == 1
    assert len(measurer.suites) == 1


def test_max_round_limit_never_generates_a_third_candidate(tmp_path: Path) -> None:
    session, provider, *_ = execute(
        tmp_path,
        [(40, 40), (60, 60), (80, 80)],
        [candidate("one", 0, "zero"), candidate("two", -1, "negative")],
        [RunStatus.PASS, RunStatus.PASS],
        max_rounds=2,
    )

    assert session.stop_reason is CoverageStopReason.MAX_ROUNDS_REACHED
    assert len(session.rounds) == 2
    assert len(provider.prompts) == 2


def test_coverage_regression_rejects_candidate(tmp_path: Path) -> None:
    session, *_ = execute(
        tmp_path,
        [(75, 75), (50, 50)],
        [candidate("regression", 0, "zero")],
        [RunStatus.PASS],
    )

    assert session.stop_reason is CoverageStopReason.COVERAGE_REGRESSION
    assert session.rounds[0].accepted is False
    assert session.final_coverage is session.initial_coverage
    assert len(session.accepted_test_files) == 1


def test_empty_or_non_test_candidate_is_rejected(tmp_path: Path) -> None:
    session, _, runner, measurer, *_ = execute(
        tmp_path, [(50, 50)], ["value = 1\n"], [], max_rounds=3
    )

    assert session.stop_reason is CoverageStopReason.CANDIDATE_NOT_ACCEPTED
    assert "NO_PYTEST_TEST_FUNCTIONS" in session.rounds[0].quality_warnings
    assert runner.suites == []
    assert len(measurer.suites) == 1


@pytest.mark.parametrize(
    ("keyword", "value"),
    [
        ("max_coverage_rounds", -1),
        ("coverage_target", -0.1),
        ("coverage_target", 100.1),
        ("max_repair_attempts", -1),
        ("max_accepted_test_chars", 0),
    ],
)
def test_invalid_engine_limits_are_rejected(tmp_path: Path, keyword: str, value: float) -> None:
    function = make_function(tmp_path)
    store = ArtifactStore(tmp_path / "runs")
    arguments = {keyword: value}

    with pytest.raises(ValueError):
        CoverageEngine(
            ScriptedProvider([]),
            ScriptedRunner([]),
            ScriptedCoverageRunner([coverage(function, 100)]),
            store,
            **arguments,  # type: ignore[arg-type]
        )


def test_artifacts_and_root_metadata_preserve_coverage_research_data(tmp_path: Path) -> None:
    session, _, _, _, store, run, phase2, function = execute(
        tmp_path,
        [(50, 50), (100, 100)],
        [candidate("zero", 0, "zero")],
        [RunStatus.PASS],
    )
    metadata = store.save_session_result(
        run,
        function,
        phase2,
        provider="fake",
        model="scripted",
        base_url="local",
        http_timeout_seconds=1,
        temperature=0,
        max_repair_attempts=1,
        execution_timeout_seconds=30,
        coverage_session=session,
        max_coverage_rounds=3,
        coverage_target=100,
    )

    baseline = run.run_dir / "coverage" / "baseline"
    round_dir = run.run_dir / "coverage" / "round-001"
    assert (baseline / "coverage_raw.json").is_file()
    assert (baseline / "coverage_result.json").is_file()
    for name in ("prompt.txt", "raw_response.txt", "generated_test.py", "result.json"):
        assert (round_dir / name).is_file()
    assert (round_dir / "candidate" / "attempt-000" / "result.json").is_file()
    assert metadata["coverage"]["metrics"]["line_coverage_gain"] == 50
    assert metadata["coverage"]["metrics"]["coverage_round_count"] == 1
    assert metadata["coverage"]["metrics"]["total_generation_count"] == 2
    assert metadata["coverage"]["stop_reason"] == "TARGET_REACHED"

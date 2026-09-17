import hashlib
import json
from pathlib import Path

import pytest

from autotest.artifact_store import ArtifactStore
from autotest.errors import ArtifactError
from autotest.failure_analyzer import FailureAnalyzer
from autotest.mutation_runner import MutationResult, MutationStatus
from autotest.project_analyzer import FunctionInfo
from autotest.repair_engine import (
    AttemptKind,
    RepairSessionResult,
    StopReason,
)
from autotest.repair_engine import TestAttempt as Attempt
from autotest.test_generator import GeneratedTest
from autotest.test_quality import analyze_test_structure
from autotest.test_runner import TestRunResult as RunResult
from autotest.test_runner import TestStatus as RunStatus

RUN_ID = "20260915T071530Z-a3f91c"
TIMESTAMP = "2026-09-15T07:15:30Z"


def test_preserves_utf8_artifacts_metadata_and_hashes(tmp_path: Path) -> None:
    source = tmp_path / "project with spaces" / "tính_toán.py"
    source.parent.mkdir()
    source.write_text('def greet():\n    """Xin chào."""\n    return "café"\n', encoding="utf-8")
    function = FunctionInfo(
        file_path=source.resolve(),
        module_name="tính_toán",
        function_name="greet",
        source_code='def greet():\n    """Xin chào."""\n    return "café"',
        start_line=1,
        end_line=3,
        docstring="Xin chào.",
    )
    original_test = tmp_path / "original.py"
    original_test.write_text("def test_greet():\n    assert 'café'\n", encoding="utf-8")
    generated = GeneratedTest(
        target_function="greet",
        test_file=original_test,
        prompt="Hãy tạo pytest cho café.\n",
        raw_response="Phản hồi thô: café\n",
        test_code="def test_greet():\n    assert 'café'\n",
    )
    store = ArtifactStore(tmp_path / "runs")
    run = store.create_run(run_id=RUN_ID, timestamp_utc=TIMESTAMP)

    stored = store.save_generation(run, generated)
    execution = RunResult(stored.test_file, 0, RunStatus.PASS, "1 passed", "", 0.125)
    metadata = store.save_result(
        run,
        function,
        stored,
        execution,
        provider="ollama",
        model="qwen2.5-coder:14b",
        base_url="http://localhost:11434",
        http_timeout_seconds=120.0,
        temperature=0.0,
        execution_timeout_seconds=30.0,
    )

    assert run.run_dir.is_dir()
    assert run.prompt_file.read_text(encoding="utf-8") == generated.prompt
    assert run.raw_response_file.read_text(encoding="utf-8") == generated.raw_response
    assert run.generated_test_file.read_text(encoding="utf-8") == generated.test_code
    assert stored.test_file == run.generated_test_file
    parsed = json.loads(run.result_file.read_text(encoding="utf-8"))
    assert parsed == metadata
    assert parsed["run_id"] == RUN_ID
    assert parsed["timestamp_utc"] == TIMESTAMP
    assert parsed["execution"]["status"] == "PASS"
    assert parsed["execution"]["exit_code"] == 0
    assert parsed["llm"]["temperature"] == 0.0
    assert parsed["generation"] == {
        "prompt_file": "prompt.txt",
        "raw_response_file": "raw_response.txt",
        "generated_test_file": "generated_test.py",
    }
    assert parsed["hashes"] == {
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "prompt_sha256": hashlib.sha256(run.prompt_file.read_bytes()).hexdigest(),
        "generated_test_sha256": hashlib.sha256(run.generated_test_file.read_bytes()).hexdigest(),
    }
    run.result_file.read_bytes().decode("utf-8")


def test_runs_are_unique_and_existing_run_is_not_overwritten(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "runs")
    first = store.create_run()
    second = store.create_run()

    assert first.run_id != second.run_id
    assert first.run_dir != second.run_dir

    store.create_run(run_id=RUN_ID, timestamp_utc=TIMESTAMP)
    with pytest.raises(ArtifactError, match="Refusing to overwrite"):
        store.create_run(run_id=RUN_ID, timestamp_utc=TIMESTAMP)


def test_generation_artifacts_cannot_be_saved_twice(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "runs")
    run = store.create_run(run_id=RUN_ID, timestamp_utc=TIMESTAMP)
    original = tmp_path / "test.py"
    original.write_text("assert True\n", encoding="utf-8")
    generated = GeneratedTest("target", original, "prompt", "raw", "assert True\n")
    store.save_generation(run, generated)

    with pytest.raises(ArtifactError, match="Refusing to overwrite"):
        store.save_generation(run, generated)


def test_phase2_attempt_and_session_metadata_are_immutable(tmp_path: Path) -> None:
    source = tmp_path / "calculator.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    function = FunctionInfo(
        source.resolve(),
        "calculator",
        "add",
        "def add(a, b):\n    return a + b",
        1,
        2,
        None,
    )
    store = ArtifactStore(tmp_path / "runs")
    run = store.create_run(run_id=RUN_ID, timestamp_utc=TIMESTAMP)
    attempt_files = store.create_attempt(run, 0)
    prompt = "Tạo kiểm thử cho hàm cộng.\n"
    raw_response = "def test_add():\n    assert add(2, 3) == 5\n"
    store.save_attempt_prompt(attempt_files, prompt)
    store.save_attempt_raw_response(attempt_files, raw_response)
    test_file = store.save_attempt_test(attempt_files, raw_response)
    run_result = RunResult(test_file, 0, RunStatus.PASS, "1 passed\n", "", 0.1)
    attempt = Attempt(
        0,
        AttemptKind.INITIAL,
        raw_response,
        test_file,
        prompt,
        raw_response,
        run_result,
        FailureAnalyzer().analyze(run_result),
        analyze_test_structure(raw_response),
        (),
        0.2,
    )
    attempt_metadata = store.save_attempt_result(
        attempt_files, attempt, execution_timeout_seconds=30.0
    )
    session = RepairSessionResult(
        attempts=(attempt,),
        initial_status=RunStatus.PASS,
        final_status=RunStatus.PASS,
        repair_count=0,
        repaired_successfully=False,
        stopped_reason=StopReason.PASS_REACHED,
        total_generation_seconds=0.2,
        total_execution_seconds=0.1,
    )
    root_metadata = store.save_session_result(
        run,
        function,
        session,
        provider="ollama",
        model="qwen2.5-coder:14b",
        base_url="http://localhost:11434",
        http_timeout_seconds=120.0,
        temperature=0.0,
        max_repair_attempts=3,
        execution_timeout_seconds=30.0,
    )

    assert json.loads(attempt_files.result_file.read_text(encoding="utf-8")) == attempt_metadata
    assert attempt_metadata["generation"]["test_function_count"] == 1
    assert attempt_metadata["generation"]["assert_count"] == 1
    assert (
        attempt_metadata["hashes"]["prompt_sha256"]
        == hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    )
    assert root_metadata["execution"]["initial_status"] == "PASS"
    assert root_metadata["execution"]["final_status"] == "PASS"
    assert root_metadata["execution"]["status"] == "PASS"
    assert root_metadata["execution"]["exit_code"] == 0
    assert (
        root_metadata["hashes"]["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert root_metadata["repair"]["repaired_successfully"] is False
    assert root_metadata["repair"]["repair_success"] is False
    assert root_metadata["attempts"] == [
        {"index": 0, "kind": "INITIAL", "status": "PASS", "directory": "attempt-000"}
    ]
    assert not any(path.name in {".pytest_cache", "__pycache__"} for path in run.run_dir.rglob("*"))
    with pytest.raises(ArtifactError, match="Refusing to overwrite"):
        store.save_attempt_result(attempt_files, attempt, execution_timeout_seconds=30.0)


def test_mutation_artifacts_are_immutable_and_normalized(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "runs")
    run = store.create_run(run_id=RUN_ID, timestamp_utc=TIMESTAMP)
    artifacts = store.create_mutation(run)
    raw = artifacts.raw_stats_file
    raw.write_text('{"killed": 2}\n', encoding="utf-8")
    result = MutationResult(
        target_file=tmp_path / "source.py",
        function_name="classify",
        status=MutationStatus.COMPLETE,
        total_mutants=3,
        killed_mutants=2,
        survived_mutants=1,
        tool_total_mutants=3,
        mutation_score_percent=200 / 3,
        duration_seconds=1.25,
        backend_version="3.7.0",
        python_version="Python 3.10.12",
        pytest_version="pytest 8.4.2",
        mutation_venv="/home/ubuntu/autotest-mutation-env",
        wsl_distribution="Ubuntu",
        raw_stats_file=raw,
        workspace=artifacts.workspace,
        hashes={"target_source_sha256": "abc"},
        stdout="tool output\n",
        stderr="",
    )

    metadata = store.save_mutation_result(artifacts, result)

    assert json.loads(artifacts.result_file.read_text(encoding="utf-8")) == metadata
    assert metadata["status"] == "COMPLETE"
    assert metadata["mutation_score_percent"] == pytest.approx(66.6666667)
    assert metadata["wsl_distribution"] == "Ubuntu"
    assert metadata["mutation_venv"] == "/home/ubuntu/autotest-mutation-env"
    assert metadata["backend_version"] == "3.7.0"
    assert metadata["python_version"] == "Python 3.10.12"
    assert artifacts.stdout_file.read_text(encoding="utf-8") == "tool output\n"
    assert artifacts.stderr_file.read_text(encoding="utf-8") == ""
    with pytest.raises(ArtifactError, match="Refusing to overwrite"):
        store.save_mutation_result(artifacts, result)
    with pytest.raises(ArtifactError, match="Refusing to overwrite"):
        store.create_mutation(run)

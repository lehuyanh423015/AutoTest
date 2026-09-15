import hashlib
from pathlib import Path

import pytest

from autotest.artifact_store import ArtifactStore
from autotest.errors import MutationEnvironmentUnavailableError
from autotest.mutation_runner import MutationStatus, WSLMutmutBackend
from autotest.project_analyzer import ProjectAnalyzer


@pytest.mark.mutation
def test_live_wsl_mutmut_evaluation(tmp_path: Path) -> None:
    backend = WSLMutmutBackend(timeout=300.0)
    try:
        backend.check_environment()
    except MutationEnvironmentUnavailableError as exc:
        pytest.skip(f"live WSL/Mutmut environment unavailable: {exc}")

    fixture_dir = Path(__file__).parent / "fixtures" / "mutation_suite"
    function = ProjectAnalyzer().analyze_function(fixture_dir / "classifier.py", "classify_number")
    accepted_test = fixture_dir / "accepted_suite.py"
    source_hash = hashlib.sha256(function.file_path.read_bytes()).hexdigest()
    test_hash = hashlib.sha256(accepted_test.read_bytes()).hexdigest()
    store = ArtifactStore(tmp_path / "runs")
    run = store.create_run()
    artifacts = store.create_mutation(run)

    result = backend.run(function, [accepted_test], artifacts.mutation_dir)
    store.save_mutation_result(artifacts, result)

    assert result.status in (MutationStatus.COMPLETE, MutationStatus.NO_MUTANTS)
    assert result.backend_version == "3.7.0"
    assert result.status is MutationStatus.COMPLETE
    assert result.total_mutants > 0
    assert result.raw_stats_file == artifacts.raw_stats_file
    assert artifacts.raw_stats_file.is_file()
    assert artifacts.result_file.is_file()
    assert result.mutation_score_percent is not None
    assert hashlib.sha256(function.file_path.read_bytes()).hexdigest() == source_hash
    assert hashlib.sha256(accepted_test.read_bytes()).hexdigest() == test_hash
    assert result.hashes["target_source_sha256"] == source_hash
    assert result.hashes["accepted_test_sha256"][str(accepted_test.resolve())] == test_hash
    assert (artifacts.workspace / "classifier.py").is_file()
    assert sorted(path.name for path in (artifacts.workspace / "tests").glob("*.py")) == [
        "test_accepted_000.py"
    ]

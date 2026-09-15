import os
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from autotest.test_runner import TestRunner as Runner
from autotest.test_runner import TestStatus as RunStatus


def write_test(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    return path


def test_pass_classification_and_local_import_path(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("def value():\n    return 42\n", encoding="utf-8")
    tests_dir = tmp_path / "outside" / "tests"
    tests_dir.mkdir(parents=True)
    test_file = write_test(
        tests_dir / "test_pass.py",
        "from sample import value\n\ndef test_value():\n    assert value() == 42\n",
    )

    result = Runner(timeout=10).run(test_file, project_root=tmp_path)

    assert result.status is RunStatus.PASS
    assert result.passed is True
    assert result.exit_code == 0
    assert "1 passed" in result.stdout


def test_fail_classification_and_captured_output(tmp_path: Path) -> None:
    test_file = write_test(
        tmp_path / "test_fail.py",
        "def test_failure():\n    print('captured marker')\n    assert False\n",
    )

    result = Runner(timeout=10).run(test_file, project_root=tmp_path)

    assert result.status is RunStatus.FAIL
    assert result.passed is False
    assert result.exit_code == 1
    assert "captured marker" in result.stdout


def test_collection_error_classification(tmp_path: Path) -> None:
    test_file = write_test(tmp_path / "test_error.py", "this is invalid python\n")

    result = Runner(timeout=10).run(test_file, project_root=tmp_path)

    assert result.status is RunStatus.ERROR
    assert result.exit_code not in (0, 1)
    assert "SyntaxError" in result.stdout


def test_timeout_classification(tmp_path: Path) -> None:
    test_file = write_test(
        tmp_path / "test_timeout.py",
        "import time\n\ndef test_slow():\n    time.sleep(5)\n",
    )

    result = Runner(timeout=0.25).run(test_file, project_root=tmp_path)

    assert result.status is RunStatus.TIMEOUT
    assert result.exit_code is None
    assert result.duration_seconds < 3


def test_uses_generated_directory_as_controlled_pytest_root(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    generated_dir = tmp_path / "generated"
    generated_dir.mkdir()
    test_file = write_test(generated_dir / "test_sample.py", "def test_ok():\n    pass\n")

    with patch(
        "autotest.test_runner.subprocess.run",
        return_value=CompletedProcess([], 0, "passed", ""),
    ) as run:
        result = Runner().run(test_file, project_root=project_root)

    command = run.call_args.args[0]
    assert command[3:5] == ["--rootdir", str(generated_dir.resolve())]
    assert command[-2:] == ["-p", "no:cacheprovider"]
    assert run.call_args.kwargs["cwd"] == generated_dir.resolve()
    assert run.call_args.kwargs["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert run.call_args.kwargs["env"]["PYTHONPATH"].split(os.pathsep)[0] == str(
        project_root.resolve()
    )
    assert result.status is RunStatus.PASS


def test_space_paths_and_stderr_are_supported(tmp_path: Path) -> None:
    project_root = tmp_path / "project with spaces"
    project_root.mkdir()
    generated_dir = tmp_path / "generated tests"
    generated_dir.mkdir()
    test_file = write_test(
        generated_dir / "test output.py",
        "import atexit\n"
        "import sys\n\n"
        "atexit.register(lambda: print('stderr marker', file=sys.stderr))\n\n"
        "def test_output():\n"
        "    pass\n",
    )

    result = Runner(timeout=10).run(test_file, project_root=project_root)

    assert result.status is RunStatus.PASS
    assert "stderr marker" in result.stderr


def test_existing_pythonpath_is_preserved_without_global_mutation(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    generated_dir = tmp_path / "generated"
    generated_dir.mkdir()
    test_file = write_test(generated_dir / "test_sample.py", "def test_ok():\n    pass\n")
    existing = str(tmp_path / "existing imports")

    with (
        patch.dict(os.environ, {"PYTHONPATH": existing}),
        patch(
            "autotest.test_runner.subprocess.run",
            return_value=CompletedProcess([], 0, "passed", ""),
        ) as run,
    ):
        Runner().run(test_file, project_root=project_root)
        child_pythonpath = run.call_args.kwargs["env"]["PYTHONPATH"]
        assert os.environ["PYTHONPATH"] == existing

    assert child_pythonpath == os.pathsep.join((str(project_root.resolve()), existing))

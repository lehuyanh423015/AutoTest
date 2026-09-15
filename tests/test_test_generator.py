from pathlib import Path

import pytest

from autotest.errors import GenerationError
from autotest.llm.base import LLMProvider
from autotest.project_analyzer import FunctionInfo
from autotest.test_generator import TestGenerator as Generator
from autotest.test_generator import sanitize_test_code
from autotest.test_runner import TestRunner as Runner
from autotest.test_runner import TestStatus as RunStatus


class FakeProvider(LLMProvider):
    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


def make_info(module_name: str = "odd module-name") -> FunctionInfo:
    return FunctionInfo(
        file_path=Path("source.py").resolve(),
        module_name=module_name,
        function_name="do_work",
        source_code="def do_work():\n    return 1",
        start_line=1,
        end_line=2,
        docstring=None,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  assert True  ", "assert True\n"),
        ("```python\nassert True\n```", "assert True\n"),
        ("```py\nassert True\n```", "assert True\n"),
        ("```\nassert True\n```", "assert True\n"),
    ],
)
def test_sanitize_test_code(raw: str, expected: str) -> None:
    assert sanitize_test_code(raw) == expected


def test_generates_clean_file_and_preserves_raw_response(tmp_path: Path) -> None:
    raw = (
        "```python\nfrom odd_module_name import do_work\n\n"
        "def test_it():\n    assert do_work() == 1\n```"
    )
    provider = FakeProvider(raw)

    generated = Generator(provider, tmp_path).generate(make_info())

    assert generated.test_file == (tmp_path / "test_odd_module_name_do_work.py").resolve()
    assert generated.test_file.read_text(encoding="utf-8") == generated.test_code
    assert generated.raw_response == raw
    assert "Target function: do_work" in generated.prompt
    assert provider.prompts == [generated.prompt]
    assert not generated.test_code.startswith("```")


def test_empty_generation_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(GenerationError, match="no test code"):
        Generator(FakeProvider("  "), tmp_path).generate(make_info())


def test_supports_explicit_artifact_filename(tmp_path: Path) -> None:
    generated = Generator(
        FakeProvider("assert True"), tmp_path, test_filename="generated_test.py"
    ).generate(make_info())

    assert generated.test_file == (tmp_path / "generated_test.py").resolve()


@pytest.mark.parametrize("filename", ["not-python.txt", "../outside.py"])
def test_rejects_unsafe_artifact_filename(tmp_path: Path, filename: str) -> None:
    with pytest.raises(GenerationError, match="simple .py filename"):
        Generator(FakeProvider("assert True"), tmp_path, test_filename=filename).generate(
            make_info()
        )


def test_invalid_python_is_not_repaired_and_executes_as_error(tmp_path: Path) -> None:
    raw = "```python\ndef broken(:\n```"

    generated = Generator(FakeProvider(raw), tmp_path).generate(make_info())
    result = Runner(timeout=10).run(generated.test_file, project_root=tmp_path)

    assert generated.test_code == "def broken(:\n"
    assert result.status is RunStatus.ERROR
    assert "SyntaxError" in result.stdout

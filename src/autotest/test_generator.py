"""Generate and persist pytest source for an analyzed function."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from autotest.errors import GenerationError, LLMError
from autotest.llm.base import LLMProvider
from autotest.project_analyzer import FunctionInfo
from autotest.prompt_builder import PromptBuilder


@dataclass(frozen=True, slots=True)
class GeneratedTest:
    """The complete artifact produced by one generation request."""

    target_function: str
    test_file: Path
    prompt: str
    raw_response: str
    test_code: str
    generation_duration_seconds: float = 0.0


def sanitize_test_code(raw_response: str) -> str:
    """Strip whitespace and at most one surrounding Markdown code fence."""
    cleaned = raw_response.strip()
    fenced = re.fullmatch(
        r"```(?:python|py)?[ \t]*\r?\n(?P<code>.*)\r?\n```",
        cleaned,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        cleaned = fenced.group("code").strip()
    return f"{cleaned}\n" if cleaned else ""


class TestGenerator:
    """Coordinate prompt construction, generation, cleaning, and saving."""

    def __init__(
        self,
        provider: LLMProvider,
        output_dir: Path | str = Path("workspace/generated_tests"),
        prompt_builder: PromptBuilder | None = None,
        test_filename: str | None = None,
    ) -> None:
        self.provider = provider
        self.output_dir = Path(output_dir)
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.test_filename = test_filename

    def generate(self, function: FunctionInfo) -> GeneratedTest:
        prompt = self.prompt_builder.build(function)
        started = time.perf_counter()
        try:
            raw_response = self.provider.generate(prompt)
        except LLMError:
            raise
        except Exception as exc:
            raise GenerationError(f"LLM provider failed unexpectedly: {exc}") from exc
        generation_duration = time.perf_counter() - started

        test_code = sanitize_test_code(raw_response)
        if not test_code:
            raise GenerationError("The LLM provider returned no test code.")

        filename = self.test_filename or self._filename(
            function.module_name, function.function_name
        )
        if Path(filename).name != filename or Path(filename).suffix.lower() != ".py":
            raise GenerationError("Generated test filename must be a simple .py filename.")
        test_file = (self.output_dir / filename).resolve()
        if test_file == function.file_path.resolve():
            raise GenerationError("Refusing to overwrite the target source file.")

        try:
            test_file.parent.mkdir(parents=True, exist_ok=True)
            test_file.write_text(test_code, encoding="utf-8", newline="\n")
        except OSError as exc:
            raise GenerationError(f"Could not save generated test to {test_file}: {exc}") from exc

        return GeneratedTest(
            target_function=function.function_name,
            test_file=test_file,
            prompt=prompt,
            raw_response=raw_response,
            test_code=test_code,
            generation_duration_seconds=generation_duration,
        )

    @staticmethod
    def _filename(module_name: str, function_name: str) -> str:
        def safe_part(value: str) -> str:
            part = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").lower()
            return part or "target"

        return f"test_{safe_part(module_name)}_{safe_part(function_name)}.py"

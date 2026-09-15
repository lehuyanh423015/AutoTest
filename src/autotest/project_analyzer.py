"""Static analysis for standalone Python source files."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from autotest.errors import AnalyzerError, FunctionNotFoundError


@dataclass(frozen=True, slots=True)
class FunctionInfo:
    """Source and location metadata for one top-level function."""

    file_path: Path
    module_name: str
    function_name: str
    source_code: str
    start_line: int
    end_line: int
    docstring: str | None


class ProjectAnalyzer:
    """Discover functions without importing or executing target code."""

    def discover_functions(self, file_path: Path | str) -> tuple[str, ...]:
        """Return top-level function names in source order."""
        _, tree = self._read_and_parse(Path(file_path))
        return tuple(
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )

    def analyze_function(self, file_path: Path | str, function_name: str) -> FunctionInfo:
        """Extract a named top-level function and its basic metadata."""
        path = Path(file_path)
        source, tree = self._read_and_parse(path)

        node = next(
            (
                item
                for item in tree.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name == function_name
            ),
            None,
        )
        if node is None:
            raise FunctionNotFoundError(
                f"Top-level function {function_name!r} was not found in {path}."
            )

        if node.end_lineno is None:  # pragma: no cover - guaranteed by supported CPython
            raise AnalyzerError(f"Could not determine the end of function {function_name!r}.")

        start_line = min(
            (decorator.lineno for decorator in node.decorator_list),
            default=node.lineno,
        )
        source_lines = source.splitlines(keepends=True)
        function_source = "".join(source_lines[start_line - 1 : node.end_lineno])
        function_source = function_source.rstrip("\r\n")

        return FunctionInfo(
            file_path=path.resolve(),
            module_name=path.stem,
            function_name=node.name,
            source_code=function_source,
            start_line=start_line,
            end_line=node.end_lineno,
            docstring=ast.get_docstring(node),
        )

    @staticmethod
    def _read_and_parse(path: Path) -> tuple[str, ast.Module]:
        if not path.exists():
            raise AnalyzerError(f"Python source file does not exist: {path}")
        if not path.is_file():
            raise AnalyzerError(f"Target path is not a file: {path}")
        if path.suffix.lower() != ".py":
            raise AnalyzerError(f"Target file must have a .py extension: {path}")

        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise AnalyzerError(f"Could not read Python source file {path}: {exc}") from exc

        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            location = f"line {exc.lineno}" if exc.lineno is not None else "unknown line"
            raise AnalyzerError(f"Invalid Python syntax in {path} ({location}): {exc.msg}") from exc

        return source, tree

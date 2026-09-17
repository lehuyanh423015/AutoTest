"""Conservative repository-layout adapter for the frozen WSL Mutmut backend."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Sequence

from autotest.errors import MutationError
from autotest.mutation_runner import WSLMutmutBackend
from autotest.project_analyzer import FunctionInfo
from autotest.project_inspector import ProjectProfile


class RepositoryWSLMutmutBackend(WSLMutmutBackend):
    """Copy profiled production modules, never target tests or installed packages."""

    def __init__(self, source_root: Path, profile: ProjectProfile, **kwargs) -> None:
        super().__init__(**kwargs)
        self.source_root = source_root.resolve()
        self.profile = profile

    def _prepare_workspace(
        self, function: FunctionInfo, test_files: Sequence[Path | str], workspace: Path
    ) -> tuple[Path, tuple[Path, ...], Path]:
        if not test_files:
            raise MutationError("Mutation evaluation requires accepted generated tests.")
        try:
            relative_target = function.file_path.relative_to(self.source_root)
        except ValueError as exc:
            raise MutationError("Mutation target is outside the prepared source copy.") from exc
        paths = {
            item.path.relative_to(self.profile.root)
            for item in self.profile.modules
            if not item.is_test_module
        }
        if relative_target not in paths:
            raise MutationError("Mutation target is absent from the production profile.")
        tests_dir = workspace / "tests"
        try:
            workspace.mkdir(parents=True, exist_ok=False)
            tests_dir.mkdir()
            for relative in sorted(paths):
                source = self.source_root / relative
                destination = workspace / relative
                if source.is_symlink() or not source.is_file():
                    raise MutationError(f"Unsupported mutation source: {relative.as_posix()}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            copied_tests = []
            for index, item in enumerate(test_files):
                destination = tests_dir / f"test_accepted_{index:03d}.py"
                shutil.copyfile(Path(item).resolve(), destination)
                copied_tests.append(destination)
            source_roots = [
                path.relative_to(self.profile.root).as_posix() for path in self.profile.source_roots
            ]
            support_paths = [path.as_posix() for path in sorted(paths) if path != relative_target]
            config = workspace / "pyproject.toml"
            config.write_text(
                "[tool.mutmut]\n"
                f'source_paths = ["{relative_target.as_posix()}"]\n'
                f"also_copy = {support_paths!r}\n"
                'pytest_add_cli_args_test_selection = ["tests"]\n'
                "use_git_change_detection = false\n\n"
                "[tool.pytest.ini_options]\n"
                f"pythonpath = {source_roots!r}\n"
                'addopts = ["--import-mode=importlib", "-p", "no:cacheprovider"]\n',
                encoding="utf-8",
                newline="",
            )
            config_dir = workspace.parent / "config"
            config_dir.mkdir(exist_ok=True)
            with (config_dir / "pyproject.toml").open("xb") as file:
                file.write(config.read_bytes())
        except OSError as exc:
            raise MutationError(f"Could not prepare repository mutation workspace: {exc}") from exc
        return workspace / relative_target, tuple(copied_tests), config

"""Provider-neutral execution settings for a prepared repository copy."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RepositoryExecutionContext:
    python_executable: Path
    working_directory: Path
    source_roots: tuple[Path, ...]
    pytest_config: Path

    def environment(self) -> dict[str, str]:
        """Pass launch essentials, but neither host PYTHONPATH nor credentials."""
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR"}
        result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        result["PYTHONPATH"] = os.pathsep.join(str(path) for path in self.source_roots)
        result["PYTHONNOUSERSITE"] = "1"
        result["PYTHONDONTWRITEBYTECODE"] = "1"
        result["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        return result

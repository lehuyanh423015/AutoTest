"""Bounded, deterministic, static discovery of a local Python project."""

from __future__ import annotations

import ast
import configparser
import hashlib
import json
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autotest.errors import ProjectInspectionError

INSPECTION_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "build",
        "dist",
        "site-packages",
        "node_modules",
        "workspace",
        "docs",
        "examples",
    }
)
_EXCLUDED_DIRS = INSPECTION_EXCLUDED_DIRS
_LOCK_NAMES = ("uv.lock", "poetry.lock", "Pipfile.lock")
_METADATA_NAMES = ("pyproject.toml", "setup.cfg", "setup.py", "pytest.ini", "tox.ini")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")


@dataclass(frozen=True, slots=True)
class DependencyDeclaration:
    value: str
    source_file: Path
    group: str


@dataclass(frozen=True, slots=True)
class PythonRequirementDeclaration:
    value: str
    source_file: Path


@dataclass(frozen=True, slots=True)
class FunctionSummary:
    name: str
    start_line: int
    end_line: int
    is_async: bool


@dataclass(frozen=True, slots=True)
class PythonModuleInfo:
    path: Path
    module_name: str | None
    source_root: Path
    is_test_module: bool
    top_level_functions: tuple[FunctionSummary, ...]


@dataclass(frozen=True, slots=True)
class ProjectProfile:
    root: Path
    project_name: str
    is_python_project: bool
    python_requirement: str | None
    python_requirements: tuple[PythonRequirementDeclaration, ...]
    metadata_files: tuple[Path, ...]
    dependency_files: tuple[Path, ...]
    lock_files: tuple[Path, ...]
    build_backend: str | None
    package_manager: str | None
    package_manager_evidence: tuple[str, ...]
    test_framework: str | None
    test_framework_evidence: tuple[str, ...]
    source_roots: tuple[Path, ...]
    source_root_evidence: tuple[str, ...]
    test_roots: tuple[Path, ...]
    test_root_evidence: tuple[str, ...]
    dependencies: tuple[DependencyDeclaration, ...]
    modules: tuple[PythonModuleInfo, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a portable, stable JSON-compatible profile."""

        def relative(path: Path) -> str:
            return path.relative_to(self.root).as_posix() or "."

        payload = {
            "schema_version": 1,
            "project_name": self.project_name,
            "root": ".",
            "is_python_project": self.is_python_project,
            "python_requirement": self.python_requirement,
            "python_requirements": [
                {"value": item.value, "source_file": relative(item.source_file)}
                for item in self.python_requirements
            ],
            "metadata_files": [relative(path) for path in self.metadata_files],
            "dependency_files": [relative(path) for path in self.dependency_files],
            "lock_files": [relative(path) for path in self.lock_files],
            "build_backend": self.build_backend,
            "package_manager": self.package_manager,
            "package_manager_evidence": list(self.package_manager_evidence),
            "test_framework": self.test_framework,
            "test_framework_evidence": list(self.test_framework_evidence),
            "source_roots": [relative(path) for path in self.source_roots],
            "source_root_evidence": list(self.source_root_evidence),
            "test_roots": [relative(path) for path in self.test_roots],
            "test_root_evidence": list(self.test_root_evidence),
            "dependencies": [
                {
                    "value": item.value,
                    "source_file": relative(item.source_file),
                    "group": item.group,
                }
                for item in self.dependencies
            ],
            "modules": [
                {
                    "path": relative(item.path),
                    "module_name": item.module_name,
                    "source_root": relative(item.source_root),
                    "is_test_module": item.is_test_module,
                    "top_level_functions": [
                        {
                            "name": function.name,
                            "start_line": function.start_line,
                            "end_line": function.end_line,
                            "is_async": function.is_async,
                        }
                        for function in item.top_level_functions
                    ],
                }
                for item in self.modules
            ],
            "warnings": list(self.warnings),
        }
        normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload["profile_sha256"] = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"

    @property
    def sha256(self) -> str:
        """Hash normalized profile content, independent of absolute checkout location."""
        return self.to_dict()["profile_sha256"]


class ProjectInspector:
    """Inspect metadata and declarations without importing or executing target code."""

    def __init__(
        self, *, max_source_file_bytes: int = 2 * 1024 * 1024, max_python_files: int = 10_000
    ) -> None:
        if max_source_file_bytes < 1 or max_python_files < 1:
            raise ValueError("Inspection limits must be positive.")
        self.max_source_file_bytes = max_source_file_bytes
        self.max_python_files = max_python_files

    def inspect(self, project_root: Path | str) -> ProjectProfile:
        try:
            root = Path(project_root).resolve(strict=True)
            mode = root.stat().st_mode
        except FileNotFoundError as exc:
            raise ProjectInspectionError(
                f"Project directory does not exist: {project_root}"
            ) from exc
        except PermissionError as exc:
            raise ProjectInspectionError(
                f"Permission denied inspecting project: {project_root}"
            ) from exc
        except OSError as exc:
            raise ProjectInspectionError(
                f"Cannot access project directory {project_root}: {exc}"
            ) from exc
        if not stat.S_ISDIR(mode):
            raise ProjectInspectionError(f"Project path is not a directory: {root}")

        warnings: set[str] = set()
        dependencies: list[DependencyDeclaration] = []
        requirements: list[PythonRequirementDeclaration] = []
        project_names: list[tuple[str, Path]] = []
        explicit_sources: list[tuple[Path, str]] = []
        configured_tests: list[tuple[Path, str]] = []
        pytest_evidence: set[str] = set()
        try:
            entries = {path.name: path for path in root.iterdir() if not path.is_symlink()}
            skipped_links = sorted(path.name for path in root.iterdir() if path.is_symlink())
        except OSError as exc:
            raise ProjectInspectionError(f"Cannot list project directory {root}: {exc}") from exc
        warnings.update(f"Skipped symlink: {name}" for name in skipped_links)
        metadata = tuple(
            sorted(
                (
                    entries[name]
                    for name in _METADATA_NAMES
                    if name in entries and entries[name].is_file()
                ),
                key=lambda p: p.name,
            )
        )
        locks = tuple(
            sorted(
                (
                    entries[name]
                    for name in _LOCK_NAMES
                    if name in entries and entries[name].is_file()
                ),
                key=lambda p: p.name,
            )
        )
        dependency_files = self._dependency_files(root, entries, warnings)
        python_project = bool(metadata or locks or dependency_files or "Pipfile" in entries)
        build_backend: str | None = None
        if "pyproject.toml" in entries and entries["pyproject.toml"].is_file():
            build_backend = self._pyproject(
                entries["pyproject.toml"],
                root,
                dependencies,
                requirements,
                project_names,
                explicit_sources,
                configured_tests,
                pytest_evidence,
                warnings,
            )
        if "setup.cfg" in entries and entries["setup.cfg"].is_file():
            self._setup_cfg(
                entries["setup.cfg"],
                root,
                dependencies,
                requirements,
                project_names,
                explicit_sources,
                warnings,
            )
        if "setup.py" in entries and entries["setup.py"].is_file():
            self._setup_py(entries["setup.py"], dependencies, requirements, project_names, warnings)
        for path in dependency_files:
            self._requirements(path, dependencies, warnings)
        for name in ("pytest.ini", "tox.ini"):
            if name in entries and entries[name].is_file():
                self._pytest_ini(entries[name], root, configured_tests, pytest_evidence, warnings)

        test_roots, test_evidence = self._test_roots(root, entries, configured_tests, warnings)
        source_roots, source_evidence = self._source_roots(
            root, entries, explicit_sources, test_roots, warnings
        )
        modules = self._modules(root, source_roots, test_roots, warnings)
        if modules:
            python_project = True
        if not python_project:
            warnings.add("No Python project evidence found.")
        for path in test_roots:
            if (path / "conftest.py").is_file():
                pytest_evidence.add(f"{self._relative(root, path / 'conftest.py')}: conftest.py")
        if (root / "conftest.py").is_file():
            pytest_evidence.add("conftest.py: pytest convention")
        if any(re.match(r"^pytest(?:$|[<>=~!\[; ])", item.value.lower()) for item in dependencies):
            pytest_evidence.add("dependency declaration: pytest")
        unittest_evidence: set[str] = set()
        for module in modules:
            if module.is_test_module:
                imports = self._test_framework_imports(module.path)
                if "pytest" in imports:
                    pytest_evidence.add(f"{self._relative(root, module.path)}: imports pytest")
                if "unittest" in imports:
                    unittest_evidence.add(f"{self._relative(root, module.path)}: imports unittest")
        if pytest_evidence and unittest_evidence:
            warnings.add("Multiple test frameworks detected: pytest, unittest")
        test_framework = (
            "pytest"
            if pytest_evidence and not unittest_evidence
            else "unittest"
            if unittest_evidence and not pytest_evidence
            else None
        )

        values = {item.value for item in requirements}
        python_requirement = next(iter(values)) if len(values) == 1 else None
        if len(values) > 1:
            warnings.add("Conflicting Python requirements: " + ", ".join(sorted(values)))
        declared_names = {name for name, _ in project_names}
        if len(declared_names) > 1:
            warnings.add("Conflicting project names: " + ", ".join(sorted(declared_names)))
        project_name = project_names[0][0] if project_names else root.name
        managers = self._manager_evidence(root, entries, dependency_files)
        manager_names = {name for name, _ in managers}
        if len(manager_names) > 1:
            warnings.add(
                "Multiple dependency workflows detected: " + ", ".join(sorted(manager_names))
            )
        package_manager = managers[0][0] if len(manager_names) == 1 else None
        if locks:
            warnings.update(
                f"{path.name} detected; lockfile solver semantics were not parsed."
                for path in locks
            )
        if "Pipfile" in entries:
            warnings.add("Pipfile detected; declarations were not parsed.")
        if not entries:
            warnings.add("Project directory is empty.")

        return ProjectProfile(
            root=root,
            project_name=project_name,
            is_python_project=python_project,
            python_requirement=python_requirement,
            python_requirements=tuple(
                sorted(requirements, key=lambda x: (self._relative(root, x.source_file), x.value))
            ),
            metadata_files=metadata,
            dependency_files=dependency_files,
            lock_files=locks,
            build_backend=build_backend,
            package_manager=package_manager,
            package_manager_evidence=tuple(
                sorted(f"{self._relative(root, path)}: {name}" for name, path in managers)
            ),
            test_framework=test_framework,
            test_framework_evidence=tuple(sorted(pytest_evidence | unittest_evidence)),
            source_roots=source_roots,
            source_root_evidence=source_evidence,
            test_roots=test_roots,
            test_root_evidence=test_evidence,
            dependencies=tuple(
                sorted(
                    dependencies,
                    key=lambda x: (self._relative(root, x.source_file), x.group, x.value),
                )
            ),
            modules=modules,
            warnings=tuple(sorted(warnings)),
        )

    @staticmethod
    def _relative(root: Path, path: Path) -> str:
        return path.relative_to(root).as_posix() or "."

    def _read(self, path: Path, warnings: set[str]) -> str | None:
        try:
            if path.stat().st_size > self.max_source_file_bytes:
                warnings.add(f"Skipped oversized file: {path.name}")
                return None
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            warnings.add(f"Could not read {path.name}: {exc.__class__.__name__}")
            return None

    def _dependency_files(
        self, root: Path, entries: dict[str, Path], warnings: set[str]
    ) -> tuple[Path, ...]:
        found = [
            path
            for name, path in entries.items()
            if path.is_file()
            and re.fullmatch(r"requirements(?:[-_][\w.-]+)?\.txt", name, re.IGNORECASE)
        ]
        directory = entries.get("requirements")
        if directory is not None and directory.is_dir():
            try:
                for path in directory.iterdir():
                    if path.is_symlink():
                        warnings.add(f"Skipped symlink: {self._relative(root, path)}")
                    elif path.is_file() and path.suffix.lower() == ".txt":
                        found.append(path)
            except OSError:
                warnings.add("Could not list requirements/ directory.")
        if "Pipfile" in entries and entries["Pipfile"].is_file():
            found.append(entries["Pipfile"])
        return tuple(sorted(found, key=lambda p: self._relative(root, p)))

    def _pyproject(
        self,
        path: Path,
        root: Path,
        deps: list[DependencyDeclaration],
        reqs: list[PythonRequirementDeclaration],
        names: list[tuple[str, Path]],
        sources: list[tuple[Path, str]],
        tests: list[tuple[Path, str]],
        pytest: set[str],
        warnings: set[str],
    ) -> str | None:
        raw = self._read(path, warnings)
        if raw is None:
            return None
        try:
            data = tomllib.loads(raw)
        except tomllib.TOMLDecodeError:
            warnings.add("pyproject.toml: malformed TOML")
            return None
        project = data.get("project", {})
        if isinstance(project, dict):
            if isinstance(project.get("name"), str):
                names.append((project["name"], path))
            if isinstance(project.get("requires-python"), str):
                reqs.append(PythonRequirementDeclaration(project["requires-python"], path))
            self._append_strings(project.get("dependencies"), path, "runtime", deps, warnings)
            optional = project.get("optional-dependencies", {})
            if isinstance(optional, dict):
                for group, values in optional.items():
                    self._append_strings(values, path, f"optional:{group}", deps, warnings)
        build = data.get("build-system", {})
        backend = None
        if isinstance(build, dict):
            self._append_strings(build.get("requires"), path, "build", deps, warnings)
            if isinstance(build.get("build-backend"), str):
                backend = build["build-backend"]
        groups = data.get("dependency-groups", {})
        if isinstance(groups, dict):
            for group, values in groups.items():
                label = (
                    "development"
                    if group == "dev"
                    else "test"
                    if group == "test"
                    else f"group:{group}"
                )
                self._append_strings(values, path, label, deps, warnings)
        tool = data.get("tool", {})
        if isinstance(tool, dict):
            pytest_cfg = tool.get("pytest", {})
            if isinstance(pytest_cfg, dict) and isinstance(pytest_cfg.get("ini_options"), dict):
                pytest.add("pyproject.toml: tool.pytest.ini_options")
                self._configured_testpaths(
                    pytest_cfg["ini_options"].get("testpaths"),
                    root,
                    "pyproject.toml",
                    tests,
                    warnings,
                )
            setuptools = tool.get("setuptools", {})
            if isinstance(setuptools, dict):
                package_dir = setuptools.get("package-dir", {})
                if isinstance(package_dir, dict):
                    for value in package_dir.values():
                        if isinstance(value, str):
                            self._explicit_root(
                                root,
                                value,
                                "pyproject.toml: setuptools package-dir",
                                sources,
                                warnings,
                            )
        return backend

    @staticmethod
    def _append_strings(
        values: object,
        path: Path,
        group: str,
        deps: list[DependencyDeclaration],
        warnings: set[str],
    ) -> None:
        if values is None:
            return
        if isinstance(values, list) and all(isinstance(value, str) for value in values):
            deps.extend(DependencyDeclaration(value, path, group) for value in values)
        else:
            warnings.add(f"{path.name}: unresolved {group} dependencies")

    def _requirements(
        self, path: Path, deps: list[DependencyDeclaration], warnings: set[str]
    ) -> None:
        if path.name == "Pipfile":
            return
        raw = self._read(path, warnings)
        if raw is None:
            return
        name = path.stem.lower()
        group = (
            "runtime"
            if name == "requirements"
            else "test"
            if "test" in name
            else "development"
            if "dev" in name
            else "unknown"
        )
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                deps.append(DependencyDeclaration(stripped, path, group))

    def _setup_cfg(
        self,
        path: Path,
        root: Path,
        deps: list[DependencyDeclaration],
        reqs: list[PythonRequirementDeclaration],
        names: list[tuple[str, Path]],
        sources: list[tuple[Path, str]],
        warnings: set[str],
    ) -> None:
        raw = self._read(path, warnings)
        if raw is None:
            return
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(raw)
        except configparser.Error:
            warnings.add("setup.cfg: malformed configuration")
            return
        if parser.has_option("metadata", "name"):
            names.append((parser.get("metadata", "name").strip(), path))
        if parser.has_option("options", "python_requires"):
            reqs.append(
                PythonRequirementDeclaration(parser.get("options", "python_requires").strip(), path)
            )
        if parser.has_option("options", "install_requires"):
            for value in parser.get("options", "install_requires").splitlines():
                if value.strip() and not value.lstrip().startswith("#"):
                    deps.append(DependencyDeclaration(value.strip(), path, "runtime"))
        if parser.has_section("options.extras_require"):
            for group, text in parser.items("options.extras_require"):
                for value in text.splitlines():
                    if value.strip() and not value.lstrip().startswith("#"):
                        deps.append(DependencyDeclaration(value.strip(), path, f"optional:{group}"))
        if parser.has_option("options", "package_dir"):
            for line in parser.get("options", "package_dir").splitlines():
                if "=" in line:
                    self._explicit_root(
                        root,
                        line.split("=", 1)[1].strip(),
                        "setup.cfg: package_dir",
                        sources,
                        warnings,
                    )

    def _setup_py(
        self,
        path: Path,
        deps: list[DependencyDeclaration],
        reqs: list[PythonRequirementDeclaration],
        names: list[tuple[str, Path]],
        warnings: set[str],
    ) -> None:
        raw = self._read(path, warnings)
        if raw is None:
            return
        try:
            tree = ast.parse(raw, filename=str(path))
        except SyntaxError:
            warnings.add("setup.py: syntax error; static metadata unavailable")
            return
        calls = [
            node.value
            for node in tree.body
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and (
                isinstance(node.value.func, ast.Name)
                and node.value.func.id == "setup"
                or isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "setup"
            )
        ]
        for call in calls:
            for keyword in call.keywords:
                if keyword.arg not in {
                    "name",
                    "python_requires",
                    "install_requires",
                    "extras_require",
                }:
                    continue
                try:
                    value = ast.literal_eval(keyword.value)
                except (ValueError, TypeError, SyntaxError, RecursionError):
                    warnings.add(f"setup.py: dynamic {keyword.arg} unresolved statically")
                    continue
                if keyword.arg == "name" and isinstance(value, str):
                    names.append((value, path))
                elif keyword.arg == "python_requires" and isinstance(value, str):
                    reqs.append(PythonRequirementDeclaration(value, path))
                elif keyword.arg == "install_requires":
                    self._append_strings(value, path, "runtime", deps, warnings)
                elif keyword.arg == "extras_require" and isinstance(value, dict):
                    for group, items in value.items():
                        self._append_strings(items, path, f"optional:{group}", deps, warnings)
                else:
                    warnings.add(f"setup.py: unsupported static {keyword.arg}")

    def _pytest_ini(
        self,
        path: Path,
        root: Path,
        tests: list[tuple[Path, str]],
        pytest: set[str],
        warnings: set[str],
    ) -> None:
        raw = self._read(path, warnings)
        if raw is None:
            return
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(raw)
        except configparser.Error:
            warnings.add(f"{path.name}: malformed configuration")
            return
        if path.name == "pytest.ini" or parser.has_section("pytest"):
            pytest.add(f"{path.name}: pytest configuration")
        if parser.has_option("pytest", "testpaths"):
            self._configured_testpaths(
                parser.get("pytest", "testpaths").split(), root, path.name, tests, warnings
            )

    @staticmethod
    def _configured_testpaths(
        value: object, root: Path, origin: str, tests: list[tuple[Path, str]], warnings: set[str]
    ) -> None:
        if not isinstance(value, (str, list)):
            return
        names = value.split() if isinstance(value, str) else value
        for name in names:
            if isinstance(name, str):
                candidate = root / name
                if candidate.is_symlink():
                    warnings.add(f"Skipped symlink test root: {name}")
                elif candidate.is_dir() and candidate.resolve().is_relative_to(root):
                    tests.append((candidate.resolve(), f"{origin}: testpaths"))
                else:
                    warnings.add(f"Configured test root unavailable: {name}")

    @staticmethod
    def _explicit_root(
        root: Path, value: str, origin: str, sources: list[tuple[Path, str]], warnings: set[str]
    ) -> None:
        if not value or value == ".":
            candidate = root
        else:
            candidate = root / value
        if candidate.is_symlink():
            warnings.add(f"Skipped symlink source root: {value}")
        elif candidate.is_dir() and candidate.resolve().is_relative_to(root):
            sources.append((candidate.resolve(), origin))
        else:
            warnings.add(f"Explicit source root unavailable: {value}")

    def _source_roots(
        self,
        root: Path,
        entries: dict[str, Path],
        explicit: list[tuple[Path, str]],
        test_roots: tuple[Path, ...],
        warnings: set[str],
    ) -> tuple[tuple[Path, ...], tuple[str, ...]]:
        evidence = list(explicit)
        for name in ("src", "lib"):
            path = entries.get(name)
            if path is not None and path.is_dir() and self._has_python(path):
                evidence.append((path, f"convention: {name}/"))
        flat_packages = [
            p
            for name, p in entries.items()
            if p.is_dir()
            and p not in test_roots
            and name not in _EXCLUDED_DIRS
            and name not in {"src", "lib", "tests", "test", "requirements"}
            and (p / "__init__.py").is_file()
        ]
        root_modules = [
            p
            for name, p in entries.items()
            if p.is_file()
            and p.suffix == ".py"
            and name not in {"setup.py", "conftest.py"}
            and not name.startswith("test_")
        ]
        if flat_packages or root_modules:
            evidence.append((root, "convention: flat package or root module"))
        roots = tuple(sorted({path for path, _ in evidence}, key=lambda p: self._relative(root, p)))
        if len(roots) > 1:
            warnings.add("Multiple possible source roots; all were retained.")
        if not roots and any(
            name in entries for name in ("pyproject.toml", "setup.cfg", "setup.py")
        ):
            warnings.add("No identifiable source root.")
        return roots, tuple(
            sorted(f"{self._relative(root, path)}: {reason}" for path, reason in evidence)
        )

    @staticmethod
    def _has_python(directory: Path) -> bool:
        try:
            return any(p.is_file() and p.suffix == ".py" for p in directory.iterdir()) or any(
                p.is_dir() and not p.is_symlink() and any(c.suffix == ".py" for c in p.iterdir())
                for p in directory.iterdir()
            )
        except OSError:
            return False

    def _test_roots(
        self,
        root: Path,
        entries: dict[str, Path],
        configured: list[tuple[Path, str]],
        warnings: set[str],
    ) -> tuple[tuple[Path, ...], tuple[str, ...]]:
        evidence = list(configured)
        for name in ("tests", "test"):
            path = entries.get(name)
            if path is not None and path.is_dir():
                evidence.append((path, f"convention: {name}/"))
        roots = tuple(sorted({path for path, _ in evidence}, key=lambda p: self._relative(root, p)))
        return roots, tuple(
            sorted(f"{self._relative(root, path)}: {reason}" for path, reason in evidence)
        )

    def _modules(
        self,
        root: Path,
        source_roots: tuple[Path, ...],
        test_roots: tuple[Path, ...],
        warnings: set[str],
    ) -> tuple[PythonModuleInfo, ...]:
        modules: dict[Path, PythonModuleInfo] = {}
        examined = 0
        for source_root, is_test in (
            *((path, False) for path in source_roots),
            *((path, True) for path in test_roots),
        ):
            stack = [source_root]
            while stack:
                directory = stack.pop()
                try:
                    children = sorted(directory.iterdir(), key=lambda p: p.name)
                except OSError:
                    warnings.add(f"Could not list directory: {self._relative(root, directory)}")
                    continue
                for path in children:
                    relative = self._relative(root, path)
                    if path.is_symlink():
                        warnings.add(f"Skipped symlink: {relative}")
                    elif path.is_dir():
                        if path.name not in _EXCLUDED_DIRS and (
                            is_test
                            or not any(path == t or path.is_relative_to(t) for t in test_roots)
                        ):
                            stack.append(path)
                    elif path.is_file() and path.suffix == ".py":
                        if not is_test and (
                            path.name in {"setup.py", "conftest.py"}
                            or path.name.startswith("test_")
                        ):
                            continue
                        if path in modules:
                            continue
                        examined += 1
                        if examined > self.max_python_files:
                            warnings.add(
                                "Python file count limit reached; remaining modules skipped."
                            )
                            return tuple(
                                sorted(modules.values(), key=lambda m: self._relative(root, m.path))
                            )
                        raw = self._read(path, warnings)
                        if raw is None:
                            continue
                        functions: tuple[FunctionSummary, ...] = ()
                        try:
                            tree = ast.parse(raw, filename=str(path))
                            functions = tuple(
                                FunctionSummary(
                                    node.name,
                                    min(
                                        (d.lineno for d in node.decorator_list), default=node.lineno
                                    ),
                                    node.end_lineno or node.lineno,
                                    isinstance(node, ast.AsyncFunctionDef),
                                )
                                for node in tree.body
                                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                            )
                        except SyntaxError:
                            warnings.add(f"Syntax error in Python module: {relative}")
                        module_name = self._module_name(path, source_root)
                        if module_name is None and not is_test:
                            warnings.add(f"Module name uncertain: {relative}")
                        modules[path] = PythonModuleInfo(
                            path, module_name, source_root, is_test, functions
                        )
        return tuple(sorted(modules.values(), key=lambda m: self._relative(root, m.path)))

    @staticmethod
    def _module_name(path: Path, source_root: Path) -> str | None:
        parts = list(path.relative_to(source_root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        if not parts or not all(_IDENTIFIER.fullmatch(part) for part in parts):
            return None
        return ".".join(parts)

    def _test_framework_imports(self, path: Path) -> set[str]:
        try:
            if path.stat().st_size > self.max_source_file_bytes:
                return set()
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SyntaxError):
            return set()
        names: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("pytest"):
                        names.add("pytest")
                    elif alias.name in {"unittest", "unittest.case"}:
                        names.add("unittest")
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                if node.module.startswith("pytest"):
                    names.add("pytest")
                elif node.module in {"unittest", "unittest.case"}:
                    names.add("unittest")
        return names

    @staticmethod
    def _manager_evidence(
        root: Path, entries: dict[str, Path], dependency_files: tuple[Path, ...]
    ) -> list[tuple[str, Path]]:
        managers: list[tuple[str, Path]] = []
        for filename, name in (
            ("uv.lock", "uv"),
            ("poetry.lock", "poetry"),
            ("Pipfile", "pipenv"),
            ("Pipfile.lock", "pipenv"),
        ):
            if filename in entries and entries[filename].is_file():
                managers.append((name, entries[filename]))
        if any(path.name != "Pipfile" for path in dependency_files):
            managers.append(
                (
                    "pip/requirements",
                    next(path for path in dependency_files if path.name != "Pipfile"),
                )
            )
        if not managers and "pyproject.toml" in entries:
            managers.append(("pyproject", root / "pyproject.toml"))
        return sorted(managers, key=lambda item: (item[0], item[1].name))

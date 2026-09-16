# Phase 5A Report — Repository Inspection and Project Discovery

## Phase Overview

Phase 5A adds static inspection of a local project directory alongside the frozen single-file generation pipeline. It produces an immutable `ProjectProfile` and optional portable JSON. It does not prepare an environment, import target code, run tests, or generate tests for a repository.

## Files Created

- `src/autotest/project_inspector.py`
- `tests/test_project_inspector.py`
- `tests/fixtures/projects/` with modern `src`, requirements, legacy `setup.cfg`, static/dynamic `setup.py`, malformed-source, and non-Python fixture layouts
- `docs/phase_reports/phase-5a.md`

## Files Modified

- `src/autotest/errors.py` — inspection-specific application error
- `src/autotest/__init__.py` — additive inspector/profile exports
- `src/autotest/main.py` — additive inspection CLI branch
- `README.md` — usage, support tiers, and safety boundary
- `docs/PROJECT_STATE.md` — current Phase 5A handoff

## Architecture Changes

`ProjectInspector.inspect(root)` validates a local directory, reads known project metadata as bounded text, parses TOML/CFG and AST literals, discovers source/test roots, and scans Python declarations without importing modules. `ProjectAnalyzer` remains the detailed single-file analyzer. `ProjectProfile`, `PythonModuleInfo`, `FunctionSummary`, `DependencyDeclaration`, and `PythonRequirementDeclaration` are frozen dataclasses. The inspector tracks evidence and sorted warnings for uncertain or unsupported interpretations.

## Behavioral Contracts

- `--inspect-project` is a separate static mode. It requires neither `--file` nor `--function`, makes zero LLM calls, runs no target pytest, and exits 0 on completed inspection or 2 on inspection/output errors. A non-Python directory is a completed profile with `is_python_project=false`.
- Existing `--file`/`--function` generation, repair, coverage, mutation, and artifact behavior and the frozen execution status/exit mapping remain unchanged. Inspection does not write a Phase 1–4B run artifact.
- Paths in serialized profiles are project-relative. Ordering and normalized SHA-256 are deterministic for unchanged content. An explicit output file is written UTF-8 with exclusive creation.
- Missing or non-directory roots raise `ProjectInspectionError`; malformed metadata/source and unsupported dynamic declarations generally become warnings rather than aborting the profile.

## Configuration and Baseline

Baseline before changes: `175 passed, 3 skipped, 3 deselected`; latest frozen tag `phase-4b-frozen`. Default source-file bound is 2 MiB; default Python-file count bound is 10,000. No environment baseline, Ollama, WSL, or mutation configuration changed.

## Supported Metadata Formats

| Tier | Files | Treatment |
| --- | --- | --- |
| Structured | `pyproject.toml` | `[project]` name/Python requirement/runtime/optional dependencies, `[build-system]` requirements/backend, dependency groups, pytest testpaths, static setuptools package-dir |
| Conservative declarations | root `requirements.txt`, `requirements-*.txt`, `requirements_*.txt`, and `requirements/*.txt` | Nonblank, noncomment entries kept verbatim with source file and context group; includes/editables/URLs are not resolved |
| Static legacy | `setup.cfg`, `setup.py` | Selected options, extras and package-dir from CFG; only literal `setup(...)` keyword values from top-level AST calls |
| Detection only | `uv.lock`, `poetry.lock`, `Pipfile`, `Pipfile.lock` | Presence/workflow evidence; solver and Pipfile declaration semantics are not interpreted |

Python requirements from multiple files retain all origins; conflicting values produce a warning and no single chosen requirement. Multiple manager ecosystems likewise produce a warning and no chosen manager. `build_backend` is reported only when explicitly declared. Groups are assigned from declaration context, never from dependency package names.

## Static Inspection Safety

The inspector uses only standard-library filesystem, TOML, CFG, JSON, hashing, and AST operations on inspected repositories. `setup.py` is parsed, never executed; dynamic expressions such as `load_requirements()` produce warnings. Source modules are not imported. Directory/file symlinks are skipped during discovery and scan; configured roots must resolve inside the project. Common VCS/cache/venv/build/output directories are excluded. Oversized Python files and syntax-invalid modules are skipped or represented with empty declarations and warnings without halting the repository scan.

## ProjectProfile Schema

The JSON has schema version 1, declared or fallback project name, root `.`; Python-project flag and requirement with all requirement origins; metadata/dependency/lockfile paths; build backend; package manager and evidence; test framework and evidence; source/test roots and evidence; raw dependency declarations with groups; module paths/names/test flags and top-level function summaries; warnings; and `profile_sha256`. No absolute checkout path is serialized.

## Source/Test Discovery Rules

Explicit setuptools package-dir paths are retained as source evidence; `src/`, `lib/`, and flat packages/root modules are conservative conventions. Multiple candidates are retained with a warning. Test roots come from pyproject/pytest.ini/tox.ini `testpaths` and `tests/` or `test/` conventions. Module names derive from source-root-relative identifier components; uncertain names are `null`. AST discovery includes only module-level sync/async `def`, including decorator start lines; methods, nested functions, and lambdas are excluded. Test modules are represented separately with `is_test_module=true`.

## Dependency Changes

None. Only Python 3.12 standard-library modules were added to production imports. `uv.lock` remains unchanged at SHA-256 `474378DFF8B726C8AF35C97E3CE7518F4240BFD1A28542B76736ACA55B4901B7`.

## Data / Artifact Changes

Inspection persists only when `--profile-output` is supplied. The demonstration output is `workspace/project_profiles/modern_project_phase5a.json`, under an AutoTest-controlled ignored directory. No existing run artifact schema changed.

## Tests Added or Modified

Thirteen offline inspector tests cover seven local fixture layouts, project-name/Python/dependency/backend extraction, requirements raw values and groups, setup.cfg and literal setup.py, dynamic setup.py without execution, module-level side effects without import, malformed TOML/setup.py/Python source, conflicting declarations/managers, missing/file/empty/non-Python/Unicode-space roots, configured/multiple roots, pytest/unittest evidence, symlinks, excluded directories, file-size limit, JSON/hash stability, and CLI isolation/output/error paths. Existing CLI regression tests remain unchanged.

## Validation Performed

On 2026-09-16:

- `uv sync --frozen`: PASS — 12 packages checked.
- `uv run ruff format --check .`: PASS — 71 files already formatted.
- `uv run ruff check .`: PASS.
- `uv run pytest`: PASS — **188 passed, 3 skipped, 3 deselected** in 9.15 seconds.
- `uv run pytest tests/test_project_inspector.py -q`: PASS — 13 passed.

The three skipped tests require opt-in Ollama; the three deselected tests require opt-in mutation infrastructure. The default suite remains offline.

## End-to-End Inspection Demonstration

Command:

```powershell
uv run python -m autotest.main `
    --inspect-project tests/fixtures/projects/modern_project `
    --profile-output workspace/project_profiles/modern_project_phase5a.json
```

Exit 0; printed project `demo`, Python requirement `>=3.10`, backend `setuptools.build_meta`, workflow `uv`, `src` source root, `tests` test root, 3 modules, 3 top-level functions, and pytest evidence. JSON inspection confirmed project-relative paths, raw grouped declarations, lockfile detection, warning, and normalized profile hash. Fixture file hashes matched before and after the inspection demonstration. The inspection path did not instantiate Ollama or a pytest runner; the side-effect fixture test verifies target Python and setup code were not executed.

## Acceptance Criteria

All applicable Phase 5A acceptance criteria pass: static and deterministic profile; required metadata/support tiers; safe setup.py analysis; root/module/function discovery; bounds and symlink safety; portable JSON; separate zero-LLM inspection CLI; unchanged frozen regression suite and lockfile; documentation and phase report; and successful actual fixture inspection.

## Known Limitations

Metadata support is intentionally conservative. Pipfile and lockfile semantics, advanced setuptools/Poetry/Hatch configuration, dynamic setup.py values, package import resolution, namespace-package confidence, and arbitrary framework semantics are not implemented. A missing or ambiguous layout may yield warnings or multiple candidate roots. A source file beyond the bound is skipped. The profile describes declarations and files, not an executable environment.

## Risks / Technical Debt

Inferred package-manager/workflow and source roots are evidence, not environment decisions. Requirements are raw declarations and may include includes, options, or VCS URLs that a later phase must interpret safely. Read-only static inspection does not establish whether dependencies are installable or tests are runnable. Phase 5B must define isolated environment preparation separately.

## Compatibility Notes

No existing artifact schema, execution status, exit code, provider, mutation backend, dependency pin, or `uv.lock` entry changed. The inspection branch exits before constructing generation components. Existing generation CLI arguments remain required when inspection mode is absent.

## Git / Repository State

The worktree was clean before Phase 5A implementation. This phase leaves its code, fixture, test, and documentation changes in the worktree for review. No commit or tag was created. Ignored demonstration profiles remain under `workspace/`.

## Recommended Next Phase

Phase 5B — Target Environment Management: consume the static profile to plan and prepare isolated target environments, with explicit dependency/workflow selection and source integrity safeguards. Do not infer environment readiness from the Phase 5A profile alone.

## Final Phase Status

PHASE STATUS: COMPLETE
READY TO FREEZE: YES

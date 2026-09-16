# Phase 5B Report — Target Environment Planning, Isolation and Provisioning

## Phase Overview

Phase 5B consumes a Phase 5A `ProjectProfile`, plans whether its declared Python and runtime dependencies can be prepared, and optionally creates a fresh copied workspace with a verified target venv. It does not generate repository tests or run target code. Frozen single-file generation and static inspection remain separate.

## Files Created

- `src/autotest/environment_planner.py`
- `src/autotest/environment_provisioner.py`
- `tests/test_environment_planner.py`, `tests/test_environment_provisioner.py`, `tests/test_environment_cli.py`
- `tests/fixtures/projects/environment_no_deps/`, `environment_pyproject/`, and `environment_requirements/`
- `docs/phase_reports/phase-5b.md`

## Files Modified

- `src/autotest/errors.py` — phase-specific planning/provisioning errors.
- `src/autotest/project_inspector.py` — exposes the existing exclusion set under a public constant, preserving the old alias.
- `src/autotest/main.py` — additive environment CLI routes and summaries.
- `src/autotest/__init__.py` — additive public environment model exports.
- `README.md`, `docs/PROJECT_STATE.md` — usage, architecture, limits, and handoff.

## Architecture Changes

Planning and provisioning are distinct. `EnvironmentPlanner.plan(profile, interpreter, offline=False)` is a pure policy decision over Phase 5A evidence. `EnvironmentProvisioner.provision(project_root, profile, plan, output_root)` checks identity, copies and verifies source, creates a venv, installs dependencies with uv, verifies runner metadata, and writes artifacts. `EnvironmentCommandRunner` is the bounded external-command boundary and can be replaced for offline tests.

## Behavioral Contracts

- Generation with `--file`/`--function` and inspection with `--inspect-project` retain their existing pipeline and exit status contracts. Environment modes are mutually exclusive with them.
- Planning never creates a venv or workspace, installs packages, accesses the network, calls Ollama, or executes target code. An unsupported repository returns a valid `UNSUPPORTED` plan.
- Provisioning does not install the target project, execute `setup.py` or target build hooks, import the target package, or run its tests. It does not intentionally write inside the original repository.
- Every preparation gets a fresh workspace. A `READY` result requires equal copy manifests, completed commands, matching runner versions, and an unchanged original manifest.

## EnvironmentPlan Schema

The frozen dataclass records schema version 1 JSON with profile hash, project name, `SUPPORTED`/`UNSUPPORTED`, selected interpreter, declared Python requirement, `COMPATIBLE`/`INCOMPATIBLE`/`UNKNOWN`, strategy, project-relative dependency sources and ordered requirements, pinned runner requirements, lockfile paths, offline/network policy, `install_target_project=false`, warnings, unsupported reasons, and a normalized SHA-256 plan hash. No timestamp enters the plan hash. The selected interpreter path is intentionally machine-specific.

## TargetEnvironment Schema

The frozen result records `READY`, `UNSUPPORTED`, `ERROR`, `TIMEOUT`, `COPY_INTEGRITY_ERROR`, or `SOURCE_INTEGRITY_ERROR`; workspace/source/venv/Python paths; selected Python and strategy; separate target and runner installation flags; original/copy/profile verification flags; command summaries with argv, exit, duration, timeout, and artifact path; tool and uv versions; plan/profile hashes; offline flag; copy counts/bytes/duration; total duration; warnings; and an error message. `provision_result.json` persists the result. Each command also has separate stdout, stderr, and JSON metadata files.

## Interpreter Selection

The current AutoTest CPython is the default. `--target-python` selects one explicit local executable, probed with isolated `-I -c` execution of the interpreter only. There is no computer-wide search or automatic Python download. Non-CPython and incompatible selections yield unsupported plans; probe failures are infrastructure errors.

## Python Requirement Compatibility

The standard-library checker supports numeric `>=`, `>`, `<=`, `<`, `==`, `!=`, and `~=` clauses, comma conjunctions, and `==`/`!=` prefix wildcards. It does not attempt full PEP 440 handling. Unparseable declarations become `UNKNOWN` and unsupported. Conflicting Phase 5A requirement declarations are unsupported. An absent declaration is `UNKNOWN` with a warning, but does not by itself block preparation.

## Dependency Strategy Selection

`NONE` handles repositories without runtime dependencies. `REQUIREMENTS` accepts only Phase 5A runtime entries that match a conservative registry requirement subset, retaining environment markers. `PYPROJECT_DECLARATIONS` uses static `[project].dependencies`. Runtime entries alone are installed; optional, development, and test groups are ignored. Simultaneous pyproject and requirements runtime declarations have no precedence rule and are unsupported. Provisioning uses the strategy and exact inputs from the plan and does not infer them again.

## Supported / Unsupported Project Classes

Supported V1 projects have a local compatible CPython and no runtime dependencies, simple registry requirements, or static pyproject runtime declarations, with no conflicting manager/Python evidence. Unsupported cases include unsafe requirements directives, editable/direct URL/VCS/local path requirements, dynamic or unresolved dependency metadata, legacy runtime dependencies, conflicting managers, Poetry/Pipenv, and complex or incompatible Python requirements. `uv.lock` is retained as evidence with a warning: exact lock replay is not implemented. External OS/service requirements and projects requiring their own build are outside V1.

## Workspace Isolation

The default root is `workspace/target_environments`; each request reserves a fresh UTC-and-random-suffix directory. It contains profile/plan/result JSON, original/copied manifests, `source/`, `.venv/`, and `commands/`. The output root is rejected if it resolves inside the original project. Mutable provisioning work uses the copied workspace as its working directory.

## Repository Copy Rules

The copy uses Phase 5A's directory exclusions, including `.git`, venvs, caches, build/dist, `site-packages`, `node_modules`, and `workspace`. Directory and file symlinks/junctions are skipped. Only bounded regular files are copied: at most 20,000 files, 32 MiB per file, and 512 MiB total. Exceeding a bound fails before dependency installation.

## Integrity Verification

The original manifest records sorted project-relative paths, sizes, and SHA-256 hashes. The copied manifest is recomputed from `source/` and must match before venv creation; mismatch is `COPY_INTEGRITY_ERROR`. A copied Phase 5A profile hash is compared when practical. The original manifest is recomputed after any completed or failed provisioning operation; a mismatch overrides the result with `SOURCE_INTEGRITY_ERROR`. This detects changes but is not a filesystem write barrier or a defense against hostile concurrent mutation.

## Dependency Provisioning

The selected interpreter creates the venv with `-m venv`. The installed uv CLI is located with `shutil.which`; each install passes separate validated requirement argv elements plus `--no-config`, `--no-python-downloads`, `--no-build`, and an explicit venv `--python` path. `--environment-offline` adds uv's documented `--offline` flag, verified against local `uv pip install --help` before implementation. Commands run without a shell, from the workspace, with a 600-second default per-command timeout and a limited environment. Default preparation may use the package index. No target package build or installation is attempted.

## Runner Tool Provisioning

After target dependency installation, a separate uv command installs `pytest==8.4.2`, `coverage==7.16.0`, and `pytest-timeout==2.4.0`. The target venv Python queries `importlib.metadata` under `-I` and must report exact planned versions. Mutmut, Ollama-related packages, and AutoTest are not installed in the target venv.

## CLI Changes

`--plan-environment <directory>` prints support, Python, strategy, runner pins, network policy, and reasons; supported and unsupported plans exit 0, infrastructure errors exit 2. `--prepare-environment <directory>` prints the plan and result; only `READY` exits 0. Additive options are `--target-python`, `--environment-output-root`, `--environment-timeout`, and `--environment-offline`. Environment modes do not instantiate Ollama or enter generation.

## Configuration and Baseline

The frozen starting baseline was `188 passed, 3 skipped, 3 deselected`; the latest frozen tag was `phase-5a-frozen`. The AutoTest interpreter is CPython 3.12.10. The real demonstration used uv 0.12.13. No frozen generation, coverage, mutation, or inspection configuration changed.

## Dependency Changes

None. Production code uses the standard library and the existing external uv CLI. `pyproject.toml` and `uv.lock` were not changed. `uv.lock` SHA-256 remains `474378DFF8B726C8AF35C97E3CE7518F4240BFD1A28542B76736ACA55B4901B7`.

## Data / Artifact Changes

Environment preparation writes only to its fresh target workspace. Artifacts include full argv arrays, stdout/stderr, exit code, duration, timeout status, plan/profile hashes, manifest hashes, and verification results. Existing generation run artifacts and inspection profile schema are unchanged. The ignored `workspace/target_environments/` directory contains demonstration evidence.

## Tests Added or Modified

Forty-eight offline Phase 5B tests cover Python compatibility and unknown cases, interpreter probing, dependency strategies and unsafe references, manager/Python conflicts, deterministic plan serialization, bounded copy and exclusions, symlink skipping, manifest/profile checks, tampered-plan rejection, command argv/offline/timeout/failure handling, version mismatch, original mutation detection, output-root rejection, unsupported plans, CLI mode routing, and no Ollama invocation. The no-dependency fixture package raises at import, so a successful preparation also checks that target imports do not occur. Frozen tests were not modified.

## Validation Performed

On 2026-09-16:

- `uv sync --frozen`: PASS — 12 packages checked.
- `uv run ruff format --check .`: PASS — 81 files already formatted.
- `uv run ruff check .`: PASS.
- `uv run pytest`: PASS — **236 passed, 3 skipped, 3 deselected**.
- Focused Phase 5B tests: **48 passed**.

The skipped tests need opt-in Ollama; mutation integration remains deselected by default. The default test suite needs no network, Ollama, WSL, or live uv installation.

## Real Provisioning Demonstration

`uv run python -m autotest.main --plan-environment tests/fixtures/projects/environment_no_deps` returned `SUPPORTED`, CPython 3.12.10, requirement `>=3.10,<3.13` as `COMPATIBLE`, strategy `NONE`, the three runner pins, and no target project installation. It created no workspace or venv.

An initial `--prepare-environment ... --environment-offline` created an evidence workspace but returned `ERROR` at `install-runner-tools`: the local uv cache did not contain the pinned pytest wheel. The failure result and unchanged original manifest were persisted; no `READY` result was fabricated.

The normal preparation command succeeded:

```powershell
uv run python -m autotest.main --prepare-environment `
    tests/fixtures/projects/environment_no_deps `
    --environment-output-root workspace/target_environments
```

The final-code run created `workspace/target_environments/20260916T121203Z-a3ed67` with a fresh `source/` and `.venv/`. `provision_result.json` reports `READY`, `copy_integrity_verified=true`, `profile_copy_verified=true`, `original_integrity_verified=true`, and 3 copied files/223 bytes. The original and copied manifests match. The target venv reports Python 3.12.10, pytest 8.4.2, coverage 7.16.0, and pytest-timeout 2.4.0. Its `unsafe_demo` package raises on import, yet preparation succeeded, demonstrating no target import. The original fixture remained unchanged.

## Acceptance Criteria

The applicable Phase 5B criteria pass: frozen regressions, pure and deterministic planning, conservative policy, fresh bounded copy, manifest verification, isolated venv, separate runner tooling and exact version verification, safe bounded argv commands, additive CLI, offline tests, real local `READY` preparation, unchanged dependencies/lockfile, and documentation. The offline demonstration failure is a cache availability limit, not a requirement for phase completion.

## Known Limitations

No exact lockfile replay, package hash pinning, full PEP 440 or requirements syntax, recursive includes/constraints, optional-group selection, Conda/Poetry/Pipenv provisioning, Python download, external service setup, or environment cache/reuse. Runtime dependency success is based on uv exit status; runner versions are checked explicitly. Prepared environments are not yet used by repository test generation.

## Security Boundary

The original repository is read-only by convention and checked by hash after provisioning. The copied workspace and venv isolate state, not hostile code. Third-party installation and any future target execution may affect the host; the venv is not a container or VM sandbox. Symlinks are skipped, but hash checks do not prevent a malicious concurrent process from changing files between scan and copy. No target project or test code is intentionally executed in Phase 5B.

## Risks / Technical Debt

Exact reproducibility depends on registry availability and dependency resolution because lock replay is deferred. Registry wheel availability varies by platform/interpreter. Copy/profile scans can race with external edits; an original-manifest recheck detects persistent differences. The current interpreter specifier subset is intentionally conservative. Unrecognized external service or system dependencies cannot be inferred reliably from static Phase 5A metadata.

## Compatibility Notes

Phase 1–4B PASS/FAIL/ERROR/TIMEOUT and exits 0/1/2/3 are unchanged. Phase 5A inspection behavior and serialized profile remain unchanged. Phase 5B uses separate support and provision statuses; no environment result is a test PASS.

## Git / Repository State

The worktree was clean at implementation start. Phase 5B changes are uncommitted and untagged for review. Demonstration workspaces are ignored. `uv.lock` matches its starting SHA-256.

## Recommended Next Phase

Phase 5C — Cross-file Dependency and Context Selection. Use repository structure to choose bounded source context. Repository generation and execution belong to a later phase.

## Final Phase Status

PHASE STATUS: COMPLETE
READY TO FREEZE: YES

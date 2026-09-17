# Phase 5D Report — Repository-Scale End-to-End Pilot

## Phase Overview

Phase 5D connects the frozen Phase 5A profile, Phase 5B copied target environment, and Phase 5C static context to the existing generation, repair, coverage, and optional mutation loops. It is a controlled single-target repository pilot, not general repository execution. Work began from clean `main` at `phase-5c-frozen` (`43d7369`); the frozen baseline was 269 passed, 3 skipped, 3 deselected, with frozen sync and Ruff green.

## Files Created

- `src/autotest/repository_execution.py`, `repository_prompts.py`, `repository_runner.py`, `repository_mutation.py`.
- `tests/test_repository_runner.py`, `tests/test_repository_cli.py`, and `tests/fixtures/projects/repository_pilot/`.
- This report.

## Files Modified

- `src/autotest/test_runner.py`, `coverage_runner.py`, `main.py`, and `__init__.py` (additive repository-mode extension points and CLI exports).
- `pyproject.toml` (exclude intentionally hostile fixture tests from default collection via `norecursedirs`).
- `README.md` and `docs/PROJECT_STATE.md` (usage, handoff, limitations, and current frozen tag).

## Architecture Changes

`RepositoryRunEngine` orchestrates existing phase engines; `RepositoryExecutionContext` supplies the copied-root import path, target Python, minimal pytest config, and reduced child environment. `RepositoryPromptBuilder` adds verified, project-relative `ContextBundle` evidence to all prompt stages. `RepositoryWSLMutmutBackend` stages local support modules without broadening the selected mutation target. Frozen standalone mode remains the default path when repository mode is not selected.

## Repository Pipeline

Inspect → validate target → select static context → plan → provision a fresh copy and venv → verify input identity → generate and execute → bounded repair → baseline and optional feedback coverage → optional mutation evaluation and feedback → final integrity verification → persist root result. The LLM provider is not created before successful preflight.

## Repository Target Model

The public target is `ContextTarget(project-relative .py file, exact top-level function name)`. The profile must identify a single production module and import name. Methods, ambiguous module names, and unsupported layouts fail explicitly; the pilot does not guess an import path.

## Preflight Validation

The original profile, selected context, plan, copied profile/source, and provisioner status are checked before a model call. A changed context source yields stale-context failure. A mutation request for an unsupported repository layout fails before generation. The original source manifest is snapshotted and rechecked on completion.

## Context Integration

Context evidence is selected from the original profile and verified against the prepared copy. Exact declaration text, relative source paths, unresolved/external references, omissions, and the portable context SHA-256 are persisted. The context hash is checked again before prompt construction, so subsequent source changes stop the run.

## Target Environment Integration

Phase 5B's `EnvironmentPlanner` and `EnvironmentProvisioner` are reused. Generated tests do not run against the original checkout or the AutoTest interpreter: the prepared `TargetEnvironment.python_executable` launches pytest and coverage. The target package itself is imported from the verified copied source roots and is not installed into the venv.

## Execution Context

Child processes use explicit argv, target-venv Python, copied-source `PYTHONPATH`, a minimal environment, and timeouts. Python bytecode, pytest cache, and automatic plugin loading are disabled. This narrows accidental coupling but does not contain hostile code.

## Repository Prompting

Initial, repair, coverage, and mutation-feedback prompts include the selected import path and bounded static context. Evidence paths are project-relative; absolute checkout, prepared workspace, and venv paths are not sent as source context. Existing prompt constraints on testing rather than source repair remain in force.

## Repository Repair

Phase 2 `RepairEngine` keeps attempt numbering, PASS/FAIL/ERROR/TIMEOUT mapping, bounded retries, candidate-only replacement, and conservative assertion-failure guidance. Repository execution adds copied-source and accepted-test checks around each subprocess.

## Repository Coverage

Phase 3 `CoverageEngine` and target-function-only line/branch normalization are reused. Baseline measurement occurs even at zero feedback rounds. Supplementary test modules are separately generated; only passing, non-regressing, coverage-improving candidates join the accepted suite. Coverage child processes use the target venv and copied import roots.

## Repository Mutation

Mutation remains opt-in. For a supported dependency-free local module layout, a fresh WSL workspace receives the selected copied source module, local support modules, and accepted generated tests. Mutmut `3.7.0` uses its isolated WSL venv; only the named function's mutants count as applicable. Unsupported repository mutation is reported explicitly. No mutation-guided model call occurs with evaluation-only `--mutation`.

## Repository Mutation Feedback

`--mutation-feedback` reuses Phase 4B's survivor IDs/diffs, cumulative additional modules, stable-universe comparison, coverage non-regression, and mutation-score improvement checks. A deterministic repository-level test proves one survivor is killed by a newly accepted module; zero-survivor baseline makes no feedback model call.

## Integrity Enforcement

The original checkout is compared against its pre-provisioning manifest at the end. The prepared source has a stricter bounded manifest, including unexpected files/directories and symlinks, checked before and after each generated-test pytest/coverage subprocess and at the end. Accepted generated tests are hashed and rechecked around later subprocesses. Detected source or accepted-test modification stops the pipeline with error status; attack tests cover source edits, newly created cache directories, accepted-test overwrites, and original-checkout edits. These checks are detection, not write prevention.

## Artifact Structure

`workspace/repository_runs/<run-id>/` holds `result.json`, `context/`, `integrity/`, target-environment reference, and existing attempt/coverage/mutation/mutation-feedback hierarchies. The Phase 5B copy and venv are separately under `workspace/target_environments/<id>/`. Raw model, pytest, coverage, and Mutmut artifacts remain available, including raw and normalized mutation statistics.

## Metrics

`RepositoryRunResult` schema 1 records profile/plan/context hashes; context size/uncertainty; environment status; initial/final execution and repair; initial/final line and branch coverage, gains, and accepted rounds; optional mutation initial/final score, gain, applicable total/killed/survived; accepted test paths; source/test integrity booleans; stop reason; stage-specific LLM calls; timings; relative artifact references; and a stable result hash. PASS/FAIL/ERROR/TIMEOUT CLI exit codes remain 0/1/2/3.

## CLI Changes

`--run-project <directory> --project-target <relative-file.py>:<function>` selects the additive repository mode; `--repository-output-root` controls run artifacts. Existing target-Python, offline/environment timeout, context policy, provider, repair, coverage, and opt-in mutation controls apply. CLI modes are mutually exclusive. A sample command is in `README.md`.

## Configuration and Baseline

The AutoTest host uses Python 3.12.10 and pinned runner tools. The repository fixture is a small `src/demo` package with same-file and cross-file dependencies, a branch boundary, and intentionally failing pre-existing test/conftest files that must never be collected. The real WSL run used `Ubuntu-22.04` and `/home/ubuntu/autotest-mutation-env` (Python 3.10.12, pytest 8.4.2, Mutmut 3.7.0).

## Dependency Changes

None. `uv.lock` was not modified; its SHA-256 remains `474378DFF8B726C8AF35C97E3CE7518F4240BFD1A28542B76736ACA55B4901B7`. Mutmut remains outside the main Windows venv.

## Tests Added or Modified

Offline tests cover copied-package imports, real subprocess repair/coverage, existing-test/config isolation, stale/ambiguous/mismatched preflight, source and accepted-test tampering, original checkout mutation, status mapping, explicit unsupported mutation, fake mutation and transactional survivor feedback, WSL staging, portable prompt content, CLI exclusivity, and artifact/result metrics. Real target-venv, WSL mutation, and Ollama tests are opt-in via environment variables.

## Validation Performed

- `uv sync --frozen`: passed, 12 packages checked.
- `uv run ruff format --check .`: passed.
- `uv run ruff check .`: passed.
- `uv run pytest`: **290 passed, 6 skipped, 3 deselected**. The six skips are three earlier Ollama smoke tests and three new opt-in repository live tests; three earlier mutation integrations are deselected by default.

## Deterministic End-to-End Demonstration

The scripted provider plus copied-source fixture ran real pytest and coverage subprocesses: first candidate failed an import, Phase 2 repaired it to PASS, baseline target coverage was incomplete, and a supplementary candidate was accepted at 100% line/branch coverage. Existing repository test and `conftest.py` were not collected. The source and accepted generated tests stayed unchanged. A separate scripted mutation-feedback run accepted an additional passing test that changed one stable survivor to killed, improving score 0% → 100%.

## Real Target-Venv Demonstration

With `AUTOTEST_RUN_REPOSITORY_VENV=1`, the opt-in test provisioned a real Phase 5B target venv and executed generated pytest plus coverage from that venv. Result: PASS, 100% final line and branch coverage, one initial-generation and one coverage-generation model-stub call, source/test integrity true. This used no Ollama or WSL.

## Real Ollama Demonstration

Local Ollama listed `qwen2.5-coder:14b`. A real CLI invocation of `--run-project tests/fixtures/projects/repository_pilot --project-target src/demo/pricing.py:calculate_total` returned exit 0, `Environment READY`, `Execution PASS`, 100% line coverage, and unchanged original source. The opt-in real-provider test (`AUTOTEST_RUN_REPOSITORY_OLLAMA=1`) also passed: one live initial-generation call, final PASS, 100% line/branch coverage, source/test integrity true. The model's generated assertion is experimental evidence, not a semantic correctness guarantee.

## Real Mutation Demonstration

With `AUTOTEST_RUN_REPOSITORY_MUTATION=1`, the opt-in WSL integration passed after provisioning a real target venv and running real Mutmut. The result was COMPLETE; raw Mutmut export persisted `killed=6`, `survived=0`, `total=11`; normalized applicable counts were 6 killed, 0 survived, 5 unreported, score `6/(6+0) × 100 = 100%`. Both raw and normalized files persisted. The selected source and accepted generated tests were hash-unchanged, the WSL workspace was separate, and all mutation-feedback LLM-call counts were zero. The selected target remained `src/demo/pricing.py:calculate_total`, with local support modules copied but not selected for mutation.

## Acceptance Criteria

The controlled Phase 5D criteria pass: frozen contracts/regressions, single-target identity, verified copied execution, portable context prompts, target-venv pytest/coverage, bounded repair/coverage, optional mutation/feedback, source/test integrity detection, complete root artifacts, deterministic offline E2E, real target-venv/Ollama/WSL demonstrations, no dependency/lock change, and documentation. No Phase 6 work was started.

## Known Limitations

The pilot supports one profiled top-level function and conservative Phase 5B dependency plans. Dynamic imports, methods, broad re-export patterns, arbitrary existing test configuration, and dependency-heavy WSL mutation are unsupported. Static context may omit runtime-relevant behavior. A PASS, coverage percentage, or mutation score does not prove a correct oracle.

## Security Boundary

The copied workspace, venv, reduced environment, explicit pytest configuration, timeouts, and hash checks improve reproducibility and detect some changes; **they are not a hostile-code sandbox**. Imported target code, dependencies, and generated tests can still affect the host before detection. Use an external disposable VM/container for untrusted repositories. Docker/VM containment is outside Phase 5D.

## Risks / Technical Debt

Mutation currently assumes a supported local package layout and fixed WSL distribution configuration. Hash checking is post-hoc rather than OS-enforced. Probe/install steps may execute dependency tooling; Phase 5B's conservative plan deliberately rejects unsafe declarations. Reported live metrics are from one small fixture, not a benchmark. Phase 6 should evaluate quality, stability, and broader target coverage without weakening the current safety checks.

## Compatibility Notes

Standalone Phase 1–4B generation and Phase 5A/B/C inspection, planning, provisioning, and selection modes retain their schemas and status semantics. The repository runner is additive. `pyproject.toml` only excludes fixture directories from default pytest discovery; no production dependency or `uv.lock` changed.

## Git / Repository State

Implementation began clean at `phase-5c-frozen` on `main`. Phase 5D files are uncommitted and untagged for review; live run artifacts are ignored under `workspace/` or local pytest temporary directories. No commit or freeze tag was created.

## Recommended Next Phase

Phase 6 — Experiment and Benchmark Framework, after Phase 5D review/freeze. Do not infer universal repository support from this pilot.

## Final Phase Status

PHASE STATUS: COMPLETE
READY TO FREEZE: YES

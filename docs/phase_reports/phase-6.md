# Phase 6 Report — Experiment and Benchmark Framework

## Phase Overview

Phase 6 evaluates the frozen Phase 5D repository pilot. The starting Git tag is
`phase-5d-frozen` at `181892a`; work is on `phase-6-work`. The frozen baseline passed
`uv sync --frozen`, Ruff, and 290 passed / 6 skipped / 3 deselected. No Phase 5D generation,
repair, coverage, mutation, context, or environment implementation was changed.

## Files Created

- `src/autotest/experiment.py`, `experiment_runner.py`, `experiment_metrics.py`.
- `tests/test_experiment.py`.
- `experiments/example_phase6.json`, `experiments/live_phase6_mini.json`.
- `docs/PHASE6_METRICS.md` and this report.

## Files Modified

- `src/autotest/main.py`: three additive, exclusive experiment CLI modes.
- `README.md`: manifest, commands, artifacts, missingness, safety, and execution policy.
- `docs/PROJECT_STATE.md`: Phase 5D frozen state and Phase 6 handoff.

## Architecture Changes

`ExperimentRunner` expands a static `ExperimentPlan`, then invokes one fresh
`RepositoryRunEngine` per logical cell. It does not implement test generation or feedback.
Immutable definition/target/configuration/spec/result shells, explicit status records, an atomic
checkpoint, and normalized exports sit outside the frozen pipeline. Execution is sequential.

## Experiment Definition and Benchmark Manifest

Schema version 1 JSON holds a stable experiment name, local repositories, project-relative
top-level targets, configurations, repetitions, model/temperature, final mutation evaluation,
fail-fast, and bounded common controls. Relative repository paths resolve from the manifest.
Duplicate repository/target/configuration IDs and malformed target syntax fail validation.
The definition SHA-256 uses logical manifest paths and settings; runtime timestamps and durations
are excluded. A copied manifest snapshot and source checksum support resume identity checks.

## Canonical Configurations and Evaluation Separation

`direct`: initial generation, zero repair, zero coverage feedback, baseline coverage measurement
when executable, and no mutation feedback. `execution`: frozen repair, no coverage feedback.
`coverage`: repair plus coverage feedback. `mutation`: repair, coverage feedback, and mutation
feedback. A separate `evaluate_mutation` setting requests final mutation evaluation in all groups.
A/B/C never receive survivor diffs. The same model, temperature, target, context/environment
policy, coverage target, and timeouts apply to all configurations in one manifest.

## Experiment Planning, Run Identity, and Repeated Runs

Static preflight uses the existing inspector, context selector, interpreter probe, and environment
planner. It does not provision, install, call an LLM, run pytest, or run mutation. Unsupported
targets are retained as planned `UNSUPPORTED` rows. Expansion sorts repository ID, target ID,
1-based repetition, then canonical configuration order. The logical ID is
`repository::target::configuration::repetition`; a plan hash covers the definition hash and every
normalized spec, including policy and preflight evidence. Every repetition/configuration uses a
new provider, fresh generated suite, new copied environment, and separate repository-run artifacts.

## Checkpoint and Resume

Before each run, its spec and an `INCOMPLETE` record are written. Each finished/unsupported run
gets a run record and atomically replaced `experiment_state.json`. Resume loads the saved manifest,
replans, checks definition/plan/profile identity, skips complete records, and retries incomplete
ones. A changed source manifest or repository profile raises `RESUME_IDENTITY_MISMATCH`. The
deterministic test stopped after 6 of 16 cells, resumed exactly 10, and ended with 16 unique rows.

## Metrics Schema, Definitions, and Missingness

Record schema 1 has flat run identity, settings, statuses, context/environment evidence,
initial/final execution, repair, coverage, mutation raw/applicable/killed/survived/other counts,
stage LLM calls, stage runtime, integrity flags, and a raw Phase 5D result reference. Detailed
definitions and denominators are in `docs/PHASE6_METRICS.md`. `PASS` and `FAIL` are executable;
`ERROR` and `TIMEOUT` are not. A final generated-test `FAIL` is `COMPLETED` research data.
Missing measurements are JSON null and empty CSV cells. Mutation score keeps the frozen
`killed / (killed + survived) * 100` denominator; raw Mutmut total is separate.

## Aggregation and CSV/JSON Artifacts

Configuration, repository/configuration, and target/configuration groups report descriptive
count, mean, median, sample standard deviation, min, and max for available numeric observations.
Rates carry measured denominators. No significance claim or financial token cost is generated.
The experiment root contains `experiment_definition.json`, `experiment_plan.json`,
`version_evidence.json`, `experiment_state.json`, per-run specs/records/references,
`results/runs.json`, `results/runs.csv`, `results/aggregates.json`, `results/aggregates.csv`,
`results/failures.csv`, and `summary.json`. The aggregate CSV is one wide row per configuration.
Repository-run prompts, logs, venvs, and mutation workspaces are referenced, not duplicated.

## CLI Changes

`--validate-experiment` exits 0 for valid definitions even with unsupported targets and 2 for
invalid definitions. `--run-experiment` and `--resume-experiment` return 0 when orchestration
finishes and records are persisted, including generated-test failures or unsupported cells; an
incomplete fail-fast batch exits 2. These modes are exclusive with all earlier major modes.

## Offline Experiment Demonstration

The default offline test runs two targets, four configurations, and two repetitions: 16 fresh
repository-run invocations. A scripted provider, copied-source provisioner, and deterministic
mutation backend exercise real pytest/coverage subprocesses without Ollama, WSL, network, or
package installation. All 16 records and required exports were verified, including raw mutation
total 2, applicable total 1, killed 1, survived 0, and other/unreported 1 per measured run.
Canonical call exclusions, failing-suite preservation, unsupported continuation, fail-fast,
manifest errors, exact descriptive statistics, missingness, CLI validation, and resume identity
were tested. The six-then-ten resume demonstration passed.

## Live Ollama Mini-Experiment

Local `qwen2.5-coder:14b` was available. A real one-target, four-configuration, one-repetition
run completed at `workspace/experiments/phase6-live-mini/`. Direct, execution, and coverage each
returned `COMPLETED` / `PASS`, 100% target line and branch coverage, and one LLM call. Their
measured total runtimes were 87.97, 27.03, and 26.16 seconds respectively. These tiny fixture
results demonstrate orchestration only; they are not comparative research conclusions.

## Live Mutation Mini-Experiment

The initial desktop mini-run returned `PIPELINE_ERROR` / `MUTATION_ERROR` for its mutation cell:
the historical backend requested WSL registration `Ubuntu-22.04`, absent on this machine. That
failure remains in its original artifacts. Additive maintenance now resolves the effective distro
as explicit `--mutation-wsl-distribution`, then `AUTOTEST_MUTATION_WSL_DISTRO`, then historical
default `Ubuntu-22.04`. The distro name is runtime infrastructure, excluded from experiment
definition, plan, and logical run identity. The mutation artifact records the effective distro,
venv, Python, pytest, and Mutmut versions.

With `AUTOTEST_MUTATION_WSL_DISTRO=Ubuntu`, the real repository mutation integration passed:
`COMPLETE`, raw Mutmut total 11, applicable 6, killed 6, survived 0, other/unreported 5,
normalized score 100%, and verified source/accepted-test integrity. The desktop registration is
Ubuntu OS 22.04.1 on WSL 1; the venv is `/home/ubuntu/autotest-mutation-env` with Python 3.10.12,
pytest 8.4.2, and Mutmut 3.7.0.

The unchanged four-cell Phase 6 manifest was then rerun under an `E:` workspace. Its mutation
cell reached Mutmut but failed with `shutil.Error`: `Permission denied` copying
`tests/__pycache__` into `mutants/tests` on that WSL 1 mounted filesystem. A second run of the
same manifest from Windows Temp produced a `COMPLETED` / `PASS` mutation cell with `COMPLETE`
mutation evaluation: raw 11, applicable 6, killed 6, survived 0, other/unreported 5, score 100%,
source/test integrity true, effective distro `Ubuntu`. It used one LLM call and measured 100%
target line coverage. The saved definition and plan hashes match the original mini-run.
**LIVE MUTATION EXPERIMENT: VERIFIED.**

The Windows Temp rerun's Direct and Execution cells also completed PASS. Its Coverage cell had an
unrelated `LLM_ERROR`: `Ollama request timed out after 120 seconds`; the earlier mini-run's
Coverage cell had completed PASS.

After the desktop Ubuntu registration was converted to WSL 2, the unchanged manifest was run
again with its output on `E:` at `workspace/experiments-wsl2/phase6-live-mini/`. All four cells
(`direct`, `execution`, `coverage`, `mutation`) were `COMPLETED` / `PASS`; there were no unrelated
cell failures in this run. The Mutation backend was `COMPLETE` with raw total 11, applicable 6,
killed 6, survived 0, other/unreported 5, and normalized score 100%. The source and accepted-test
integrity flags were both true. Its artifact recorded WSL registration `Ubuntu`, Python 3.10.12,
pytest 8.4.2, and Mutmut 3.7.0; raw Mutmut statistics were saved. The mutation stderr did not
contain `Permission denied`. The definition and plan hashes equal those of the earlier WSL 1
`E:` run, so the previous filesystem copy error did not reproduce under WSL 2. These small runs
validate orchestration, not a comparative research conclusion.

## Validation Performed

- `uv sync --frozen`: passed, 12 packages checked.
- `uv run ruff format --check .`: passed.
- `uv run ruff check .`: passed.
- `uv run pytest`: 315 passed, 6 skipped, 3 deselected after WSL portability maintenance.
- Deterministic 16-cell offline matrix and resume: passed.
- Focused offline mutation/repository/experiment/CLI tests: 125 passed, 3 opt-in skips.
- Real repository WSL mutation integration with `Ubuntu` override: passed; 6/6 scored mutants killed.
- Live four-cell rerun from Windows Temp: mutation verified; one unrelated Coverage LLM timeout recorded.
- Live four-cell rerun from WSL 2 `E:` workspace: all four cells completed PASS; mutation backend COMPLETE, 6/6 scored mutants killed, no copy permission error.

## Dependency Changes and Compatibility Notes

No production or development dependency was added. `uv.lock` is unchanged, SHA-256
`474378DFF8B726C8AF35C97E3CE7518F4240BFD1A28542B76736ACA55B4901B7`. Phase 5D
`RepositoryRunEngine` behavior and standalone modes remain compatible. The historical laptop
registration `Ubuntu-22.04` remains the default; desktop `Ubuntu` is a runtime override. Mutation
selection, scoring, feedback, and Phase 6 metric definitions were not changed.

## Security Boundary, Known Limitations, Risks / Technical Debt

Only benchmark repositories trusted enough to execute on the host. Repeated venvs and subprocess
timeouts do not contain malicious code; external VM isolation remains the operator's choice.
Phase 6 has no environment cache, parallel execution, benchmark cloning, automatic cleanup,
inferential statistics, or semantic oracle scoring. Fresh venvs and mutation workspaces can grow
disk usage. Resume requires the original manifest to remain accessible and unchanged. The
historical WSL 1 `E:` workspace produced a mutation copy permission error; that error did not
reproduce in the verified WSL 2 `E:` run. The separate Windows Temp Coverage-cell Ollama timeout
remains recorded as an unrelated pipeline error.

## Git / Repository State

Implementation is uncommitted on `phase-6-work`; `main`, frozen tags, and Git history were not
changed. Git is the cross-device workflow. Ignored `workspace/` contains local experiment evidence.

## Recommended Next Phase

Phase 7 — Final Hardening, Research Packaging and Release Baseline, after Phase 6 review/freeze.

## Final Phase Status

The framework criteria and required offline demonstrations passed. Live mutation is verified on
this desktop with the `Ubuntu` override from both Windows Temp and a WSL 2 `E:` workspace. The
original name-mismatch failure, historical WSL 1 `E:` permission error, and separate Ollama
timeout remain documented.

PHASE STATUS: COMPLETE
READY TO FREEZE: YES

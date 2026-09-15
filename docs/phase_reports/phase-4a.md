# Phase 4A Report — Mutation Testing Evaluation

## Phase Overview

Phase 4A adds mutation testing as an opt-in evaluation layer after the final passing cumulative
suite. It measures whether accepted tests detect artificial faults and makes zero mutation-guided
LLM calls. Phases 1–3 remain frozen.

## Files Created

- `src/autotest/mutation_runner.py`
- `tests/test_mutation_runner.py`
- `tests/test_mutation_integration.py`
- `tests/fixtures/mutation_suite/classifier.py`
- `tests/fixtures/mutation_suite/accepted_suite.py`
- `tools/mutation/requirements-mutation.txt`
- `tools/mutation/README.md`
- `docs/phase_reports/phase-4a.md`

## Files Modified

- `src/autotest/artifact_store.py`
- `src/autotest/errors.py`
- `src/autotest/main.py`
- `tests/test_artifact_store.py`
- `tests/test_main.py`
- `pyproject.toml` (mutation marker and default deselection only)
- `README.md`

## Architecture Changes

`MutationBackend` is the small engine-neutral boundary. `WSLMutmutBackend` copies the target and
final accepted tests into a fresh per-run workspace, validates the external toolchain, runs a clean
pytest baseline, invokes Mutmut for one selected function, exports supported machine-readable
statistics, verifies protected inputs, and returns `MutationResult`. Mutmut is never imported into
the AutoTest process.

## Behavioral Contracts

- Mutation starts only for a final `PASS` suite and only with `--mutation`.
- Mutation status is separate from `PASS`, `FAIL`, `ERROR`, and `TIMEOUT`.
- Low mutation score does not change a passing exit code.
- Requested mutation infrastructure failure uses existing exit code 2.
- Mutation results never trigger generation, repair, or source modification.
- Only final accepted tests are copied; rejected coverage candidates and failed repairs are absent.

## Configuration and Baseline

The Windows AutoTest baseline remains CPython 3.12 with the frozen uv environment. The overall
mutation timeout defaults to 300 seconds and is configurable with `--mutation-timeout`. Mutation is
disabled by default.

The requested Phase 1 and Phase 1.1 reports were not present in the authoritative repository at
implementation time. Existing Phase 2 and Phase 3 reports and current code/tests were inspected.

## Mutation Environment

The backend targets WSL distribution `Ubuntu-22.04` and virtualenv
`/home/ubuntu/autotest-mutation-env`. It derives and directly invokes absolute `bin/python`,
`bin/pytest`, and `bin/mutmut` paths without activation, shell initialization, or global `PATH`.
The live environment reported Python 3.10.12, pytest 8.4.2, and Mutmut 3.7.0. pytest and Mutmut
semantic versions are parsed from supported CLI output and validated exactly.

## Dependency Changes

Main AutoTest dependencies: **UNCHANGED**. No runtime or development dependency was added and the
main `uv.lock` is unchanged.

External mutation environment: `mutmut==3.7.0` and `pytest==8.4.2`, pinned separately in
`tools/mutation/requirements-mutation.txt`. Effective WSL Python, pytest, Mutmut, and virtualenv
path are recorded for every mutation run.

## Data / Artifact Changes

An enabled run adds `mutation/` with immutable `mutation_result.json`, `stdout.txt`, `stderr.txt`,
the exact `mutmut_raw_stats.json` when the tool exports one, a copied config, and a fresh workspace.
The workspace contains only `<target>.py`, `tests/test_accepted_*.py`, configuration, and
Mutmut's own `mutants/` output. Root `result.json` additively records mutation metrics plus final
line and branch coverage.

Reproducibility metadata includes WSL tool versions and SHA-256 hashes for the original target,
accepted tests, copied inputs, and mutation configuration. Original target/test hashes are checked
after successful, failed, and timed-out mutation execution.

## Mutation Score Definition

AutoTest defines:

```text
killed_mutants / (killed_mutants + survived_mutants) * 100
```

No-tests, skipped, suspicious, timeout, interrupted, and segfault categories are persisted but
excluded. A zero denominator produces `None` / N/A. Mutmut 3.7.0's export omits not-checked and
caught-by-type-check counts; the difference between its `total` and exported category sum is
conservatively stored as `unreported_mutants`. AutoTest does not configure a type checker.

## Tests Added or Modified

Offline tests cover score semantics, normal/all/partial/none/zero results, all supported export
categories, malformed and missing data, unknown fields, function isolation, fresh workspaces,
accepted-only copies, raw preservation, absolute configurable virtualenv executables, exact pytest
and Mutmut version recording/validation, WSL/tool/version unavailability, clean
baseline failure, mutation failure, outer timeout, no applicable mutants, protected source
modification, immutable mutation artifacts, CLI validation, CLI status output, skipped mutation,
low-score exit behavior, and infrastructure exit behavior.

The live fixture has two top-level functions and deterministic accepted tests. The opt-in
`mutation` test does not depend on Ollama, is deselected by the default pytest configuration, and
skips with a clear reason if the pinned WSL toolchain is unavailable when selected explicitly.

## Validation Performed

- Frozen pre-change baseline: 112 passed, 3 skipped.
- `uv sync --frozen`: passed.
- `uv run ruff format --check .`: passed after cleanup.
- `uv run ruff check .`: passed after cleanup.
- Default pytest: 153 passed, 3 Ollama skips, 1 mutation test deselected.
- Offline mutation-focused tests: passed.
- Pinned v3.7.0 CLI/config/export behavior checked against its tagged source.
- Main lockfile content remained unchanged.

## Live Mutation Verification

`uv run pytest -m mutation tests/test_mutation_integration.py -v` passed using the isolated WSL
virtualenv. The final successful test completed in 21.76 seconds and verified clean baseline execution,
applicable mutants, raw export persistence, normalization, score calculation, workspace isolation,
and unchanged source/test hashes.

A real CLI run completed at
`workspace/runs-phase4a-live/20260915T132419Z-580424` with:

- final execution: PASS; 4 tests passed;
- line coverage: 100.0%; branch coverage: 100.0%;
- mutation status: COMPLETE; backend: Mutmut 3.7.0;
- applicable mutants: 10; killed: 10; survived: 0; all other exported categories: 0;
- mutation score: 100.0%; mutation duration: 16.4357 seconds;
- raw and normalized mutation statistics persisted;
- original source and accepted-test hashes matched after execution;
- total LLM generations: 1 initial call, 0 coverage calls, 0 mutation-guided calls.

## Acceptance Criteria

All Phase 4A acceptance criteria are satisfied, including isolation, selected-function
filtering with Mutmut 3.7.0's `<module>.x_<function>__mutmut_*` key format, exact-version
validation, immutable artifacts, source/test integrity, finite timeout,
machine-readable parsing, default offline compatibility, and unchanged execution semantics.

The freeze-only criterion requiring successful live Mutmut and CLI mutation runs is satisfied.

## Known Limitations

- The external Ubuntu virtualenv must remain separately provisioned at the configured path.
- Mutmut 3.7.0 does not expose not-checked and caught-by-type-check separately in its CI/CD export.
- Equivalent or irrelevant surviving mutants are not classified.
- One standalone file and one top-level function remain the supported target scope.

## Risks / Technical Debt

Mutmut CLI/config behavior is version-sensitive, which is why execution is pinned and validated.
Terminating `wsl.exe` is the outer timeout boundary; WSL process cleanup should be observed during
live validation. Generated tests remain untrusted, and subprocess/WSL separation is not a complete
security sandbox.

## Compatibility Notes

Without `--mutation`, the Phase 3 CLI flow and stable exit mapping are unchanged. `pyproject.toml`
adds the `mutation` marker and deselects it in default pytest runs. Mutmut is not added to the main
environment or imported by production code. Phase 4B has not been started.

## Git / Repository State

The Phase 4A implementation is intentionally left as working-tree changes for review. No commit,
tag, or freeze operation was performed.

## Recommended Next Phase

Freeze Phase 4A after review. Phase 4B remains separate future scope and has not been started.

## Final Phase Status

**PHASE STATUS: COMPLETE**

**READY TO FREEZE: YES**

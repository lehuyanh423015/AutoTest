# Phase 3 Report — Coverage-Guided Test Generation

## Phase Overview

Phase 3 extends the frozen Phase 1–2 pipeline with coverage-guided supplementary test generation.
Coverage begins only after Phase 2 has produced a passing suite. AutoTest measures a baseline,
provides target-function missing lines and branch arcs to the existing provider, validates each new
candidate with the existing bounded repair policy, and accepts only non-regressing coverage
improvements. Target and generated modules remain outside the AutoTest interpreter process.

## Files Created

- `src/autotest/coverage_runner.py`
- `src/autotest/coverage_engine.py`
- `tests/test_coverage_runner.py`
- `tests/test_coverage_prompt.py`
- `tests/test_coverage_engine.py`
- `tests/fixtures/sample_project/branching.py`
- `tests/fixtures/coverage_suite/baseline_classify.py`
- `docs/phase_reports/phase-3.md`

## Files Modified

- `src/autotest/__init__.py`
- `src/autotest/artifact_store.py`
- `src/autotest/errors.py`
- `src/autotest/main.py`
- `src/autotest/prompt_builder.py`
- `src/autotest/repair_engine.py`
- `src/autotest/test_runner.py`
- `tests/test_main.py`
- `tests/test_ollama_integration.py`
- `tests/test_test_runner.py`
- `README.md`

## Architecture Changes

- `CoverageRunner` uses `sys.executable -m coverage run --branch -m pytest`, then coverage.py's
  JSON command. It owns subprocess-local environment settings, report parsing, output capture, and
  removal of the temporary coverage data file.
- Immutable `CoverageResult` normalizes executable/executed/missing lines, executed/missing arcs,
  percentages, raw-report location, and measurement duration for one selected function.
- `PromptBuilder.build_coverage()` produces deterministic line-numbered feedback with a bounded
  accepted-test context and explicit additional-tests-only/oracle constraints.
- `TestRunner.run_suite()` adds cumulative multi-file execution while `run()` remains compatible.
- `RepairEngine.run()` accepts optional immutable prior test files. When present, every candidate
  and repair is validated as the prior suite plus only the current mutable candidate.
- `CoverageEngine` owns an iterative, bounded proposal loop and transactional acceptance.
- `ArtifactStore` adds coverage baseline/round APIs and coverage research metadata without
  removing the frozen Phase 2 APIs or attempt layout.

## Behavioral Contracts

- Coverage runs only when the Phase 2 final execution status is `PASS`.
- Round 0 is always a measurement-only baseline. The default maximum of three permits at most
  three subsequent LLM coverage-generation calls; zero still measures baseline and makes none.
- Accepted tests are never rewritten or deleted. Each round creates a separate module, and all
  accepted modules execute cumulatively.
- Syntactically invalid candidates can use Phase 2 repair. Valid candidates with zero pytest test
  functions are rejected before execution. Test/assert counts and quality warnings are retained.
- A passing candidate is accepted only when at least one meaningful target metric improves and no
  previously executed target line or arc regresses.
- Non-improvement and regression preserve evidence, reject the candidate, preserve the prior
  passing suite, and stop safely.
- Target achievement requires line coverage at or above the configured target and applicable
  branch coverage at or above it. Branch N/A satisfies the branch condition.
- Stop reasons are `TARGET_REACHED`, `MAX_ROUNDS_REACHED`, `NO_COVERAGE_IMPROVEMENT`,
  `COVERAGE_REGRESSION`, `CANDIDATE_NOT_ACCEPTED`, `COVERAGE_ERROR`, `LLM_ERROR`, and
  `PIPELINE_ERROR`.
- Frozen statuses remain `PASS`, `FAIL`, `ERROR`, and `TIMEOUT`; CLI exits remain 0, 1, 2, and 3.
  A missed coverage target does not turn a passing test suite into failure or add an exit code.

## Configuration and Baseline

- Python: CPython 3.12.10
- Provider/model: Ollama with `qwen2.5-coder:14b`
- Coverage: coverage.py 7.16.0 with branch measurement enabled
- Default coverage target: 100.0%
- Default maximum coverage rounds after baseline: 3
- Default maximum candidate repairs per coverage round: inherited `max_repair_attempts=3`
- Coverage/test subprocess timeout: 30 seconds per subprocess command
- Accepted-test prompt context bound: 12,000 characters
- Pre-change frozen regression: 78 passed, 2 opt-in tests deselected; Ruff clean

## Dependency Changes

None. Phase 3 uses the existing coverage.py 7.16.0, pytest 8.4.2, pytest-cov 7.1.0,
pytest-timeout 2.4.0, and Python standard library. No version changed and `uv.lock` was not
modified.

## Data / Artifact Changes

The Phase 2 root attempts remain unchanged. Phase 3 adds:

```text
workspace/runs/<run-id>/
├── result.json
├── attempt-000/
│   └── ...
└── coverage/
    ├── baseline/
    │   ├── coverage_raw.json
    │   ├── coverage_result.json
    │   ├── stdout.txt
    │   └── stderr.txt
    └── round-001/
        ├── prompt.txt
        ├── raw_response.txt
        ├── generated_test.py
        ├── coverage_raw.json
        ├── coverage_result.json
        ├── stdout.txt
        ├── stderr.txt
        ├── result.json
        └── candidate/
            ├── attempt-000/
            └── attempt-001/ ...
```

Raw coverage.py JSON remains separate and exact. Normalized reports contain function-only data.
Round metadata records before/after coverage, deltas, acceptance, stop information, execution
history, test/assert counts, quality warnings, paths, and hashes. Root metadata preserves Phase 2
fields and adds initial/final coverage, accepted history/files, gains, bounds, stop state, LLM call
counts, repair counts, generation/execution timing, and all coverage measurement time. Artifact
writes are UTF-8 and exclusive. The subprocess-only `.coverage-autotest` database is removed.

## Tests Added or Modified

Deterministic offline coverage includes:

- real line/missing-line and branch/missing-arc extraction;
- negative arc destinations, target-range filtering, and an uncovered second function;
- no-branch functions, malformed JSON, command failure, timeout, UTF-8, spaces, and exclusive
  coverage artifacts;
- deterministic, bounded coverage prompts with actual line numbers and oracle constraints;
- 50→75→100 acceptance, baseline 100 short-circuiting, zero-round mode, and two-round bounds;
- cumulative suite preservation, candidate failure→repair→coverage, repair exhaustion, no
  improvement, regression rollback, and zero-test rejection;
- coverage subtree/root research metadata and call/timing counts;
- CLI defaults, validation, reporting, and frozen status/exit behavior;
- an opt-in live Ollama coverage round with an intentionally incomplete deterministic baseline.

## Validation Performed

On 2026-09-15:

- `uv sync --frozen`: PASS — 12 locked packages checked in 1 ms.
- `uv run ruff format --check .`: PASS — 37 files already formatted.
- `uv run ruff check .`: PASS.
- `uv run pytest`: PASS — 112 passed, 3 opt-in Ollama tests skipped in 8.09 seconds.
- `$env:AUTOTEST_RUN_OLLAMA='1'; uv run pytest -m ollama tests/test_ollama_integration.py -v`:
  PASS — 3 passed in 134.22 seconds.

The default suite is offline and uses scripted providers for coverage-loop decisions.

## End-to-End Verification

Command:

```powershell
uv run python -m autotest.main `
    --file tests/fixtures/sample_project/branching.py `
    --function classify_number `
    --max-repair-attempts 3 `
    --max-coverage-rounds 3 `
    --coverage-target 100
```

Observed result:

- Run ID: `20260915T093618Z-30a2ef`
- Initial/final execution: PASS → PASS; CLI exit 0
- Generated tests: 4 passed in 0.02 seconds
- Phase 2 repairs: 0
- Baseline line coverage: 100.00%
- Baseline branch coverage: 100.00%
- Final line/branch coverage: 100.00% / 100.00%
- Coverage rounds used/accepted: 0 / 0
- Coverage stop: `TARGET_REACHED`; no redundant coverage LLM call
- Coverage.py identified executable lines 4, 6, 7, 8, 9, and 10 and all four arcs
- Required Phase 2 and coverage baseline files were present; no `.coverage-autotest`, cache, or
  bytecode artifact remained

The separate live coverage integration began with a deterministic partial suite, sent one actual
coverage prompt to Ollama, persisted the real response and candidate, executed the cumulative
suite through Phase 2 policy, and re-measured/preserved the outcome. It intentionally does not
require probabilistic model output to reach 100%.

## Acceptance Criteria

All applicable Phase 3 acceptance criteria were verified. Frozen tests and public execution
contracts pass; dependency and lock inputs are unchanged; coverage is subprocess-isolated and
branch-enabled; raw and normalized reports are preserved; guidance uses function-only missing
lines/arcs; baseline/zero-round/target short-circuit behavior is deterministic; candidates are
separate, cumulative, repairable, and transactional; regressions and non-improvement stop safely;
research metrics and stop state persist; CLI target failure remains distinct from test failure;
both loops are bounded; documentation and the phase report are present.

## Known Limitations

- Coverage demonstrates execution, not oracle correctness or fault-detection ability.
- Scope remains one standalone Python file and one top-level function, not methods, packages, or
  repository-scale dependency/context analysis.
- Branch arcs are reported structurally; AutoTest does not label semantic true/false paths.
- Accepted-test prompt context uses a deterministic character bound rather than token-aware or
  repository-scale selection.
- Generated tests execute in subprocesses with timeouts, but this is not a security sandbox.
- Process-tree containment, mutation testing, source repair, and semantic oracle validation remain
  outside Phase 3.

## Risks / Technical Debt

- A weak or incorrect assertion can pass and increase coverage; structural warnings cannot prove
  oracle quality.
- Coverage measurement executes untrusted generated code and relies on OS subprocess boundaries.
- A provider failure can leave an intentionally partial round containing its saved prompt and
  stop metadata but no fabricated response.
- Rare test side effects may cause genuine coverage regression; the conservative policy rejects
  the candidate rather than trying to infer a cause automatically.

## Compatibility Notes

- Phase 1/2 analyzer, provider, `TestRunner.run()`, four statuses, repair defaults, and exit codes
  remain available.
- `TestRunner.run_suite()` is additive. `RepairEngine.run()` gained an optional prior-file sequence
  whose empty default preserves Phase 2 behavior.
- Phase 2 artifact APIs and attempt paths remain available. Coverage metadata is an additive root
  section and coverage files are isolated beneath `coverage/`.
- Existing CLI arguments remain valid. Phase 3 defaults to coverage baseline plus at most three
  feedback rounds; use `--max-coverage-rounds 0` for the Phase 2 execution-feedback ablation.

## Git / Repository State

No commit, branch, or tag was created. Phase 3 changes remain in the shared worktree. Runtime
evidence is under the ignored `workspace/runs/` tree. No dependency manifest or lockfile changed
during Phase 3.

## Recommended Next Phase

Review and freeze the function-range semantics, branch-arc evidence, transactional acceptance,
root metrics, deterministic scenarios, and live artifacts. Do not begin Phase 4 until the Phase 3
freeze is explicitly accepted.

## Final Phase Status

PHASE STATUS: COMPLETE
READY TO FREEZE: YES

# Phase 2 Report — Execution Feedback and Automatic Test Repair

## Phase Overview

Phase 2 extends the frozen Phase 1.1 direct-generation pipeline with a provider-neutral, bounded
execution-feedback loop. AutoTest can now classify non-passing pytest outcomes, build constrained
repair prompts, generate replacement test code, execute it, and repeat within a fixed limit.
Target source is never imported into AutoTest and is never modified.

## Files Created

- `src/autotest/failure_analyzer.py`
- `src/autotest/repair_engine.py`
- `src/autotest/test_quality.py`
- `tests/test_failure_analyzer.py`
- `tests/test_repair_engine.py`
- `tests/test_test_quality.py`
- `docs/phase_reports/phase-2.md`

## Files Modified

- `src/autotest/__init__.py`
- `src/autotest/artifact_store.py`
- `src/autotest/main.py`
- `src/autotest/prompt_builder.py`
- `src/autotest/test_generator.py`
- `tests/test_artifact_store.py`
- `tests/test_main.py`
- `tests/test_ollama_integration.py`
- `tests/test_prompt_builder.py`
- `README.md`

## Architecture Changes

- `FailureAnalyzer` maps raw `TestRunResult` evidence to a small failure category and bounded
  feedback. Oversized feedback retains the traceback tail; raw stdout/stderr remain unchanged.
- `PromptBuilder.build_repair()` adds status-aware repair instructions without changing the frozen
  initial-generation prompt API.
- `RepairEngine` owns a straightforward iterative loop and uses the existing `LLMProvider` and
  subprocess runner contracts.
- `TestAttempt` and `RepairSessionResult` expose attempt history and aggregate outcomes.
- Lightweight AST metrics count test functions and assertions. Oracle checks emit diagnostic
  warnings for obvious tautologies or assertion removal; they do not rewrite code.
- `ArtifactStore` retains its Phase 1 methods and adds immutable attempt/session persistence.

## Behavioral Contracts

- Attempt 0 is `INITIAL`; attempts 1 and later are `REPAIR`.
- `max_repair_attempts=3` permits at most four executions. Zero permits exactly one execution.
- PASS stops immediately. ERROR, TIMEOUT, and conservatively handled FAIL are repair-eligible.
- Stop reasons are `PASS_REACHED`, `MAX_REPAIRS_REACHED`, `LLM_ERROR`, and `PIPELINE_ERROR`.
- Repair success is true only when initial status is not PASS and final status is PASS.
- CLI exit codes remain 0 PASS, 1 FAIL, 2 ERROR/pipeline failure, and 3 TIMEOUT.
- Invalid repaired Python executes as ERROR; it is not repaired inside the AutoTest process.
- A passing repaired test suite does not prove oracle correctness or absence of target defects. It
  means only that the suite is executable and passing against the current implementation.

## Configuration and Baseline

- Python: CPython 3.12.10
- Provider: local Ollama REST API through `urllib.request`
- Model: `qwen2.5-coder:14b`
- Ollama URL: `http://localhost:11434`
- Temperature: `0.0`
- HTTP timeout: 120 seconds
- Test subprocess timeout: 30 seconds
- Maximum repair attempts: 3
- Maximum prompt feedback: 12,000 characters, retaining the output tail

Low temperature reduces experimental variation but does not guarantee bit-for-bit model output.

## Dependency Changes

None. No runtime or development dependency was added or upgraded, and `uv.lock` was not changed
for Phase 2.

## Data / Artifact Changes

Phase 2 CLI runs use this schema:

```text
workspace/runs/<run-id>/
├── result.json
├── attempt-000/
│   ├── prompt.txt
│   ├── raw_response.txt
│   ├── generated_test.py
│   ├── stdout.txt
│   ├── stderr.txt
│   └── result.json
└── attempt-001/
    └── ...
```

Each attempt records status, exit code, execution and generation timing, timeout, failure category,
test/assert counts, quality warnings, and prompt/test hashes. Root metadata records initial/final
status, attempt count/history, repair count/success, stop reason, aggregate timing, LLM settings,
and source hash. Exact prompts and raw model responses are stored before sanitization. Writes use
UTF-8 exclusive creation. Pytest caches and bytecode are disabled in the subprocess.

The root schema preserves Phase 1 status, exit code, duration, timeout, generation-path, and source
hash fields. Generation paths now point into `attempt-000/`; this is the documented schema
evolution required to preserve all attempts.

## Tests Added or Modified

Deterministic tests cover:

- syntax, import, collection, assertion, timeout, and generic failure classification;
- feedback truncation, including very small limits;
- repair prompt evidence and ERROR/TIMEOUT/FAIL-specific constraints;
- ERROR→PASS, ERROR→ERROR→PASS, FAIL→PASS, and TIMEOUT→PASS;
- bounded exhaustion, PASS short-circuiting, zero-repair mode, and provider failure;
- invalid repaired Python through a real pytest subprocess;
- assertion/test metrics and obvious oracle-degradation warnings;
- attempt numbering, immutability, UTF-8, hashes, raw history, and root metadata;
- all final CLI status mappings, negative CLI values, and repair success;
- opt-in direct Ollama generation and an intentional live repair attempt.

## Validation Performed

On 2026-09-15:

- `uv sync --frozen`: PASS — 12 locked packages checked.
- `uv run ruff format --check .`: PASS — 29 files already formatted.
- `uv run ruff check .`: PASS.
- `uv run pytest`: PASS — 78 passed, 2 opt-in Ollama tests skipped in 3.37 seconds.
- `uv run pytest -m ollama tests/test_ollama_integration.py -v`: PASS — 2 passed in
  94.30 seconds.

## End-to-End Verification

Command:

```powershell
uv run python -m autotest.main `
    --file tests/fixtures/sample_project/calculator.py `
    --function divide `
    --max-repair-attempts 3
```

Verified result:

- Run ID: `20260915T085233Z-87f2ef`
- Initial/final history: PASS → PASS
- Repairs used: 0
- Stop reason: `PASS_REACHED`
- CLI exit: 0
- Generated suite: 5 passed in 0.02 seconds
- Final pytest subprocess duration: 0.379 seconds
- Attempt metrics: 5 test functions, 7 assertions
- Required root, prompt, raw-response, generated-test, stdout/stderr, and attempt-result artifacts:
  present
- Artifact cache directories: 0
- Source, prompt, and generated-test hashes: independently matched

The live repair integration separately supplied an intentional bad import. It verified ERROR on
attempt 0, a real repair prompt/response, immutable attempt 1 artifacts, execution of the returned
test, and root result persistence. It deliberately did not require probabilistic output to PASS.

## Acceptance Criteria

All applicable Phase 2 acceptance criteria were verified. Phase 1 regression behavior remains
covered, direct generation works, no-repair mode executes once, all non-passing statuses are
eligible under documented guidance, PASS does not repair, limits are validated and bounded,
attempt/root artifacts are preserved, final CLI semantics remain frozen, and default tests require
no Ollama.

## Known Limitations

- FAIL repair cannot solve the general test-oracle problem.
- Structural counts and oracle warnings are diagnostics, not semantic quality guarantees.
- The prototype still handles one standalone file and one top-level function.
- Arbitrary repository dependencies, packages, methods, and cross-file context are unsupported.
- There is no coverage guidance, mutation testing, source repair, or environment automation.
- Subprocess execution is not a security sandbox.

## Risks / Technical Debt

- A model can produce a passing but weak or incorrect oracle despite explicit prompt constraints.
- Failure categories use intentionally simple string heuristics rather than traceback parsing.
- A provider failure after an attempt prompt is saved leaves an intentionally incomplete attempt
  directory containing the exact prompt but no fabricated response or execution result.
- Process-tree containment and container isolation remain future safety work.

## Compatibility Notes

- `LLMProvider.generate(prompt) -> str`, `TestRunner`, `TestStatus`, and analyzer contracts remain
  compatible.
- `PromptBuilder.build()` and direct `TestGenerator.generate()` remain available.
- `GeneratedTest` gained an optional generation-duration field with a backward-compatible default.
- Existing ArtifactStore Phase 1 APIs remain available.
- Existing CLI commands remain valid; they now default to bounded repair. Set
  `--max-repair-attempts 0` for frozen Phase 1 execution behavior.

## Git / Repository State

No commit, branch, or tag was created. The worktree contains the Phase 1/1.1 implementation and
Phase 2 changes as uncommitted modifications/new files. Runtime artifacts under `workspace/` are
ignored by Git.

## Recommended Next Phase

Review the attempt schemas, conservative FAIL policy, and live repair evidence, then freeze Phase 2
before planning any coverage or mutation-related phase. Do not treat PASS rate alone as test-quality
evidence.

## Final Phase Status

PHASE STATUS: COMPLETE
READY TO FREEZE: YES

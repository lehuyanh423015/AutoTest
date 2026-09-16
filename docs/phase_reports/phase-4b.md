# Phase 4B Report — Mutation-Guided Test Generation

## Phase Overview

Phase 4B adds separately opt-in, bounded generation of supplementary tests from surviving Mutmut
evidence. The Phase 4A evaluation is preserved as immutable Round 0. Candidates must pass the
original source, may reuse frozen Phase 2 repair, must not regress target coverage, and enter the
cumulative suite only after a fresh stable-universe mutation run kills a prior survivor without
regression.

## Files Created

- `src/autotest/mutation_feedback.py`
- `tests/test_mutation_feedback.py`
- `tests/test_mutation_prompt.py`
- `tests/test_mutation_feedback_integration.py`
- `tests/fixtures/mutation_feedback_suite/boundary.py`
- `tests/fixtures/mutation_feedback_suite/baseline_suite.py`
- `tests/fixtures/mutation_feedback_suite/fee.py`
- `docs/phase_reports/phase-4b.md`

## Files Modified

- `src/autotest/mutation_runner.py`
- `src/autotest/prompt_builder.py`
- `src/autotest/artifact_store.py`
- `src/autotest/main.py`
- `tests/test_mutation_runner.py`
- `tests/test_main.py`
- `README.md`

## Architecture Changes

`MutationFeedbackEngine` composes the frozen provider, `RepairEngine`, `CoverageRunner`, and the
Phase 4A mutation backend. `MutationFeedbackBackend` is a structural protocol adding supported
survivor extraction to the existing mutation execution boundary. `MutationFeedbackSessionResult`
and `MutationFeedbackRoundResult` expose baseline/final data, round decisions, accepted files,
coverage state, generation/repair counts, and explicit stop reasons.

## Behavioral Contracts

- `--mutation` remains Phase 4A evaluation only.
- `--mutation-feedback` implies mutation evaluation and enables Phase 4B.
- Mutation feedback starts only after a final passing suite and successful baseline mutation.
- A zero-survivor baseline makes zero mutation-feedback LLM calls.
- Round 0 is preserved; no candidate can overwrite prior accepted tests or source.
- A candidate needs mutation improvement, not coverage improvement; coverage must not regress.
- Research outcomes such as maximum rounds and non-improvement preserve PASS exit code 0.
- Mutation, LLM, or pipeline infrastructure failures continue to use exit code 2.

## Configuration and Baseline

`max_mutation_rounds` defaults to 3 and accepts zero for Phase 4A-equivalent baseline-only mode.
`max_mutants_per_round` defaults to 5 and must be at least one. Prompt context is bounded to
12,000 characters. Existing `--mutation-timeout` and `--mutation-venv` settings are reused.

The frozen external environment remains Ubuntu-22.04 with Python 3.10.12, pytest 8.4.2, Mutmut
3.7.0, and `/home/ubuntu/autotest-mutation-env`.

## Dependency Changes

No runtime or development dependency changed. `pyproject.toml` and the main `uv.lock` were not
modified by Phase 4B. The lockfile SHA-256 remains
`474378DFF8B726C8AF35C97E3CE7518F4240BFD1A28542B76736ACA55B4901B7`.

## Survivor Extraction Strategy

Pinned Mutmut 3.7.0 was inspected directly. AutoTest invokes the absolute virtualenv executable
with `results --all true`, parses target-scoped full IDs and supported statuses, sorts by ID, and
selects the first N survivors. It invokes `show <mutant-id>` for each selected survivor. It does
not use `browse`, `apply`, a private Python API, or the internal database. The full results stream
and each exact selected diff are preserved.

## Mutation Feedback Prompt

The deterministic prompt includes round index, original module/function, line-numbered original
source, bounded immutable accepted tests, selected stable IDs, and exact diffs. It requests only a
supplementary pytest module and prohibits source changes, mutant-derived oracles, observed-output
oracles, tautologies, network/services, subprocesses, installation, and sleep. It explicitly warns
that equivalent mutants may not admit a defensible distinguishing test.

## Candidate Acceptance Policy

The cumulative accepted suite plus candidate first runs against the original implementation.
Frozen Phase 2 repair can change only that candidate. Zero-test candidates and exhausted repairs
are rejected. Coverage is remeasured, but a gain is not required. A fresh isolated mutation run is
then compared. Acceptance requires a stable universe, at least one former survivor now killed, no
former killed mutant now surviving, no mutation-score decrease, and no coverage regression.

## Mutation Universe Validation

Full sorted mutant-ID tuples are compared before and after. Source path/hash, selected function,
mutation config hash, and exact backend version must also match. Unexpected change stops with
`MUTANT_SET_CHANGED`; no score comparison is silently accepted.

## Data / Artifact Changes

The existing `mutation/` subtree remains the Round 0 record. New `mutation-feedback/baseline/`
stores raw results and selected exact diffs. Every `round-NNN/` stores input survivor evidence,
prompt, raw response, sanitized candidate, nested Phase 2 attempt history, coverage verification,
fresh mutation workspace/results, post-run survivor evidence, and the acceptance decision.

Root `result.json` adds `mutation_feedback` without replacing `mutation`. It includes initial/final
scores and counts, gain, survivor reduction, round history, accepted files, feedback/repair calls,
generation/execution/mutation timing, and final line/branch coverage.

## Tests Added or Modified

Offline tests cover realistic Mutmut result parsing, malformed/unknown output, target scoping,
deterministic ordering/limiting, exact diff commands, safe filenames, prompt content/bounds,
all-killed and zero-round short circuits, passing improvement, non-improvement, mutation and
coverage regression, mutant-set change, exhausted repair, two-round cumulative suites, LLM
failure, CLI validation/implied mutation, separate root metadata, and artifact persistence.

The default suite uses only fakes for mutation feedback and does not require WSL, Mutmut, Ollama,
network access, or internet access. Real tests retain the existing `mutation` marker.

## Validation Performed

- Pre-change frozen regression: 153 passed, 3 skipped, 1 mutation test deselected.
- `uv sync --frozen`: passed.
- `uv run ruff format --check .`: passed.
- `uv run ruff check .`: passed.
- Default pytest: 171 passed, 3 Ollama skips, 3 mutation tests deselected.
- Focused offline Phase 4A/4B and CLI tests: 68 passed.
- Main lockfile hash remained unchanged.

## Real Mutmut Verification

The deterministic real-Mutmut integration passed in 26.23 seconds. Its incomplete baseline had 6
applicable mutants: 4 killed, 2 survived, score 66.67%. Supported CLI extraction identified both
survivors. The scripted provider supplied the meaningful inclusive-boundary test. It passed with
unchanged 100% line and branch coverage, killed both former survivors, and was accepted. The fresh
result was 6 killed, 0 survived, score 100%; gain 33.33 percentage points.

Source and accepted-test hashes remained unchanged, both mutation workspaces were fresh, full IDs
were stable, and raw plus normalized evidence was persisted.

## Live Ollama Verification

The separately opt-in real Ollama plus real Mutmut integration passed in 80.63 seconds using
`qwen2.5-coder:14b`. The same real 4-killed/2-survived baseline produced one mutation-feedback LLM
call. The real response created one passing boundary test, killed both survivors, retained 100%
line/branch coverage, and was transactionally accepted at 6 killed/0 survived and 100% score.

## End-to-End Demonstration

The actual CLI Phase 4B run is preserved at
`workspace/runs-phase4b-live/20260915T141359Z-634873`. It used the arithmetic-boundary fixture and
completed with execution PASS and exit code 0. Baseline mutation was 23 applicable, 19 killed, 4
survived, score 82.61%. One real mutation-feedback call produced a candidate that required one
Phase 2 repair and then passed the original suite. Fresh mutation remained 19 killed/4 survived,
82.61%, so the candidate was correctly rejected with `NO_MUTATION_IMPROVEMENT`. Final gain was
0.00 percentage points and survivors reduced was zero. This is a valid research outcome and is not
misreported as improvement.

A separate actual CLI run against the simpler boundary fixture observed 6 killed/0 survived and
correctly stopped at `ALL_MUTANTS_KILLED` with zero feedback calls.

## Acceptance Criteria

All applicable Phase 4B criteria are satisfied: frozen regressions remain green, baseline-only
and evaluation-only modes remain reproducible, survivor evidence uses supported pinned commands,
feedback is bounded and additional-only, candidate repair and coverage verification are reused,
full-universe transactional acceptance is enforced, artifacts/metrics are complete, real Mutmut
improvement is deterministic, real Ollama integration ran, and an actual CLI feedback round was
observed honestly.

## Known Limitations

- Equivalent mutants are warned about but not classified automatically.
- Character bounds are deterministic approximations rather than token counting.
- Only one standalone file and one top-level function are supported.
- The external WSL environment must already be provisioned at the configured absolute path.
- Real LLM candidates can pass yet fail to improve mutation score and are then rejected.

## Risks / Technical Debt

Mutmut output/config behavior is version-sensitive and therefore pinned exactly. Generated tests
remain untrusted; subprocess and WSL isolation are not a security sandbox. Mutation runs are
expensive and intentionally use one child for reproducibility. Automatic equivalent-mutant
analysis remains outside scope.

## Compatibility Notes

Phases 1–4A contracts and status mappings remain unchanged. Without `--mutation-feedback`, no new
generation occurs. No new marker, dependency, activation script, global executable, or global PATH
assumption was introduced.

## Git / Repository State

Phase 4B is left as working-tree changes for review. No commit, tag, or freeze operation was
performed. Live run artifacts remain under ignored `workspace/` paths.

## Recommended Next Phase

Freeze and review Phase 4B. Do not begin the next repository-scale phase until that review is
complete.

## Final Phase Status

**PHASE STATUS: COMPLETE**

**READY TO FREEZE: YES**

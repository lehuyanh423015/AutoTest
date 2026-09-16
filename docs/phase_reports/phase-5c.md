# Phase 5C Report — Cross-File Dependency Analysis and Context Selection

## Phase Overview

Phase 5C adds a static, target-centered `ProjectProfile + ContextTarget → ContextBundle` path. It selects a small, defensible set of exact local source declarations for one top-level function. It does not generate tests, execute project code, use a prepared environment, or call an LLM. The frozen Phase 5B tag is `phase-5b-frozen` at `3323058`.

## Files Created

- `src/autotest/context_selector.py`
- `tests/test_context_selector.py`
- `tests/fixtures/projects/context_project/` (pyproject and six Python files)
- `docs/phase_reports/phase-5c.md`

## Files Modified

- `src/autotest/main.py`: additive, exclusive static CLI mode and fresh context artifacts.
- `src/autotest/__init__.py`: additive public context API exports.
- `README.md`: Phase 5C use, scope, evidence, and limits.
- `docs/PROJECT_STATE.md`: corrected frozen milestone, current validation, and Phase 5C handoff.

## Architecture Changes

`ProjectInspector` remains the sole repository layout discoverer. `ContextSelector` consumes its production-module paths, module names, and test flags, then lazily parses relevant bounded source files once per selection. The selector owns target validation, top-level symbol and module-import indexes, target-centered dependency expansion, ranking, budgets, evidence, hashes, JSON, and text rendering. It does not change `ProjectAnalyzer`, `PromptBuilder`, `EnvironmentPlanner`, or `EnvironmentProvisioner`.

## Behavioral Contracts

The target is one project-relative `.py` path and top-level sync/async function. Invalid, missing, non-production, nested, method, ambiguous, malformed, or oversized mandatory targets fail with CLI exit 2. A valid bundle may be `PARTIAL` and still exits 0. Existing generation, inspection, planning, and preparation modes and their artifact/status contracts are unchanged. Selection makes zero LLM calls, target subprocesses, installations, target imports, and test executions.

## ContextTarget Schema

`ContextTarget(file: Path, function: str)` uses `src/shop/pricing.py:calculate_total` syntax. The serialized file is project-relative and slash-separated. Absolute paths, parent traversal, non-Python suffixes, and invalid names are rejected. Duplicate top-level declarations with the selected name are rejected rather than chosen arbitrarily.

## ContextSelectionPolicy

The immutable defaults are `max_chars=32000`, `max_files=8`, `max_items=24`, and `max_dependency_depth=2`. Depth 0 is the target, depth 1 its direct dependencies, and depth 2 supporting dependencies. Positive character/file/item limits and nonnegative depth are enforced.

## Symbol Index

Only `ProjectProfile.modules` marked production are eligible. Module-name collisions remain ambiguous. Top-level functions, async functions, classes, assignments, and annotated assignments are indexed without evaluation. Methods and nested functions are not indexed. Source comes from original UTF-8 lines and AST line metadata, including decorators and in-range comments. The target file and all selected files have SHA-256 evidence.

## Import Resolution

Module-scope direct imports and imports nested in module-level `if`/`try` branches are discovered syntactically. Direct `from` imports, aliases, `import package.module`, module aliases, `from . import module`, and relative `from ..module import symbol` use the profiled module-name index, not Python import machinery. Relevant import statements are separate source items. Ambiguous modules, missing local declarations, star imports, and package re-export uncertainty are not guessed.

## Dependency Resolution

References are collected from function bodies, defaults, decorators, return/argument annotations, and selected supporting declarations. Parameters, common local bindings, and builtins are filtered conservatively. Same-module globals and unambiguous imported local symbols/module attributes become declaration items and edges. Each selected support item can expand references through the configured depth. Stable declaration identities prevent duplicate selection and stop cycles. This is not a whole-program call graph or full scope/type analysis.

## ContextBundle Schema

The immutable result records schema version 1 in JSON, profile and target hashes, target/policy, exact `ContextItem` source and hashes, `DependencyEdge` reasons, external/unresolved references, budget/depth omissions, warnings, selected file hashes, selected file/item/character metrics, direct and recursive dependency counts, depth reached, completeness status, and `bundle_sha256`. `ContextItem` paths are relative, with kind, symbol, line range, depth, reason, and untruncated source. The bundle is evidence, not a provider prompt.

## Selection Priority

The target is mandatory and first. A deterministic priority queue orders relevant imports before declarations at the same depth, then same-module symbols, explicit imported symbols, and imported-module attributes; deeper declarations follow direct ones. Equal-priority candidates use project-relative path, start line, and symbol. Cycles and repeated references never duplicate an item.

## Context Budgeting

The target is never truncated; if it exceeds `max_chars`, selection fails with `TARGET_EXCEEDS_CONTEXT_BUDGET`. Optional declarations/imports are admitted whole or omitted with `CHARACTER_BUDGET`, `FILE_BUDGET`, `ITEM_BUDGET`, or `DEPTH_LIMIT`. The file budget counts unique source files including the target. Lower-priority candidates continue after an omission. No token estimator or semantic score was added.

## External / Unresolved References

Standard-library imports are labeled `STDLIB`; other nonlocal imports are `THIRD_PARTY_OR_UNKNOWN`. Their source is not indexed. Star imports, unbound globals, missing/ambiguous local symbols and modules, unreadable support modules, and dynamic lookups remain explicit uncertainty. A malformed support module adds a warning and unresolved record while allowing a valid target bundle.

## Serialization and Hashing

JSON is UTF-8, indented, deterministic, and contains no absolute machine root, timestamp, venv, or interpreter path. The hash covers normalized content except itself. The separate `selection_metrics.json` records elapsed selection seconds without destabilizing bundle identity. `context.txt` renders target, relevant imports, supporting declarations, external/unresolved evidence, and omissions deterministically; it contains no LLM instructions. A changed selected declaration changes the hash; an unchanged profile and changed unselected-file content that does not alter structural profile evidence leaves it unchanged. File hashes deliberately cover full selected files, so unrelated edits *within* a selected file also change bundle identity.

## CLI Changes

`--select-context <project> --context-target <relative.py>:<function>` is mutually exclusive with generation, inspection, planning, and preparation. Optional `--context-output-root`, `--context-max-chars`, `--context-max-files`, `--context-max-items`, and `--context-max-depth` control output and policy. The output root cannot be inside the selected project. Each successful run reserves a fresh directory by exclusive creation under `workspace/context_bundles` by default, containing `context_bundle.json`, `context.txt`, and `selection_metrics.json`. Success or partial selection exits 0; target and infrastructure errors exit 2. No PASS/FAIL/TIMEOUT test status is reused.

## Configuration and Baseline

Starting Git state: clean `main`, HEAD `3323058360611cc740343f931373c05ffa2621c4` equal to `phase-5b-frozen`. Frozen baseline was 236 passed, 3 skipped, 3 deselected; both Ruff checks passed. CPython remains 3.12.10. The Phase 5B report's claim that Phase 5B was untagged was historical and is superseded by Git and this report.

## Dependency Changes

None. Standard-library modules only. `pyproject.toml` is unchanged. `uv.lock` remains unchanged at SHA-256 `474378DFF8B726C8AF35C97E3CE7518F4240BFD1A28542B76736ACA55B4901B7`.

## Data / Artifact Changes

Only new ignored `workspace/context_bundles/<fresh-id>/` evidence is written. The target fixture/repository and frozen run, profile, plan, and provision artifact schemas are untouched. The bundle is portable between unchanged repository roots and copied source roots.

## Tests Added or Modified

Thirty-three new offline tests cover target syntax/validation, duplicate/malformed targets, no-execution fixture, exact source and excluded unused symbols, depth 0/1/2, all budgets and no partial declarations, hashes/portability/rendering, async functions, aliases, unaliased and package-module imports, relative and conditional imports, annotated assignments, decorators/defaults/annotations/local filtering, cycles, missing/ambiguous symbols, external imports, dynamic lookup, excluded test modules/methods/nested functions, oversized/malformed support, fresh CLI artifacts, mode errors, and no Ollama/environment construction. Frozen tests were not changed.

## Validation Performed

On 2026-09-16:

- `uv sync --frozen`: PASS — 12 packages checked.
- `uv run ruff format --check .`: PASS — 89 files already formatted.
- `uv run ruff check .`: PASS.
- `uv run pytest`: PASS — **269 passed, 3 skipped, 3 deselected** in 10.12 seconds.
- `uv run pytest tests/test_context_selector.py -q`: PASS — 33 passed.

The default suite remains offline. Three Ollama tests are opt-in skipped; three mutation tests remain deselected by default.

## End-to-End Context Demonstration

The actual command used `uv run python -m autotest.main --select-context tests/fixtures/projects/context_project --context-target src/shop/pricing.py:calculate_total --context-output-root workspace/context_bundles --context-max-depth 2`. It exited 0 with `PARTIAL` (intentional star-import uncertainty), selecting 4 files, 11 items, 584 exact source characters, 4 direct and 1 recursive local declarations, one stdlib external reference (`decimal`), two star-import unresolved records, and no omissions. The bundle is `workspace/context_bundles/20260916T165642Z-a7116f/context_bundle.json`; the text and timing metric are alongside it. It contains target `calculate_total`, same-file `normalize_total`, local `Order`, `DEFAULT_DISCOUNT`, cross-file `compute_tax`, and recursive `TAX_RATE`; unused helpers, `utils.py`, and unused `unrelated_module` were excluded. Target and dependency modules both contain top-level `raise RuntimeError(...)` but selection succeeded, demonstrating they were not imported/executed.

## Depth Comparison Demonstration

The actual depth-1 command produced `workspace/context_bundles/20260916T165639Z-7335b1/context_bundle.json`: 4 files, 9 items, 530 characters, 4 direct local dependencies, and `TAX_RATE:DEPTH_LIMIT`. Depth 2 produced 4 files, 11 items, 584 characters, 5 local dependencies, and no omissions. The bundle hashes differ (`42e5cffa08baeb7a536469f780ea8fb6b9bdeb865a0d35ab041616f79faf8868` versus `ccd148adf93b11ca70cf11bc86aebc40891ec3522f4a8683e11c6f3abdb4a46f`). The extra depth-2 items are the `decimal` import in `tax.py` and `TAX_RATE`; the latter is the additional recursive local declaration.

## Acceptance Criteria

The applicable Phase 5C criteria pass: frozen regressions and CLI compatibility; no dependency/lockfile change; profile-based, portable, bounded, deterministic static context; exact complete target and support source; indexed top-level declaration kinds; direct/aliased/relative and module-attribute resolution; external/unresolved/omission and hash evidence; no imports/execution/LLM/environment work; fresh CLI artifacts; real fixture and depth comparison; documentation. The report does not claim repository-scale generation or execution.

## Known Limitations

Static local-binding filtering is conservative, not compiler-grade; complex closure/scoping behavior may create false positive or missing edges. Import resolution does not chase arbitrary `__init__.py` re-exports, `__all__`, star imports, runtime dynamic imports, attribute mutation, or installed external-library source. Import conditions are discovered but not evaluated. A module object referenced without a static attribute remains unresolved. A profile/source race is not an OS write barrier. No method target, whole-program graph, type inference, tokenizer, or existing-test context is provided.

## Risks / Technical Debt

Selecting a bounded declaration does not prove semantic relevance or oracle quality. Source may change between profile creation and later consumption; the bundle's source/file hashes are future verification evidence, not locking. `ContextBundle` is deliberately not connected to `PromptBuilder`; any Phase 5D integration must validate those hashes and preserve execution isolation. Python venvs and subprocesses are not hostile-code sandboxes.

## Compatibility Notes

Frozen Phase 1–5B execution statuses, exit codes, artifact schemas, profile/plan/environment models, coverage, mutation, and single-file prompts are unchanged. This phase adds a separate static mode and models only.

## Git / Repository State

Implementation started on clean `main` at `phase-5b-frozen`. Phase 5C files are uncommitted and untagged for review. Demonstration artifacts are ignored under `workspace/`. No commit or freeze tag was created.

## Recommended Next Phase

Phase 5D — Repository-Scale End-to-End Pilot: combine verified copied `TargetEnvironment` and source-checked `ContextBundle` with existing generation/execution, while addressing untrusted-code containment. Do not treat Phase 5C's static evidence as executed tests.

## Final Phase Status

PHASE STATUS: COMPLETE
READY TO FREEZE: YES

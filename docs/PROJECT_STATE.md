# AutoTest Project State

## Project Objective

AutoTest is a research prototype that generates pytest tests for one named top-level Python function in one standalone `.py` file, then records execution, target-function coverage, and optional mutation evidence. It also statically profiles local Python repositories for future phases. The repository is the source of truth; Phase 1 and 1.1 reports are absent.

## Current Milestone

Phase 5A repository inspection is implemented for review; the latest frozen milestone remains Phase 4B at tag `phase-4b-frozen` (`52a7abe`). Earlier frozen tags are `environment-v1.0`, `phase-2-frozen`, `phase-3-frozen`, and `phase-4a-frozen`. Phase 5A adds static discovery alongside the unchanged single-file generation pipeline. The worktree was clean at the start of Phase 5A implementation; no commit or tag was created for this phase.

## Implemented Phases

- Initial generation: AST-based selection, deterministic prompt, provider-neutral LLM interface, Ollama REST provider, saved pytest module, subprocess execution, and run artifacts.
- Phase 2: execution feedback, structural/oracle diagnostics, and bounded repair of generated tests.
- Phase 3: subprocess line/branch coverage for the selected function and transactional supplementary test generation.
- Phase 4A: opt-in, evaluation-only mutation testing through isolated WSL/Mutmut; no survivor-driven LLM calls.
- Phase 4B: separately opt-in survivor-diff prompts, supplementary candidates, and transactional mutation improvement checks.
- Phase 5A: separate static local-project inspection, structured `ProjectProfile`, deterministic JSON, and `--inspect-project` CLI mode. It does not prepare or execute target environments.

## Current Pipeline

`ProjectAnalyzer` parses source without importing it. `RepairEngine` builds the initial prompt, calls `LLMProvider`, persists attempt 0, and runs pytest through `TestRunner`; non-PASS outcomes can receive up to the configured repair limit. Only a PASS suite enters `CoverageEngine`, which measures baseline coverage even with zero feedback rounds. Each coverage proposal is a new module, can use candidate-only repair, and joins the cumulative suite only after PASS and a non-regressing target coverage gain. Optional `--mutation` evaluates that accepted suite in a fresh WSL workspace. `--mutation-feedback` also extracts survivor IDs/diffs, generates additional modules, checks the original suite, remeasures coverage, and reruns mutation; it accepts only stable-universe, non-regressing mutation improvement. Root `result.json` is written after orchestration. A nonpassing Phase 2 suite skips coverage and mutation; a coverage infrastructure error skips mutation and exits 2.

Separately, `ProjectInspector` takes a local directory, statically reads known metadata/dependency files, discovers source/test roots and Python modules, parses top-level declarations with AST, and returns `ProjectProfile`. Inspection mode makes no Ollama call, target import, test subprocess, or dependency installation. Optional JSON output is independent of the Phase 1–4B run-artifact schema.

## Core Architecture

| Concern | Implementation |
| --- | --- |
| Source analysis | `project_analyzer.py`: `ProjectAnalyzer`, `FunctionInfo` (top-level sync/async functions) |
| Repository inspection | `project_inspector.py`: `ProjectInspector`, immutable `ProjectProfile`, `PythonModuleInfo`, `FunctionSummary`, dependency and Python-requirement declarations |
| Prompts and LLM | `prompt_builder.py`: `PromptBuilder`; `llm/base.py`: `LLMProvider`; `llm/ollama_provider.py`: `OllamaProvider` |
| Generation and execution | `test_generator.py`: `TestGenerator`, `sanitize_test_code`; `test_runner.py`: `TestRunner` |
| Feedback and repair | `failure_analyzer.py`: `FailureAnalyzer`; `test_quality.py`: AST diagnostics; `repair_engine.py`: `RepairEngine` |
| Coverage | `coverage_runner.py`: `CoverageRunner`; `coverage_engine.py`: `CoverageEngine` |
| Mutation | `mutation_runner.py`: `MutationBackend`, `WSLMutmutBackend`; `mutation_feedback.py`: `MutationFeedbackEngine` |
| Persistence, CLI, errors | `artifact_store.py`: `ArtifactStore`; `main.py`: CLI orchestration; `errors.py`: `AutoTestError` subclasses |

## Frozen Contracts

- Execution statuses are `PASS`, `FAIL`, `ERROR`, `TIMEOUT`. Pytest exit 0 maps to PASS, 1 to FAIL, other exits to ERROR; a subprocess timeout maps to TIMEOUT. CLI exits are 0 PASS, 1 FAIL, 2 ERROR/pipeline or requested quality-tool infrastructure error, 3 TIMEOUT. Missed coverage targets and low mutation scores do not fail a passing suite.
- Attempt 0 is initial generation; attempts 1..N are repairs. The default `max_repair_attempts=3` allows at most four executions per candidate; zero allows one. PASS stops immediately. ERROR and TIMEOUT are repair eligible; FAIL has conservative source-justified oracle guidance. `repair_success` means initial status was non-PASS and final status PASS, not semantic correctness.
- Coverage is measured only after PASS, with `coverage run --branch` and JSON in subprocesses. Executable lines are restricted to the AST-selected function range; branch arcs are filtered by origin line and keep their destinations. A branchless function reports N/A and satisfies the branch side of the target. Baseline is round 0; default target is 100% and default feedback limit is three rounds. Zero rounds still measures baseline. Coverage candidates must pass, add at least one target line/arc gain, and preserve previously covered lines/arcs. Accepted modules remain cumulative; rejected modules remain artifacts.
- Mutation is off by default. `--mutation` alone is Phase 4A evaluation only. `WSLMutmutBackend` copies the target and accepted tests into a fresh workspace, runs clean pytest, then pinned Mutmut on `<module>.x_<function>__mutmut_*` with one child. The original source is not deliberately mutated. Score is `killed / (killed + survived) * 100`, or N/A for zero denominator. Other statuses are retained but excluded; unreported Mutmut export counts are recorded separately.
- `--mutation-feedback` implies mutation. A COMPLETE baseline is round 0; zero survivors or zero configured rounds make no feedback generation call. Default limits are three rounds and five survivor diffs per round. Candidate PASS against original source is necessary but insufficient. Coverage may stay flat but cannot regress. Full sorted mutant IDs, target/source/config hashes, backend version, and selected function must agree across evaluations; at least one former survivor must become killed, no former killed mutant may survive, and score cannot decrease. Failed candidates never join the accepted suite.

## Environment Baseline

Repository pins CPython `3.12.10` (`.python-version`, `requires-python >=3.12,<3.13`), pytest `8.4.2`, pytest-cov `7.1.0`, coverage.py `7.16.0`, pytest-timeout `2.4.0`, Ruff `0.16.6`, and psutil `7.2.2`; `uv.lock` SHA-256 is `474378DFF8B726C8AF35C97E3CE7518F4240BFD1A28542B76736ACA55B4901B7`. The intended README baseline says Windows 11 x64, uv, Ollama at `http://localhost:11434`, and `qwen2.5-coder:14b` with temperature 0 and 120-second HTTP timeout. This audit PC reports Windows 10 Pro x64 (10.0.19045), while the test interpreter is Python 3.12.10. The Ollama endpoint responded and listed the expected model; live generation was not run.

## External Mutation Environment

`tools/mutation/requirements-mutation.txt` pins Mutmut `3.7.0` and pytest `8.4.2`; neither Mutmut nor its dependencies are in the main lockfile. The backend defaults to WSL distribution name `Ubuntu-22.04`, venv `/home/ubuntu/autotest-mutation-env`, and a 300-second outer timeout. This PC instead lists WSL distribution `Ubuntu` (Ubuntu 22.04.1 LTS); that venv reports Python `3.10.12`, pytest `8.4.2`, and Mutmut `3.7.0`. A direct probe of `Ubuntu-22.04` fails because the named distribution does not exist, so default-backend mutation and mutation-feedback integrations are **NOT VERIFIED ON THIS MACHINE**. The separate setup README also contains a historical `/mnt/e/University/...` requirements path; this checkout is `E:\Dev\AutoTest`.

## CLI Capabilities

`uv run python -m autotest.main --file <file.py> --function <top-level-name>` accepts model/URL/output, pytest and Ollama timeouts, temperature, repair and coverage limits/target, opt-in `--mutation` or `--mutation-feedback`, mutation round/survivor bounds, mutation timeout/venv, and `--debug`. Defaults include `workspace/runs`, 30-second test timeout, 3 repair rounds, 3 coverage rounds, 100% coverage target, 3 mutation feedback rounds, and 5 survivors per round. There is no CLI option for the WSL distribution name.

`uv run python -m autotest.main --inspect-project <directory> [--profile-output <file>]` runs only static inspection. It needs no `--file`/`--function`, prints a summary, and optionally writes portable UTF-8 JSON to the explicitly requested path. Exit 0 means inspection completed, including a non-Python directory; exit 2 means inspection or output persistence failed. It does not report test execution statuses.

## Artifact Structure

`ArtifactStore` reserves collision-resistant UTC run directories and writes UTF-8 files with exclusive creation. Typical hierarchy:

```text
workspace/runs/<run-id>/
  result.json
  attempt-000/  attempt-001/...       # prompt, raw response, generated test, stdout/stderr, result
  coverage/baseline/               # raw/normalized JSON, stdout/stderr
  coverage/round-001/...           # prompt, response, candidate, result, nested candidate/attempt-*/
  mutation/                        # mutation_result.json, raw export, stdout/stderr, config/, workspace/
  mutation-feedback/baseline/survivors/  # raw results and exact selected diffs
  mutation-feedback/round-001/...  # prompt, response, candidate, result, nested candidate/,
                                  # coverage/, mutation/ with fresh workspace and survivor evidence
```

Exact prompts, unsanitized model responses, generated/accepted test files, pytest output, raw coverage JSON, raw Mutmut statistics, full survivor results and selected exact diffs, and input/config hashes are the primary reproducible evidence. Normalized result files and root `result.json` summarize decisions and metrics. Partial attempt/round directories can remain after a provider or pipeline failure; no result is fabricated for absent work.

## Research Metrics

Attempt results record execution status/exit/duration, generation time, failure category, test/assert counts, quality warnings, timeout, and prompt/test hashes. Root results record initial/final execution, repair count/success, attempt history, stop reason, provider settings, source hash, and generation/execution totals. Coverage adds initial/final line and branch percentages, missing lines/arcs, gains, accepted history, rounds, target/stop state, LLM and repair call counts, and coverage measurement time. Mutation adds backend/tool versions, status, category counts, score, duration, workspace/config/input hashes, and infrastructure errors. Feedback adds initial/final mutation scores and counts, gain, survivors reduced, round decisions and accepted files, LLM/repair counts, generation/execution/mutation time, and final line/branch coverage. These are research evidence, not proofs of oracle correctness.

## Test / Validation Strategy

`pyproject.toml` defaults to `-m "not mutation"`. Offline tests mock HTTP and backend decisions or use local pytest/coverage subprocesses; default runs need no Ollama, WSL, or Mutmut. Three `ollama` tests in `test_ollama_integration.py` require `AUTOTEST_RUN_OLLAMA=1`; one real mutation test and two mutation-feedback tests are deselected by default. The deterministic feedback integration needs Mutmut but no Ollama. The combined live test additionally requires `AUTOTEST_RUN_OLLAMA_MUTATION_FEEDBACK=1`.

```powershell
uv sync --frozen
uv run ruff format --check .
uv run ruff check .
uv run pytest
$env:AUTOTEST_RUN_OLLAMA = '1'; uv run pytest -m ollama tests/test_ollama_integration.py -v
uv run pytest -m mutation tests/test_mutation_integration.py -v
uv run pytest -m mutation tests/test_mutation_feedback_integration.py::test_live_mutmut_feedback_improves_boundary_suite -v
$env:AUTOTEST_RUN_OLLAMA_MUTATION_FEEDBACK = '1'; uv run pytest -m 'mutation and ollama' tests/test_mutation_feedback_integration.py::test_live_ollama_and_mutmut_feedback_is_transactional -v
```

On 2026-09-16, `uv sync --frozen` passed (12 packages checked); both Ruff checks passed (52 files formatted); default pytest passed with **175 passed, 3 skipped, 3 deselected**. Phase reports give historical counts, not the current baseline. Live integrations were not rerun during this audit.

The Phase 5A development baseline started from that green suite. Phase 5A adds local fixture tests for metadata parsing, static `setup.py`, source/test roots, function discovery, safety limits, deterministic JSON, and inspection CLI isolation. Its validation result is recorded in `docs/phase_reports/phase-5a.md`.

## Known Limitations

Generation scope is one standalone file and one top-level function. No package/method context, environment preparation, external specification, equivalent-mutant classification, or source repair exists. Prompt context is character bounded. PASS, coverage, and mutation score are distinct evidence, never semantic correctness proofs. Generated tests are untrusted; subprocess timeouts and WSL workspaces are not a security sandbox.

Phase 5A discovers dependency declarations and layout but does not resolve or install dependencies, interpret lockfile solver semantics, execute target code, select cross-file context, or run generation against a repository. Dynamic `setup.py` values remain warnings. The source scan is bounded to 2 MiB per file and 10,000 Python files by default; excluded directories and directory symlinks are not traversed.

## Important Safety Constraints

Static analysis does not import target modules. AutoTest does not use `exec()`/`eval()` for generated tests; target/generated code runs through pytest/coverage subprocesses. Mutation copies target and accepted tests before Mutmut runs, with hashes checked; feedback also verifies protected originals after key stages. Accepted tests are append-only in orchestration and supplementary modules are separate. **The original source and accepted files are not OS write-protected during the initial/coverage pytest subprocesses.** A malicious generated test could modify them despite prompt constraints. Treat the source/accepted immutability rule as an orchestration contract with this enforcement limit, and use disposable isolation for untrusted targets.

## Git / Sync Workflow

`main` tracks `origin` on GitHub; frozen tags mark milestones. Preserve existing dirty files and ignored `workspace/` run evidence; review `git status --short` before/after future work. `.gitignore` and `.syncthing-ignore` exclude caches, temporary outputs, and workspace runs; Syncthing configuration is local. Do not infer unrecorded Phase 1 history from later reports. No commit or tag was created by this audit.

## Current Technical Debt

The WSL backend's distribution name is fixed in code and differs from this PC's installed alias. The mutation setup README has a stale absolute checkout path. The README's Windows 11 baseline differs from this PC. Generated-test subprocesses have no filesystem/container isolation, and accepted-file integrity is not checked throughout initial/coverage execution. Heuristic failure categories and AST oracle warnings cannot establish semantic test quality. Earlier phase reports and their counts are historical snapshots.

## Recommended Next Phase

Phase 5B — Target Environment Management: use the Phase 5A profile to plan isolated target interpreters and dependency preparation, without changing the frozen generation/repair/coverage/mutation contracts implicitly. Address source/accepted-file integrity and containment before broad untrusted-repository trials. Cross-file context selection and repository test generation remain later work.

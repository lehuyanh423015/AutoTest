# AutoTest — Phase 4A mutation testing evaluation

AutoTest is a research prototype that asks a local large language model to generate pytest
tests for a selected Python function. Phase 4A adds opt-in mutation-quality evaluation after the
frozen generation, repair, and coverage pipeline has produced a final passing cumulative suite:

```text
standalone .py file -> AST analysis -> deterministic prompt -> Ollama
                    -> generated test file -> pytest subprocess -> PASS?
                    -> failure analysis -> repair prompt -> bounded retry
                    -> line/branch coverage baseline -> target reached?
                    -> additional test -> cumulative execution/repair -> coverage comparison
                    -> final passing suite -> isolated WSL/Mutmut evaluation -> mutation metrics
```

The current scope is one standalone Python file and one named top-level function at a time.
Static analysis never imports or executes the target module. Generated code is never evaluated,
executed, or imported in the AutoTest process; it is saved and passed to pytest in a separate
process with a configurable timeout.

## Architecture

- `project_analyzer.py` discovers and extracts top-level functions with Python's `ast` module.
- `prompt_builder.py` creates a deterministic, constrained pytest prompt.
- `llm/base.py` defines the provider-neutral generation interface.
- `llm/ollama_provider.py` calls Ollama's `/api/generate` REST endpoint with `urllib.request`.
- `test_generator.py` cleans one optional Markdown fence and saves a deterministic test file.
- `artifact_store.py` preserves each CLI run, its inputs, result metadata, and SHA-256 hashes.
- `failure_analyzer.py` creates coarse failure categories and tail-preserving bounded feedback.
- `test_quality.py` records structural metrics and flags obvious oracle degradation patterns.
- `repair_engine.py` owns the provider-neutral, iterative, bounded repair loop.
- `test_runner.py` invokes the current interpreter's pytest in a timed subprocess.
- `coverage_runner.py` invokes coverage.py in subprocesses and normalizes only the selected
  function's lines and branch arcs.
- `coverage_engine.py` owns the bounded, transactional supplementary-test loop.
- `mutation_runner.py` defines the backend boundary and invokes pinned Mutmut through WSL.
- `main.py` reports execution, coverage, and mutation outcomes separately.

The pytest subprocess runs in the controlled generated-test directory and uses that directory as
its explicit pytest root. The target file's parent directory is prepended to `PYTHONPATH` in the
subprocess environment only, allowing a test such as `from calculator import divide` to work
without changing the user's global environment. The explicit root also avoids unsafe broad
collection when target and output directories are on different Windows drives. Pytest caching and
Python bytecode output are disabled in this subprocess so the run directory remains an artifact
record rather than accumulating execution caches.

## Environment baseline

- Windows 11 x64 and CPython 3.12.10
- [uv](https://docs.astral.sh/uv/)
- pytest 8.4.2, pytest-cov 7.1.0, coverage 7.16.0, and pytest-timeout 2.4.0
- Ruff 0.16.6 and psutil 7.2.2
- [Ollama](https://ollama.com/) for real generation
- The baseline model `qwen2.5-coder:14b`

Mutation evaluation has a separate WSL environment containing Python, pytest, and exactly
`mutmut==3.7.0`. Mutmut is intentionally absent from the main Windows environment,
`pyproject.toml`, and `uv.lock`. Setup details and isolated pins live under `tools/mutation/`.

Install the locked project environment:

```powershell
uv sync --frozen
```

For a live run, start Ollama and ensure the model is present:

```powershell
ollama serve
ollama pull qwen2.5-coder:14b
```

If Ollama already runs as a Windows service, a separate `ollama serve` process is unnecessary.
The default endpoint is `http://localhost:11434`.

## Run the Phase 4A CLI

From the repository root:

```powershell
uv run python -m autotest.main `
    --file tests/fixtures/sample_project/branching.py `
    --function classify_number `
    --max-repair-attempts 3 `
    --max-coverage-rounds 3 `
    --coverage-target 100
```

Useful options include `--model`, `--ollama-url`, `--output-dir`, `--timeout`,
`--ollama-timeout`, `--temperature`, `--max-repair-attempts`, `--max-coverage-rounds`, and
`--coverage-target`. Phase 4A adds `--mutation`, `--mutation-timeout`, and `--mutation-venv`;
mutation is off by default, its outer timeout defaults to 300 seconds, and its isolated WSL
environment defaults to `/home/ubuntu/autotest-mutation-env`. Both bounded-loop limits default to `3`;
the coverage target defaults to
`100`. A repair limit of `0` reproduces direct Phase 1 generation/execution. A coverage-round
limit of `0` still measures the baseline but makes no coverage-generation request:

```powershell
uv run python -m autotest.main `
    --file tests/fixtures/sample_project/calculator.py `
    --function divide `
    --max-repair-attempts 0 `
    --max-coverage-rounds 0
```

`--max-coverage-rounds` must be non-negative. `--coverage-target` must be from 0 through 100 and
applies to line coverage and branch coverage when branches exist. `--output-dir` selects the
immutable artifact root and defaults to `workspace/runs/`, which is ignored by Git.

## Coverage guidance and acceptance

Coverage begins only after Phase 2 has produced a passing suite. Round 0 measures its baseline
with `python -m coverage run --branch -m pytest`, followed by the pinned coverage.py JSON command.
Both commands use the current Python interpreter, a subprocess-only `PYTHONPATH` and
`COVERAGE_FILE`, and a controlled artifact directory. AutoTest never imports or evaluates target
code itself.

The analyzer's AST-derived start/end lines define the target function range, including decorators
when present. Executable and missing lines come from coverage.py and are filtered to that range;
physical source lines are not guessed to be executable. Branch arcs are filtered when their
origin line belongs to the function. Destinations, including negative coverage.py sentinel values,
are preserved. A function with no measurable arcs reports branch coverage as N/A, which satisfies
the branch side of target-reached logic. Other functions in the module cannot lower the selected
function's percentage.

Each feedback round receives actual line-numbered target source, current percentages, missing
lines/arcs, and bounded accepted-test context. It must return one supplementary pytest module.
Accepted files are immutable: AutoTest executes all prior files plus the candidate, and Phase 2
repair may modify only that candidate. A passing candidate is accepted only if target coverage
improves without regression. A non-improving or regressing candidate remains preserved for
analysis but does not enter the final suite.

Coverage stops at `TARGET_REACHED`, `MAX_ROUNDS_REACHED`, `NO_COVERAGE_IMPROVEMENT`,
`COVERAGE_REGRESSION`, `CANDIDATE_NOT_ACCEPTED`, `COVERAGE_ERROR`, `LLM_ERROR`, or
`PIPELINE_ERROR`. All loops are finite; no limit is increased automatically.

High coverage does not prove that a test suite is correct or effective at detecting faults.

Coverage feedback complements execution feedback; it does not replace it.

## Mutation evaluation

Mutation testing evaluates whether tests detect artificial changes to program behavior. It gives
stronger evidence than line execution alone because it checks whether assertions distinguish the
current behavior from small altered behaviors. High code coverage does not imply high mutation
score; both metrics are retained and reported independently.

Phase 4A runs only when `--mutation` is supplied and the final accepted cumulative suite has
execution status `PASS`. It makes zero mutation-guided LLM calls. Phase 4A does not use surviving
mutants to generate new tests. A low score never changes the passing execution status and is not a
quality gate. A survived mutant does not automatically mean the generated test suite is incorrect;
equivalent or irrelevant mutants can exist.

On Windows, AutoTest invokes Python, pytest, and Mutmut by absolute path inside the configured WSL
virtualenv; it does not rely on activation, shell initialization, or global WSL tools. It validates
pytest 8.4.2 and Mutmut 3.7.0 exactly. It creates a
fresh workspace containing only the target file and final accepted tests, runs a clean pytest
baseline there, then uses Mutmut 3.7.0's generated key pattern
`<module>.x_<function>__mutmut_*`. The target is copied to the workspace root so unchanged accepted
imports such as `from calculator import divide` produce the same module key Mutmut sees.
`source_paths` contains only that exact file; generated tests are not mutated. Original source and
accepted-test hashes are checked after the tool runs.

```powershell
uv run python -m autotest.main `
    --file tests/fixtures/sample_project/branching.py `
    --function classify_number `
    --mutation `
    --mutation-timeout 300
```

AutoTest normalizes the supported Mutmut 3.7.0 `export-cicd-stats` JSON. Its score is
`killed / (killed + survived) * 100`. Timeout, skipped, suspicious, no-tests, interrupted, and
segfault categories are persisted but excluded from the denominator. If killed plus survived is
zero, the score is N/A. The export omits not-checked and caught-by-type-check counts, so their
aggregate difference from the tool total is recorded conservatively as `unreported_mutants`.
With AutoTest's type-check command disabled, mutants outside the selected-function run remain not
checked and do not contribute to the selected total or score.

## Repair policy

`ERROR` and `TIMEOUT` normally receive a repair attempt. ERROR guidance targets invalid syntax,
imports, invented APIs, fixtures, and collection structure. TIMEOUT guidance removes waiting,
blocking behavior, network use, infinite loops, and excessive work without increasing the timeout.

`FAIL` is handled conservatively because it can represent either an unsupported generated
expectation or a real target defect. Repair prompts permit changing an expectation only when the
supplied source clearly justifies it. They prohibit deleting meaningful assertions, weakening
oracles, copying observed output without source justification, and self-derived tautologies.
Lightweight AST checks record warnings for obvious degradation but do not rewrite tests.

Repair stops at PASS, the configured maximum, or an LLM/pipeline failure. `repair_success` means
only that the initial status was not PASS and the final status became PASS. A repaired suite
reaching PASS is executable and passing against the current implementation; it does not prove the
test oracle is correct or that the target program is defect-free.

## Run artifacts

Every successful generation gets a collision-resistant UTC run directory. Phase 3 adds a coverage
subtree without changing the Phase 2 attempt layout:

```text
workspace/runs/<UTC-timestamp>-<random-id>/
├── result.json
├── attempt-000/
│   ├── prompt.txt
│   ├── raw_response.txt
│   ├── generated_test.py
│   ├── stdout.txt
│   ├── stderr.txt
│   └── result.json
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
        ├── result.json
        └── candidate/attempt-000/...
```

Phase 4A adds an independent mutation subtree when requested:

```text
mutation/
├── mutation_result.json
├── mutmut_raw_stats.json
├── stdout.txt
├── stderr.txt
├── config/pyproject.toml
└── workspace/
    ├── <target>.py
    ├── tests/test_accepted_*.py
    └── mutants/...
```

The exact raw export is authoritative. Normalized categories, WSL tool versions, input/config
hashes, duration, and infrastructure errors are stored separately. No Mutmut cache is shared
between research runs.

Attempt 0 is always the initial generation; later attempts are repairs. Files use exclusive UTF-8
writes, so completed attempts are never overwritten. Every attempt records execution output,
status, timing, failure category, test/assert counts, quality warnings, and prompt/test SHA-256
hashes. Exact prompts and pre-sanitization model responses are retained. Root `result.json`
summarizes initial/final status, attempt history, repair count and success, stop reason, aggregate
generation/execution time, effective Ollama configuration, and the source hash. Phase 3 adds
initial/final coverage, missing lines/arcs, accepted history, gain, stop state, generation/repair
call counts, and aggregate measurement timing. Large raw reports remain separate. All writes are
exclusive UTF-8 writes, and temporary coverage databases are removed from artifact directories.

Feedback sent to the LLM is limited to 12,000 characters by default. When necessary, AutoTest
retains the end of pytest output because it typically contains the failed assertion and exception
details; complete stdout/stderr remain available in the attempt object and artifact files.

## Status and exit-code contracts

Execution statuses have these stable meanings:

- `PASS`: pytest exit code 0; every generated test passed.
- `FAIL`: pytest exit code 1; tests executed and at least one test failed.
- `ERROR`: generated code could not be collected or executed correctly, including syntax and
  import errors or other non-0/1 pytest outcomes.
- `TIMEOUT`: the generated-test subprocess exceeded its configured timeout. This is distinct from
  `ERROR` and normally has no subprocess exit code.

CLI process exit codes are `0` for PASS, `1` for FAIL, `2` for ERROR or an
analysis/generation/artifact pipeline error, and `3` for TIMEOUT. Expected errors are concise;
`--debug` enables tracebacks for diagnosis. Failing to reach the requested coverage target does
not turn a passing suite into FAIL and does not introduce a new exit code. A genuine coverage
infrastructure failure uses the existing pipeline-error exit code 2.
Likewise, a low mutation score preserves exit code 0 for a passing suite. Requested mutation
infrastructure failure uses exit code 2. Mutation has separate `COMPLETE`, `NO_MUTANTS`,
`TOOL_UNAVAILABLE`, `TOOL_ERROR`, and `TIMEOUT` statuses.

The effective research baseline is model `qwen2.5-coder:14b`, Ollama URL
`http://localhost:11434`, HTTP timeout 120 seconds, and temperature `0.0`. These controls reduce
experimental variation, but local LLM output is not guaranteed to be bit-for-bit deterministic.

## Test and lint AutoTest

The default suite mocks HTTP and does not require Ollama:

```powershell
uv run pytest
uv run ruff format --check .
uv run ruff check .
```

Run the opt-in live generation, repair, and coverage tests only when Ollama and the model are
available:

```powershell
$env:AUTOTEST_RUN_OLLAMA = "1"
uv run pytest -m ollama tests/test_ollama_integration.py -v
```

The default suite skips live mutation and needs neither WSL nor Mutmut. Opt into the real pinned
backend integration with:

```powershell
uv run pytest -m mutation tests/test_mutation_integration.py -v
```

## Current limitations

Phase 4A supports one standalone Python file and one top-level sync or async function. It does not
prepare arbitrary repository dependencies, analyze methods or cross-file context, verify test
oracles against an external specification, identify equivalent mutants, use mutation feedback for
generation, or provide OS/container sandboxing. Mutation requires a separately provisioned
Ubuntu 22.04 WSL environment and currently supports only pinned Mutmut 3.7.0. Accepted-test prompt
context is character-bounded,
not repository-scale context selection. AutoTest never repairs or modifies target source code.

Generated tests are untrusted code. The timeout and separate process are basic safety boundaries,
not a security sandbox. **Subprocess isolation is NOT a security sandbox.** Run AutoTest only in a
disposable or otherwise appropriately isolated environment until container sandboxing is added
in a later phase. Phase 1.1 also does not configure dependencies for arbitrary repositories.

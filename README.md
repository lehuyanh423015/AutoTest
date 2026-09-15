# AutoTest — Phase 2 execution feedback

AutoTest is a research prototype that asks a local large language model to generate pytest
tests for a selected Python function. Phase 2 adds a bounded execution-feedback loop to the
frozen Phase 1.1 pipeline:

```text
standalone .py file -> AST analysis -> deterministic prompt -> Ollama
                    -> generated test file -> pytest subprocess -> PASS?
                    -> failure analysis -> repair prompt -> bounded retry
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
- `main.py` connects the components and prints PASS, FAIL, ERROR, or TIMEOUT details.

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

## Run the Phase 2 CLI

From the repository root:

```powershell
uv run python -m autotest.main `
    --file tests/fixtures/sample_project/calculator.py `
    --function divide `
    --max-repair-attempts 3
```

Useful options include `--model`, `--ollama-url`, `--output-dir`, `--timeout`,
`--ollama-timeout`, `--temperature`, and `--max-repair-attempts`. The repair limit defaults to `3`,
meaning one initial execution plus at most three repaired executions. A value of `0` disables
repair and reproduces the direct Phase 1 generation/execution behavior:

```powershell
uv run python -m autotest.main `
    --file tests/fixtures/sample_project/calculator.py `
    --function divide `
    --max-repair-attempts 0
```

`--output-dir` selects the run-artifact root and defaults to `workspace/runs/`, which is ignored by
Git. The report includes initial and final statuses, repair progress, repairs used, stop reason,
generated path, duration, and captured pytest output.

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

Every successful generation gets a collision-resistant UTC run directory and does not overwrite
an earlier run:

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
└── attempt-001/
    └── ...
```

Attempt 0 is always the initial generation; later attempts are repairs. Files use exclusive UTF-8
writes, so completed attempts are never overwritten. Every attempt records execution output,
status, timing, failure category, test/assert counts, quality warnings, and prompt/test SHA-256
hashes. Exact prompts and pre-sanitization model responses are retained. Root `result.json`
summarizes initial/final status, attempt history, repair count and success, stop reason, aggregate
generation/execution time, effective Ollama configuration, and the source hash. Phase 1 root fields
remain present where applicable, but their artifact paths now point into `attempt-000/`.

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
`--debug` enables tracebacks for diagnosis.

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

Run the opt-in live smoke test only when Ollama and the model are available:

```powershell
$env:AUTOTEST_RUN_OLLAMA = "1"
uv run pytest -m ollama tests/test_ollama_integration.py -v
```

## Current limitations

Phase 2 supports a single Python file, a single top-level function, pytest generation through local
Ollama, subprocess execution, and bounded generated-test repair. It does not prepare arbitrary
repository dependencies, analyze methods or cross-file context, verify test oracles against an
external specification, guide generation with coverage, run mutation testing, or provide
OS/container sandboxing. It never repairs or modifies target source code.

Generated tests are untrusted code. The timeout and separate process are basic safety boundaries,
not a security sandbox. **Subprocess isolation is NOT a security sandbox.** Run AutoTest only in a
disposable or otherwise appropriately isolated environment until container sandboxing is added
in a later phase. Phase 1.1 also does not configure dependencies for arbitrary repositories.

# Phase 6 metric definitions (schema 1)

Each matrix cell has one logical run ID: `repository::target::configuration::repetition`.
Repetitions are 1-based. `COMPLETED` means the repository pipeline returned a research outcome;
the generated tests may still end in `FAIL`, `ERROR`, or `TIMEOUT`. `UNSUPPORTED`,
`PIPELINE_ERROR`, `TIMEOUT` (infrastructure), and `INCOMPLETE` are separate accounting statuses.
Only `COMPLETED` runs enter quality and cost averages. Every group also reports planned and
status counts. No metric silently replaces an unavailable measurement with zero.

`initial_pass` and `final_pass` are the proportion of measured initial/final pytest statuses equal
to `PASS`. `initial_executable` and `final_executable` count `PASS` or `FAIL` as executable;
collection/import `ERROR` and process `TIMEOUT` are not executable. Each rate reports its measured
denominator `n`.

`repair_attempts` is the frozen Phase 2 repair count. `repair_attempted` counts completed runs with
at least one repair. `repair_success` is the proportion of attempted repairs whose initial status
was not `PASS` and final status was `PASS`. Coverage percentages are target-function line and
branch coverage from Phase 3. Branchless targets have missing branch coverage. Gain is final minus
initial only when both exist. The coverage target rate requires final line coverage and, where
branch coverage applies, final branch coverage to meet the manifest's target.

Mutation score is `100 * killed / (killed + survived)`, and is missing when no applicable scored
mutants exist. Raw Mutmut total, applicable total, killed, survived, and other/unreported are
separate. The latter is `raw total - killed - survived`; it includes non-scoring statuses and
unreported entries. Mutation gain is final minus initial when both were measured. A/B/C can receive
evaluation-only mutation; their survivors never enter a prompt. D may use survivors for feedback.
The all-mutants-killed rate uses only runs with a measured final mutation score.

LLM calls count initial generation, execution repair, coverage generation, coverage candidate
repair, mutation feedback, and mutation candidate repair separately. Total is their sum when all
stage counters exist. Runtime uses measured environment preparation, generation, test execution,
coverage, mutation, and overall repository-run seconds. Stage timings can be missing; they are not
assumed zero. These values are cost proxies and are not token billing or USD estimates.

Each numeric aggregate reports observation count `n`, mean, median, sample standard deviation
when `n >= 2`, minimum, and maximum. Null observations are excluded. Groupings cover configuration,
repository/configuration, and target/configuration. The primary CSV has one row per configuration;
the JSON includes all groupings. No inferential significance claim is made.

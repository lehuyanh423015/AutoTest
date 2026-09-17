"""Sequential, resumable experiments over the frozen repository pipeline."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from autotest.context_selector import ContextTarget
from autotest.experiment import (
    ExperimentDefinition,
    ExperimentError,
    ExperimentPlan,
    ExperimentRunSpec,
    load_definition,
    plan_experiment,
)
from autotest.experiment_metrics import (
    METRICS,
    RUN_COLUMNS,
    ExperimentRunRecord,
    empty_record,
    grouped_aggregates,
    record_from_result,
    write_csv,
)
from autotest.project_inspector import ProjectInspector
from autotest.repository_runner import RepositoryRunEngine


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def _version(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


@dataclass(frozen=True, slots=True)
class ExperimentResult:
    directory: Path
    plan: ExperimentPlan
    records: tuple[ExperimentRunRecord, ...]

    @property
    def complete(self) -> bool:
        return len(self.records) == len(self.plan.specs) and all(
            item.values["run_status"] != "INCOMPLETE" for item in self.records
        )


class ExperimentRunner:
    def __init__(
        self,
        engine_factory: Callable[[ExperimentRunSpec], RepositoryRunEngine],
        *,
        progress: Callable[[str], None] = print,
    ):
        self.engine_factory = engine_factory
        self.progress = progress

    def run(
        self, definition: ExperimentDefinition, output_root: Path, *, manifest: Path
    ) -> ExperimentResult:
        plan = plan_experiment(definition)
        directory = Path(output_root).resolve() / definition.experiment_name
        if directory.exists():
            raise ExperimentError(f"Experiment directory already exists: {directory}; use resume")
        directory.mkdir(parents=True)
        original = Path(manifest).resolve(strict=True)
        _write_json(
            directory / "experiment_definition.json",
            {
                "schema_version": 1,
                "source_manifest": str(original),
                "source_manifest_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
                "definition_sha256": definition.sha256,
                "definition": definition.identity,
            },
        )
        _write_json(directory / "experiment_plan.json", plan.to_dict())
        evidence = {
            "autotest_git_commit": _version(["git", "rev-parse", "HEAD"]),
            "phase_5d_ancestor": _version(["git", "merge-base", "HEAD", "phase-5d-frozen"]),
            "python_version": platform.python_version(),
            "uv_version": _version(["uv", "--version"]),
            "model": definition.model,
        }
        _write_json(directory / "version_evidence.json", evidence)
        _write_json(directory / "experiment_state.json", self._state(plan, {}, _now()))
        return self._execute(definition, plan, directory)

    def resume(self, directory: Path) -> ExperimentResult:
        directory = Path(directory).resolve(strict=True)
        saved = json.loads((directory / "experiment_definition.json").read_text(encoding="utf-8"))
        source = Path(saved["source_manifest"])
        if (
            not source.is_file()
            or hashlib.sha256(source.read_bytes()).hexdigest() != saved["source_manifest_sha256"]
        ):
            raise ExperimentError(
                "RESUME_IDENTITY_MISMATCH: source manifest changed or disappeared"
            )
        definition = load_definition(source)
        plan = plan_experiment(definition)
        existing = json.loads((directory / "experiment_plan.json").read_text(encoding="utf-8"))
        if (
            definition.sha256 != saved["definition_sha256"]
            or plan.sha256 != existing["plan_sha256"]
            or plan.to_dict() != existing
        ):
            raise ExperimentError(
                "RESUME_IDENTITY_MISMATCH: definition, plan, or repository profile changed"
            )
        return self._execute(definition, plan, directory)

    @staticmethod
    def _state(
        plan: ExperimentPlan, records: dict[str, ExperimentRunRecord], started: str
    ) -> dict[str, Any]:
        statuses = {item.values["run_id"]: item.values["run_status"] for item in records.values()}
        return {
            "schema_version": 1,
            "definition_sha256": plan.definition_sha256,
            "plan_sha256": plan.sha256,
            "planned": len(plan.specs),
            "completed": sum(value == "COMPLETED" for value in statuses.values()),
            "unsupported": sum(value == "UNSUPPORTED" for value in statuses.values()),
            "failed": sum(value == "PIPELINE_ERROR" for value in statuses.values()),
            "timeouts": sum(value == "TIMEOUT" for value in statuses.values()),
            "incomplete": sum(value == "INCOMPLETE" for value in statuses.values()),
            "remaining": len(plan.specs)
            - sum(value != "INCOMPLETE" for value in statuses.values()),
            "run_statuses": statuses,
            "started_at": started,
            "updated_at": _now(),
        }

    def _execute(
        self, definition: ExperimentDefinition, plan: ExperimentPlan, directory: Path
    ) -> ExperimentResult:
        state = json.loads((directory / "experiment_state.json").read_text(encoding="utf-8"))
        if (
            state["definition_sha256"] != plan.definition_sha256
            or state["plan_sha256"] != plan.sha256
        ):
            raise ExperimentError("RESUME_IDENTITY_MISMATCH: checkpoint does not match plan")
        records: dict[str, ExperimentRunRecord] = {}
        for spec in plan.specs:
            run_dir = directory / "runs" / spec.run_id.replace("::", "__")
            path = run_dir / "run_record.json"
            if path.is_file():
                try:
                    item = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue  # An interrupted atomic write is retried.
                if item.get("run_id") != spec.run_id:
                    raise ExperimentError("RESUME_IDENTITY_MISMATCH: run record identity differs")
                spec_path = run_dir / "run_spec.json"
                if not spec_path.is_file() or json.loads(
                    spec_path.read_text(encoding="utf-8")
                ) != asdict(spec):
                    raise ExperimentError("RESUME_IDENTITY_MISMATCH: run spec differs")
                artifact = item.get("repository_run_artifact")
                if artifact:
                    reference = run_dir / "repository_run_reference.json"
                    try:
                        evidence = json.loads(reference.read_text(encoding="utf-8"))
                        raw = json.loads(Path(artifact).read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    if evidence.get("result") != artifact or evidence.get(
                        "result_sha256"
                    ) != raw.get("result_sha256"):
                        raise ExperimentError(
                            "RESUME_IDENTITY_MISMATCH: raw repository evidence differs"
                        )
                records[spec.run_id] = ExperimentRunRecord(
                    {key: item.get(key) for key in RUN_COLUMNS}
                )
        repositories = {item.id: item for item in definition.repositories}
        self.progress(f"Experiment: {definition.experiment_name}\nPlanned runs: {len(plan.specs)}")
        stopped = False
        for index, spec in enumerate(plan.specs, 1):
            if spec.run_id in records and records[spec.run_id].values["run_status"] != "INCOMPLETE":
                continue
            run_dir = directory / "runs" / spec.run_id.replace("::", "__")
            run_dir.mkdir(parents=True, exist_ok=True)
            _write_json(run_dir / "run_spec.json", asdict(spec))
            records[spec.run_id] = empty_record(
                definition.experiment_name, spec, "INCOMPLETE", "RUN_IN_PROGRESS"
            )
            _write_json(run_dir / "run_record.json", records[spec.run_id].to_dict())
            _write_json(
                directory / "experiment_state.json", self._state(plan, records, state["started_at"])
            )
            label = f"{spec.repository_id} {spec.target_id} {spec.configuration.upper()}"
            self.progress(f"[{index}/{len(plan.specs)}] {label} repetition {spec.repetition}")
            if spec.support_status == "UNSUPPORTED":
                record = empty_record(
                    definition.experiment_name,
                    spec,
                    "UNSUPPORTED",
                    spec.support_reason or "UNSUPPORTED",
                )
            else:
                try:
                    repository = repositories[spec.repository_id]
                    current_profile = ProjectInspector().inspect(repository.path).sha256
                    if current_profile != spec.profile_sha256:
                        raise ExperimentError(
                            "RESUME_IDENTITY_MISMATCH: repository profile changed"
                        )
                    options = next(
                        item for item in definition.configurations if item.id == spec.configuration
                    ).options(definition, directory)
                    options = replace(
                        options,
                        environment_output_root=directory
                        / "target_environments"
                        / spec.run_id.replace("::", "__"),
                    )
                    result = self.engine_factory(spec).run(
                        repository.path, ContextTarget.parse(spec.target), options
                    )
                    record = record_from_result(definition.experiment_name, spec, result)
                    _write_json(
                        run_dir / "repository_run_reference.json",
                        {
                            "result": str(result.run_directory / "result.json"),
                            "result_sha256": result.to_dict()["result_sha256"],
                        },
                    )
                except KeyboardInterrupt:
                    _write_json(
                        directory / "experiment_state.json",
                        self._state(plan, records, state["started_at"]),
                    )
                    self._exports(definition, plan, directory, records, state["started_at"])
                    raise
                except ExperimentError:
                    raise
                except Exception as exc:
                    record = empty_record(
                        definition.experiment_name,
                        spec,
                        "PIPELINE_ERROR",
                        f"{type(exc).__name__}: {exc}",
                    )
            records[spec.run_id] = record
            _write_json(run_dir / "run_record.json", record.to_dict())
            _write_json(
                directory / "experiment_state.json", self._state(plan, records, state["started_at"])
            )
            outcome = record.values["final_execution_status"] or record.values["stop_reason"]
            self.progress(f"Result: {record.values['run_status']} / {outcome}")
            if definition.fail_fast and record.values["run_status"] in {
                "UNSUPPORTED",
                "PIPELINE_ERROR",
                "TIMEOUT",
            }:
                stopped = True
                break
        self._exports(definition, plan, directory, records, state["started_at"])
        if stopped:
            self.progress(
                "Experiment stopped by fail_fast; resume to continue after resolving the cause."
            )
        return ExperimentResult(directory, plan, tuple(records[key] for key in sorted(records)))

    def _exports(
        self,
        definition: ExperimentDefinition,
        plan: ExperimentPlan,
        directory: Path,
        records: dict[str, ExperimentRunRecord],
        started: str,
    ) -> None:
        results = directory / "results"
        results.mkdir(exist_ok=True)
        ordered = [records[spec.run_id] for spec in plan.specs if spec.run_id in records]
        _write_json(
            results / "runs.json",
            {"schema_version": 1, "records": [item.to_dict() for item in ordered]},
        )
        write_csv(results / "runs.csv", list(RUN_COLUMNS), [item.values for item in ordered])
        groups = grouped_aggregates(ordered, definition.coverage_target)
        _write_json(results / "aggregates.json", groups)
        rates = (
            "initial_pass",
            "final_pass",
            "initial_executable",
            "final_executable",
            "repair_success",
            "coverage_target_reached",
            "all_mutants_killed",
        )
        stats = ("n", "mean", "median", "sample_stddev", "min", "max")
        aggregate_columns = [
            "configuration",
            "planned",
            "completed",
            "unsupported",
            "pipeline_errors",
            "timeouts",
            "incomplete",
            "repair_attempted",
        ]
        aggregate_columns += [f"{rate}_{field}" for rate in rates for field in ("n", "rate")]
        aggregate_columns += [f"{metric}_{field}" for metric in METRICS for field in stats]
        aggregate_rows = []
        for config, data in groups["by_configuration"].items():
            row = {key: data[key] for key in aggregate_columns[:8] if key != "configuration"}
            row["configuration"] = config
            for rate in rates:
                for field in ("n", "rate"):
                    row[f"{rate}_{field}"] = data[rate][field]
            for metric in METRICS:
                for field in stats:
                    row[f"{metric}_{field}"] = data["metrics"][metric][field]
            aggregate_rows.append(row)
        write_csv(results / "aggregates.csv", aggregate_columns, aggregate_rows)
        write_csv(
            results / "failures.csv",
            [
                "repository_id",
                "target_id",
                "configuration",
                "repetition",
                "run_status",
                "stop_reason",
                "repository_run_artifact",
            ],
            [item.values for item in ordered if item.values["run_status"] != "COMPLETED"],
        )
        state = self._state(plan, records, started)
        _write_json(directory / "experiment_state.json", state)
        end = _now()
        _write_json(
            directory / "summary.json",
            {
                "schema_version": 1,
                "experiment_name": definition.experiment_name,
                "definition_sha256": definition.sha256,
                "plan_sha256": plan.sha256,
                "planned_runs": len(plan.specs),
                "completed_runs": state["completed"],
                "unsupported_runs": state["unsupported"],
                "pipeline_failures": state["failed"],
                "timeouts": state["timeouts"],
                "incomplete": state["incomplete"],
                "remaining": state["remaining"],
                "started_at": started,
                "ended_at": end,
                "wall_clock_seconds": (
                    datetime.fromisoformat(end) - datetime.fromisoformat(started)
                ).total_seconds(),
                "configurations": [item.id for item in definition.configurations],
                "primary_aggregate": groups["by_configuration"],
            },
        )

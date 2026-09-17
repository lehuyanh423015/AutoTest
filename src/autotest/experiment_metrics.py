"""Flat research records and deterministic descriptive statistics."""

from __future__ import annotations

import csv
import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autotest.experiment import CONFIGURATIONS, ExperimentRunSpec
from autotest.repository_runner import RepositoryRunResult

SCHEMA_VERSION = 1
RUN_COLUMNS = (
    "experiment_id",
    "run_id",
    "repository_id",
    "target_id",
    "target",
    "configuration",
    "repetition",
    "model",
    "temperature",
    "run_status",
    "stop_reason",
    "profile_sha256",
    "context_status",
    "context_chars",
    "context_files",
    "context_items",
    "unresolved_references",
    "omitted_context_items",
    "environment_status",
    "target_python_version",
    "initial_execution_status",
    "final_execution_status",
    "repair_attempts",
    "repair_success",
    "initial_line_coverage",
    "final_line_coverage",
    "line_coverage_gain",
    "initial_branch_coverage",
    "final_branch_coverage",
    "branch_coverage_gain",
    "coverage_rounds",
    "accepted_coverage_rounds",
    "mutation_requested",
    "mutation_feedback_enabled",
    "mutation_status",
    "mutation_raw_total",
    "mutation_applicable_total",
    "mutation_killed",
    "mutation_survived",
    "mutation_other_or_unreported",
    "initial_mutation_score",
    "final_mutation_score",
    "mutation_score_gain",
    "llm_calls_initial_generation",
    "llm_calls_execution_repair",
    "llm_calls_coverage_generation",
    "llm_calls_coverage_repair",
    "llm_calls_mutation_feedback",
    "llm_calls_mutation_repair",
    "llm_calls_total",
    "environment_duration",
    "generation_duration",
    "execution_duration",
    "coverage_duration",
    "mutation_duration",
    "total_duration",
    "source_integrity_verified",
    "accepted_test_integrity_verified",
    "repository_run_artifact",
    "error_message",
)


@dataclass(frozen=True, slots=True)
class ExperimentRunRecord:
    values: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_record_schema_version": SCHEMA_VERSION,
            **{key: self.values.get(key) for key in RUN_COLUMNS},
        }


@dataclass(frozen=True, slots=True)
class ExperimentAggregate:
    group: str
    metrics: dict[str, Any]


def record_from_result(
    experiment_id: str, spec: ExperimentRunSpec, result: RepositoryRunResult
) -> ExperimentRunRecord:
    if result.stop_reason in {
        "PASS_REACHED",
        "FINAL_TEST_FAILURE",
        "FINAL_TEST_ERROR",
        "FINAL_TEST_TIMEOUT",
    }:
        status = "COMPLETED"
    elif result.stop_reason in {
        "UNSUPPORTED_ENVIRONMENT",
        "REPOSITORY_MUTATION_UNSUPPORTED",
        "INVALID_TARGET",
    }:
        status = "UNSUPPORTED"
    elif result.stop_reason == "ENVIRONMENT_TIMEOUT" or (
        result.stop_reason in {"ENVIRONMENT_ERROR", "MUTATION_ERROR"}
        and any(word in (result.error_message or "").lower() for word in ("timeout", "timed out"))
    ):
        status = "TIMEOUT"
    else:
        status = "PIPELINE_ERROR"
    llm = result.llm_calls
    times = result.timings
    mutation = None
    mutation_path = result.run_directory / "mutation" / "mutation_result.json"
    if mutation_path.is_file():
        mutation = json.loads(mutation_path.read_text(encoding="utf-8"))
    mutation_status = mutation["status"] if mutation else None
    raw_total = mutation.get("tool_total_mutants") if mutation else None
    other = (
        raw_total - result.mutation_killed - result.mutation_survived
        if raw_total is not None
        and result.mutation_killed is not None
        and result.mutation_survived is not None
        else None
    )
    calls = {
        "llm_calls_initial_generation": llm.get("initial_generation"),
        "llm_calls_execution_repair": llm.get("execution_repair"),
        "llm_calls_coverage_generation": llm.get("coverage_generation"),
        "llm_calls_coverage_repair": llm.get("coverage_candidate_repair"),
        "llm_calls_mutation_feedback": llm.get("mutation_feedback"),
        "llm_calls_mutation_repair": llm.get("mutation_candidate_repair"),
    }
    plan_path = result.run_directory / "environment_plan.json"
    version = None
    if plan_path.is_file():
        version = (
            json.loads(plan_path.read_text(encoding="utf-8"))
            .get("selected_python", {})
            .get("version")
        )

    def gain(initial: float | None, final: float | None) -> float | None:
        return final - initial if initial is not None and final is not None else None

    values = {
        "experiment_id": experiment_id,
        "run_id": spec.run_id,
        "repository_id": spec.repository_id,
        "target_id": spec.target_id,
        "target": spec.target,
        "configuration": spec.configuration,
        "repetition": spec.repetition,
        "model": spec.model,
        "temperature": spec.temperature,
        "run_status": status,
        "stop_reason": result.stop_reason,
        "profile_sha256": result.project_profile_sha256,
        "context_status": result.context_status,
        "context_chars": result.context_chars if result.context_status else None,
        "context_files": result.context_files if result.context_status else None,
        "context_items": result.context_items if result.context_status else None,
        "unresolved_references": result.unresolved_references if result.context_status else None,
        "omitted_context_items": result.omitted_items if result.context_status else None,
        "environment_status": result.environment_status,
        "target_python_version": version,
        "initial_execution_status": result.initial_execution_status.value
        if result.initial_execution_status
        else None,
        "final_execution_status": result.final_execution_status.value
        if result.final_execution_status
        else None,
        "repair_attempts": result.repair_count,
        "repair_success": result.repair_success if result.initial_execution_status else None,
        "initial_line_coverage": result.initial_line_coverage,
        "final_line_coverage": result.final_line_coverage,
        "line_coverage_gain": gain(result.initial_line_coverage, result.final_line_coverage),
        "initial_branch_coverage": result.initial_branch_coverage,
        "final_branch_coverage": result.final_branch_coverage,
        "branch_coverage_gain": gain(result.initial_branch_coverage, result.final_branch_coverage),
        "coverage_rounds": result.coverage_rounds,
        "accepted_coverage_rounds": result.accepted_coverage_rounds,
        "mutation_requested": result.mutation_enabled,
        "mutation_feedback_enabled": result.mutation_feedback_enabled,
        "mutation_status": mutation_status,
        "mutation_raw_total": raw_total,
        "mutation_applicable_total": result.mutation_total,
        "mutation_killed": result.mutation_killed,
        "mutation_survived": result.mutation_survived,
        "mutation_other_or_unreported": other,
        "initial_mutation_score": result.mutation_score_initial,
        "final_mutation_score": result.mutation_score_final,
        "mutation_score_gain": gain(result.mutation_score_initial, result.mutation_score_final),
        **calls,
        "llm_calls_total": sum(calls.values())
        if all(value is not None for value in calls.values())
        else None,
        "environment_duration": times.get("environment_preparation_seconds"),
        "generation_duration": times.get("generation_seconds"),
        "execution_duration": times.get("test_execution_seconds"),
        "coverage_duration": times.get("coverage_seconds"),
        "mutation_duration": times.get("mutation_seconds"),
        "total_duration": times.get("total_seconds"),
        "source_integrity_verified": result.source_integrity_verified,
        "accepted_test_integrity_verified": result.accepted_test_integrity_verified,
        "repository_run_artifact": str(result.run_directory / "result.json"),
        "error_message": result.error_message,
    }
    return ExperimentRunRecord(values)


def empty_record(
    experiment_id: str, spec: ExperimentRunSpec, status: str, reason: str
) -> ExperimentRunRecord:
    values = dict.fromkeys(RUN_COLUMNS)
    values.update(
        experiment_id=experiment_id,
        run_id=spec.run_id,
        repository_id=spec.repository_id,
        target_id=spec.target_id,
        target=spec.target,
        configuration=spec.configuration,
        repetition=spec.repetition,
        model=spec.model,
        temperature=spec.temperature,
        run_status=status,
        stop_reason=reason,
        profile_sha256=spec.profile_sha256,
        mutation_requested=spec.options["mutation"],
        mutation_feedback_enabled=spec.options["mutation_feedback"],
    )
    return ExperimentRunRecord(values)


def describe(values: list[float | int | None]) -> dict[str, float | int | None]:
    observed = [float(value) for value in values if value is not None]
    return {
        "n": len(observed),
        "mean": statistics.mean(observed) if observed else None,
        "median": statistics.median(observed) if observed else None,
        "sample_stddev": statistics.stdev(observed) if len(observed) > 1 else None,
        "min": min(observed) if observed else None,
        "max": max(observed) if observed else None,
    }


METRICS = (
    "repair_attempts",
    "initial_line_coverage",
    "final_line_coverage",
    "line_coverage_gain",
    "initial_branch_coverage",
    "final_branch_coverage",
    "branch_coverage_gain",
    "initial_mutation_score",
    "final_mutation_score",
    "mutation_score_gain",
    "mutation_killed",
    "mutation_survived",
    "llm_calls_total",
    "llm_calls_initial_generation",
    "llm_calls_execution_repair",
    "llm_calls_coverage_generation",
    "llm_calls_coverage_repair",
    "llm_calls_mutation_feedback",
    "llm_calls_mutation_repair",
    "environment_duration",
    "generation_duration",
    "execution_duration",
    "coverage_duration",
    "mutation_duration",
    "total_duration",
)


def aggregate(
    records: list[ExperimentRunRecord], *, coverage_target: float = 100.0
) -> dict[str, Any]:
    rows = [item.values for item in records]
    completed = [row for row in rows if row["run_status"] == "COMPLETED"]

    def rate(field: str, predicate) -> dict[str, float | int | None]:
        observed = [row for row in completed if row[field] is not None]
        return {
            "n": len(observed),
            "rate": sum(predicate(row) for row in observed) / len(observed) if observed else None,
        }

    repaired = [
        row
        for row in completed
        if row["repair_attempts"] is not None and row["repair_attempts"] > 0
    ]

    return {
        "planned": len(rows),
        "completed": len(completed),
        "unsupported": sum(row["run_status"] == "UNSUPPORTED" for row in rows),
        "pipeline_errors": sum(row["run_status"] == "PIPELINE_ERROR" for row in rows),
        "timeouts": sum(row["run_status"] == "TIMEOUT" for row in rows),
        "incomplete": sum(row["run_status"] == "INCOMPLETE" for row in rows),
        "initial_pass": rate(
            "initial_execution_status", lambda row: row["initial_execution_status"] == "PASS"
        ),
        "final_pass": rate(
            "final_execution_status", lambda row: row["final_execution_status"] == "PASS"
        ),
        "initial_executable": rate(
            "initial_execution_status",
            lambda row: row["initial_execution_status"] in {"PASS", "FAIL"},
        ),
        "final_executable": rate(
            "final_execution_status", lambda row: row["final_execution_status"] in {"PASS", "FAIL"}
        ),
        "repair_attempted": sum(
            row["repair_attempts"] > 0 for row in completed if row["repair_attempts"] is not None
        ),
        "repair_success": {
            "n": len(repaired),
            "rate": sum(
                row["initial_execution_status"] != "PASS"
                and row["final_execution_status"] == "PASS"
                for row in repaired
            )
            / len(repaired)
            if repaired
            else None,
        },
        "coverage_target_reached": rate(
            "final_line_coverage",
            lambda row: (
                row["final_line_coverage"] >= coverage_target
                and (
                    row["final_branch_coverage"] is None
                    or row["final_branch_coverage"] >= coverage_target
                )
            ),
        ),
        "all_mutants_killed": rate(
            "final_mutation_score", lambda row: row["mutation_survived"] == 0
        ),
        "metrics": {metric: describe([row[metric] for row in completed]) for metric in METRICS},
    }


def grouped_aggregates(
    records: list[ExperimentRunRecord], coverage_target: float
) -> dict[str, Any]:
    rows = sorted(records, key=lambda item: item.values["run_id"])
    configs = sorted(
        {item.values["configuration"] for item in rows},
        key=CONFIGURATIONS.index,
    )
    repos = sorted({item.values["repository_id"] for item in rows})
    targets = sorted({(item.values["repository_id"], item.values["target_id"]) for item in rows})
    return {
        "schema_version": SCHEMA_VERSION,
        "by_configuration": {
            key: aggregate(
                [item for item in rows if item.values["configuration"] == key],
                coverage_target=coverage_target,
            )
            for key in configs
        },
        "by_repository_configuration": {
            repo: {
                config: aggregate(
                    [
                        item
                        for item in rows
                        if item.values["repository_id"] == repo
                        and item.values["configuration"] == config
                    ],
                    coverage_target=coverage_target,
                )
                for config in configs
            }
            for repo in repos
        },
        "by_target_configuration": {
            f"{repo}::{target}": {
                config: aggregate(
                    [
                        item
                        for item in rows
                        if item.values["repository_id"] == repo
                        and item.values["target_id"] == target
                        and item.values["configuration"] == config
                    ],
                    coverage_target=coverage_target,
                )
                for config in configs
            }
            for repo, target in targets
        },
    }


def write_csv(
    path: Path, fieldnames: tuple[str, ...] | list[str], rows: list[dict[str, Any]]
) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

"""Versioned evidence harness and benchmark reports for team workflow comparisons."""

from __future__ import annotations

# DEPS: __future__, agent_orchestrator, dataclasses, json, pathlib, typing
# RESPONSIBILITY: Capture versioned, reportable evidence showing what planning-governed team workflow adds over direct execution.
# MODULE: decision_core
# ---

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_orchestrator.orchestrator import Orchestrator
from agent_orchestrator.policies import OrchestrationMode
from agent_orchestrator.planning import PlanStore, TeamOrchestrator
from agent_orchestrator.run_store import RunStore


EVIDENCE_SCHEMA_VERSION = "1.0"
REPORTABLE_FORMAT = "agent_orchestrator.workflow_evidence.v1"


@dataclass(frozen=True, slots=True)
class WorkflowEvidenceCase:
    requirement: str
    mode: OrchestrationMode = OrchestrationMode.SUCCESS_FIRST
    label: str | None = None
    scenario_type: str | None = None


def benchmark_evidence_cases() -> list[WorkflowEvidenceCase]:
    """Return stable benchmark cases for repeatable evidence reports."""
    return [
        WorkflowEvidenceCase(
            requirement="Build a persisted plan artifact",
            label="persisted_plan_artifact",
            scenario_type="standard",
        ),
        WorkflowEvidenceCase(
            requirement="Build plan with followup checklist",
            label="followup_checklist",
            scenario_type="followup",
        ),
        WorkflowEvidenceCase(
            requirement="Implement auth migration across multiple services",
            label="auth_migration",
            scenario_type="high_risk",
        ),
        WorkflowEvidenceCase(
            requirement="Coordinate parallel independent validation tasks",
            label="parallel_validation",
            scenario_type="parallel",
        ),
    ]


def load_workflow_evidence_cases(path: Path | str) -> list[WorkflowEvidenceCase]:
    """Load workflow evidence cases from a JSON file."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_cases = payload.get("cases", payload) if isinstance(payload, dict) else payload
    if not isinstance(raw_cases, list):
        raise ValueError("evidence case file must contain a list or an object with a cases list")

    cases: list[WorkflowEvidenceCase] = []
    for index, item in enumerate(raw_cases, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"evidence case #{index} must be an object")
        requirement = str(item.get("requirement", "")).strip()
        if not requirement:
            raise ValueError(f"evidence case #{index} is missing requirement")
        mode_value = str(item.get("mode") or OrchestrationMode.SUCCESS_FIRST.value)
        try:
            mode = OrchestrationMode(mode_value)
        except ValueError as exc:
            raise ValueError(f"evidence case #{index} has unsupported mode: {mode_value}") from exc
        cases.append(
            WorkflowEvidenceCase(
                requirement=requirement,
                mode=mode,
                label=str(item.get("label") or requirement),
                scenario_type=str(item.get("scenario_type") or _infer_scenario_type(requirement)),
            )
        )
    return cases


def capture_workflow_evidence(
    requirements: list[str] | list[WorkflowEvidenceCase],
    *,
    project_root: Path | str,
    output_path: Path | str | None = None,
) -> dict[str, object]:
    root = Path(project_root)
    evidence_root = root / ".agent_orchestrator" / "evidence"
    plans_root = evidence_root / "plans"
    team_runs_root = evidence_root / "team-runs"
    direct_runs_root = evidence_root / "direct-runs"

    normalized_cases = _normalize_cases(requirements)
    cases: list[dict[str, object]] = []
    for case in normalized_cases:
        case = _capture_case(
            case,
            project_root=root,
            plans_root=plans_root,
            team_runs_root=team_runs_root,
            direct_runs_root=direct_runs_root,
        )
        cases.append(case)

    payload = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "reportable_format": REPORTABLE_FORMAT,
        "project_root": str(root),
        "cases": cases,
        "report": _build_report(cases),
        "summary": _build_summary(cases),
    }
    if output_path is not None:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def render_workflow_evidence_markdown(payload: dict[str, object]) -> str:
    """Render a compact markdown report from a workflow evidence payload."""
    summary = payload.get("summary", {}) if isinstance(payload.get("summary"), dict) else {}
    report = payload.get("report", {}) if isinstance(payload.get("report"), dict) else {}
    cases = payload.get("cases", []) if isinstance(payload.get("cases"), list) else []
    scenario_aggregates = (
        report.get("scenario_aggregates", {}) if isinstance(report.get("scenario_aggregates"), dict) else {}
    )
    signal_counts = summary.get("signal_counts", {}) if isinstance(summary.get("signal_counts"), dict) else {}

    lines = [
        "# v1.x Evidence Report",
        "",
        "## Summary",
        "",
        f"- schema_version: {payload.get('schema_version', 'unknown')}",
        f"- reportable_format: {payload.get('reportable_format', 'unknown')}",
        f"- case_count: {summary.get('case_count', len(cases))}",
        f"- average_benefit_score: {_format_score(summary.get('average_benefit_score', 0.0))}",
        f"- team_cases_with_execution_run: {summary.get('team_cases_with_execution_run', 0)}",
        f"- direct_runs_without_plan_metadata: {summary.get('direct_runs_without_plan_metadata', 0)}",
        "",
        "## Conclusion Summary",
        "",
        *_evidence_conclusion_lines(summary, report, cases),
        "",
        "## Scenario Aggregates",
        "",
    ]
    if scenario_aggregates:
        for scenario, aggregate in sorted(scenario_aggregates.items()):
            if not isinstance(aggregate, dict):
                continue
            lines.extend(
                [
                    f"- {scenario}: cases={aggregate.get('case_count', 0)}, "
                    f"average_benefit_score={_format_score(aggregate.get('average_benefit_score', 0.0))}, "
                    f"max_benefit_score={aggregate.get('max_benefit_score', 0)}",
                ]
            )
    else:
        lines.append("- none")

    lines.extend(["", "## Signal Counts", ""])
    for key in [
        "provenance_present",
        "provenance_matches_plan_session",
        "recovery_guidance_present",
        "doc_sync_present",
        "fallback_present",
    ]:
        lines.append(f"- {key}: {signal_counts.get(key, 0)}")

    lines.extend(["", "## Cases", ""])
    for case in cases:
        if not isinstance(case, dict):
            continue
        comparison = case.get("comparison", {}) if isinstance(case.get("comparison"), dict) else {}
        lines.append(
            f"- {case.get('label') or case.get('requirement')}: "
            f"scenario={case.get('scenario_type', 'unknown')}, "
            f"benefit_score={comparison.get('benefit_score', 0)}"
        )
    lines.extend(
        [
            "",
            "## Takeaways",
            "",
            f"- governance-first cases surfaced {summary.get('team_cases_with_execution_run', 0)} linked execution runs out of {summary.get('case_count', len(cases))} cases.",
            f"- direct runs without plan metadata: {summary.get('direct_runs_without_plan_metadata', 0)}.",
            "- when provenance, recovery guidance, and doc sync appear together, the workflow is easier to explain than a fixed-template run.",
        ]
    )
    lines.append("")
    return "\n".join(lines)


def write_workflow_evidence_markdown(payload: dict[str, object], output_path: Path | str) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_workflow_evidence_markdown(payload), encoding="utf-8")
    return path


def compare_workflow_evidence(baseline: dict[str, object], current: dict[str, object]) -> dict[str, object]:
    """Build a schema-preserving comparison layer for two evidence captures."""
    baseline_summary = baseline.get("summary", {}) if isinstance(baseline.get("summary"), dict) else {}
    current_summary = current.get("summary", {}) if isinstance(current.get("summary"), dict) else {}
    baseline_report = baseline.get("report", {}) if isinstance(baseline.get("report"), dict) else {}
    current_report = current.get("report", {}) if isinstance(current.get("report"), dict) else {}
    baseline_cases = baseline.get("cases", []) if isinstance(baseline.get("cases"), list) else []
    current_cases = current.get("cases", []) if isinstance(current.get("cases"), list) else []
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "reportable_format": f"{REPORTABLE_FORMAT}.trend",
        "baseline": _comparison_snapshot(baseline_summary, baseline_report, baseline_cases),
        "current": _comparison_snapshot(current_summary, current_report, current_cases),
        "deltas": {
            "case_count": _number_delta(baseline_summary.get("case_count"), current_summary.get("case_count")),
            "average_benefit_score": _number_delta(
                baseline_summary.get("average_benefit_score"),
                current_summary.get("average_benefit_score"),
            ),
            "team_cases_with_execution_run": _number_delta(
                baseline_summary.get("team_cases_with_execution_run"),
                current_summary.get("team_cases_with_execution_run"),
            ),
            "direct_runs_without_plan_metadata": _number_delta(
                baseline_summary.get("direct_runs_without_plan_metadata"),
                current_summary.get("direct_runs_without_plan_metadata"),
            ),
            "signal_counts": _count_deltas(
                baseline_summary.get("signal_counts", {}),
                current_summary.get("signal_counts", {}),
            ),
            "scenario_aggregates": _scenario_deltas(
                baseline_report.get("scenario_aggregates", {}),
                current_report.get("scenario_aggregates", {}),
            ),
            "team_advantage_counts": _count_deltas(
                _case_tag_counts(baseline_cases, "team_advantages"),
                _case_tag_counts(current_cases, "team_advantages"),
            ),
            "direct_limitation_counts": _count_deltas(
                _case_tag_counts(baseline_cases, "direct_limitations"),
                _case_tag_counts(current_cases, "direct_limitations"),
            ),
        },
    }


def load_workflow_evidence_payload(path: Path | str) -> dict[str, object]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("evidence payload must be a JSON object")
    return payload


def render_workflow_evidence_trend_markdown(payload: dict[str, object]) -> str:
    """Render a compact markdown trend report from a comparison payload."""
    baseline = payload.get("baseline", {}) if isinstance(payload.get("baseline"), dict) else {}
    current = payload.get("current", {}) if isinstance(payload.get("current"), dict) else {}
    deltas = payload.get("deltas", {}) if isinstance(payload.get("deltas"), dict) else {}
    assessment = _trend_assessment(deltas)
    lines = [
        "# v1.x Evidence Trend",
        "",
        "## Summary",
        "",
        f"- baseline_cases: {baseline.get('case_count', 0)}",
        f"- current_cases: {current.get('case_count', 0)}",
        f"- average_benefit_score_delta: {_format_signed(deltas.get('average_benefit_score', 0.0))}",
        f"- execution_run_delta: {_format_signed(deltas.get('team_cases_with_execution_run', 0))}",
        f"- direct_without_plan_metadata_delta: {_format_signed(deltas.get('direct_runs_without_plan_metadata', 0))}",
        f"- current_version_assessment: {assessment}",
        "",
        "## Version Assessment",
        "",
        *_trend_assessment_lines(assessment, deltas),
        "",
        "## Scenario Aggregates",
        "",
    ]
    scenario_deltas = deltas.get("scenario_aggregates", {}) if isinstance(deltas.get("scenario_aggregates"), dict) else {}
    if scenario_deltas:
        for scenario, aggregate in sorted(scenario_deltas.items()):
            if not isinstance(aggregate, dict):
                continue
            lines.append(
                f"- {scenario}: cases_delta={_format_signed(aggregate.get('case_count', 0))}, "
                f"average_benefit_score_delta={_format_signed(aggregate.get('average_benefit_score', 0.0))}, "
                f"max_benefit_score_delta={_format_signed(aggregate.get('max_benefit_score', 0))}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Signal Deltas", ""])
    lines.extend(_count_delta_lines(deltas.get("signal_counts", {})))
    lines.extend(["", "## Team Advantage Deltas", ""])
    lines.extend(_count_delta_lines(deltas.get("team_advantage_counts", {})))
    lines.extend(["", "## Direct Limitation Deltas", ""])
    lines.extend(_count_delta_lines(deltas.get("direct_limitation_counts", {})))
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- positive score, execution-run, and team-advantage deltas favor the current capture; flat deltas mean the comparison shape stayed stable.",
            "- treat team advantage deltas and direct limitation deltas together when judging whether governance-first orchestration is improving.",
        ]
    )
    lines.append("")
    return "\n".join(lines)


def write_workflow_evidence_trend_markdown(payload: dict[str, object], output_path: Path | str) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_workflow_evidence_trend_markdown(payload), encoding="utf-8")
    return path


def _capture_case(
    case: WorkflowEvidenceCase,
    *,
    project_root: Path,
    plans_root: Path,
    team_runs_root: Path,
    direct_runs_root: Path,
) -> dict[str, object]:
    direct_orchestrator = Orchestrator(run_store=RunStore(root=direct_runs_root))
    direct_run = direct_orchestrator.run(case.requirement, case.mode)
    direct_payload = direct_run.to_dict()

    team_orchestrator = Orchestrator(run_store=RunStore(root=team_runs_root))
    team = TeamOrchestrator(
        orchestrator=team_orchestrator,
        store=PlanStore(root=plans_root),
        project_root=project_root,
    )
    session = team.start(case.requirement)
    session = team.mark_draft_ready(session.id)
    session = team.submit_draft_for_review(session.id)
    required_open = [gap.id for gap in session.gaps if gap.required and gap.status != "closed"]
    if required_open:
        session.status = "needs_revision"
        session.gate_verdict = "needs_revision"
        session.resume.current_phase = "in_review"
        session.resume.pending_role = "lead"
        team.store.write_session(session)
        session = team.revise(session.id, summary="Evidence capture closes required review gaps.", closed_gap_ids=required_open)
    if session.status != "approved_for_execution":
        session = team.approve(session.id)
    executed = None
    execution_payload: dict[str, object] | None = None
    if session.status == "approved_for_execution":
        executed = team.execute(session.id, case.mode)
        if executed.resume.linked_execution_run_id:
            execution_payload = team_orchestrator.run_store.read(executed.resume.linked_execution_run_id)

    team_session = executed or session
    team_summary = team_session.to_dict()["status_summary"]
    approved_plan = team_session.approved_plan if isinstance(team_session.approved_plan, dict) else {}
    selected_provider_runtime = (
        team_session.decision_verdict.selected_provider_runtime
        if team_session.decision_verdict is not None
        else {}
    )
    selected_topology = (
        team_session.decision_verdict.selected_topology
        if team_session.decision_verdict is not None
        else None
    )
    provenance = {}
    if isinstance(execution_payload, dict):
        metadata = execution_payload.get("metadata", {})
        if isinstance(metadata, dict) and isinstance(metadata.get("provenance"), dict):
            provenance = dict(metadata["provenance"])

    signals = _build_signals(
        team_session=team_session,
        status_summary=team_summary,
        approved_plan=approved_plan,
        provenance=provenance,
        selected_provider_runtime=selected_provider_runtime,
    )
    team_advantages = _team_advantages(team_session, team_summary, approved_plan, provenance, signals)
    direct_limitations = _direct_limitations(direct_payload)
    benefit_score = len(team_advantages) + len(direct_limitations)
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "label": case.label or case.requirement,
        "requirement": case.requirement,
        "mode": case.mode.value,
        "scenario_type": case.scenario_type or _infer_scenario_type(case.requirement),
        "direct_run": {
            "run_id": direct_run.run_id,
            "accepted": direct_run.accepted,
            "final_state": direct_run.final_state,
            "job_count": len(direct_run.jobs),
            "attempt_count": len(direct_run.attempts),
            "final_mode": direct_run.final_mode.value,
            "has_approved_plan_metadata": bool(
                isinstance(direct_payload.get("metadata"), dict)
                and direct_payload["metadata"].get("approved_plan")
            ),
        },
        "team_workflow": {
            "session_id": team_session.id,
            "status": team_session.status,
            "linked_execution_run_id": team_session.resume.linked_execution_run_id,
            "review_round_count": len(team_session.review_rounds),
            "approved_plan_source": approved_plan.get("execution_contract", {}).get("source")
            if isinstance(approved_plan.get("execution_contract"), dict)
            else None,
            "selected_topology": selected_topology,
            "selected_provider_runtime": selected_provider_runtime,
            "next_actions": list(team_summary.get("next_actions", [])),
            "recommended_commands": list(team_summary.get("recommended_commands", [])),
            "recovery_actions": list(team_summary.get("recovery_actions", [])),
            "execution_provenance_keys": sorted(provenance.keys()),
            "approval_state": team_summary.get("approval_state", {}),
            "runtime_health": team_summary.get("runtime_health", {}),
            "usage_cost": team_summary.get("usage_cost", {}),
        },
        "signals": signals,
        "comparison": {
            "team_advantages": team_advantages,
            "direct_limitations": direct_limitations,
            "benefit_score": benefit_score,
            "team_outcome_better_documented": bool(team_advantages or direct_limitations),
        },
    }


def _comparison_snapshot(
    summary: dict[str, object],
    report: dict[str, object],
    cases: list[object],
) -> dict[str, object]:
    return {
        "case_count": int(summary.get("case_count", len(cases)) or 0),
        "average_benefit_score": float(summary.get("average_benefit_score", 0.0) or 0.0),
        "team_cases_with_execution_run": int(summary.get("team_cases_with_execution_run", 0) or 0),
        "direct_runs_without_plan_metadata": int(summary.get("direct_runs_without_plan_metadata", 0) or 0),
        "signal_counts": summary.get("signal_counts", {}) if isinstance(summary.get("signal_counts"), dict) else {},
        "scenario_aggregates": report.get("scenario_aggregates", {}) if isinstance(report.get("scenario_aggregates"), dict) else {},
        "team_advantage_counts": _case_tag_counts(cases, "team_advantages"),
        "direct_limitation_counts": _case_tag_counts(cases, "direct_limitations"),
    }


def _evidence_conclusion_lines(
    summary: dict[str, object],
    report: dict[str, object],
    cases: list[object],
) -> list[str]:
    case_count = int(summary.get("case_count", len(cases)) or 0)
    signal_counts = summary.get("signal_counts", {}) if isinstance(summary.get("signal_counts"), dict) else {}
    scenario_counts = report.get("scenario_type_counts", {}) if isinstance(report.get("scenario_type_counts"), dict) else {}
    scenario_names = ", ".join(sorted(str(name) for name in scenario_counts)) if scenario_counts else "none"
    approved_plan_count = int(summary.get("cases_showing_approved_plan_benefit", 0) or 0)
    recovery_count = int(signal_counts.get("recovery_guidance_present", 0) or 0)
    provenance_count = int(signal_counts.get("provenance_matches_plan_session", 0) or 0)
    fallback_count = int(signal_counts.get("fallback_present", 0) or 0)
    direct_without_plan = int(summary.get("direct_runs_without_plan_metadata", 0) or 0)
    return [
        f"- planning_quality: {approved_plan_count}/{case_count} cases produced an approved plan artifact across scenarios: {scenario_names}.",
        f"- rescue_quality: {recovery_count}/{case_count} cases carried next-step or recovery guidance for the operator.",
        f"- runtime_limitation: {fallback_count}/{case_count} cases showed provider fallback signals; v1.x evidence validates command-runtime selection/provenance, not a full provider bridge or persistent session manager.",
        f"- fixed_template_advantage: {provenance_count}/{case_count} cases matched execution provenance to the plan session while {direct_without_plan} direct runs lacked approved-plan metadata.",
    ]


def _trend_assessment(deltas: dict[str, object]) -> str:
    score_delta = float(deltas.get("average_benefit_score", 0.0) or 0.0)
    execution_delta = float(deltas.get("team_cases_with_execution_run", 0) or 0)
    advantage_delta = _positive_delta_total(deltas.get("team_advantage_counts", {}))
    negative_score = score_delta < 0
    negative_execution = execution_delta < 0
    if negative_score or negative_execution:
        return "mixed_or_regressed"
    if score_delta > 0 or execution_delta > 0 or advantage_delta > 0:
        return "better"
    return "stable"


def _trend_assessment_lines(assessment: str, deltas: dict[str, object]) -> list[str]:
    score_delta = deltas.get("average_benefit_score", 0.0)
    execution_delta = deltas.get("team_cases_with_execution_run", 0)
    advantage_delta = _positive_delta_total(deltas.get("team_advantage_counts", {}))
    limitation_delta = deltas.get("direct_runs_without_plan_metadata", 0)
    if assessment == "better":
        verdict = "current_is_better: yes"
    elif assessment == "stable":
        verdict = "current_is_better: no measurable improvement; no regression detected"
    else:
        verdict = "current_is_better: mixed; inspect negative deltas before release"
    return [
        f"- {verdict}",
        f"- improvement_signals: average_benefit_score_delta={_format_signed(score_delta)}, execution_run_delta={_format_signed(execution_delta)}, positive_team_advantage_delta={_format_signed(advantage_delta)}.",
        f"- limitation_signals: direct_without_plan_metadata_delta={_format_signed(limitation_delta)}; compare this with case_count_delta before treating it as a regression.",
    ]


def _positive_delta_total(value: object) -> int:
    counts = value if isinstance(value, dict) else {}
    return sum(max(int(item or 0), 0) for item in counts.values())


def _number_delta(baseline: object, current: object) -> float | int:
    baseline_value = float(baseline or 0)
    current_value = float(current or 0)
    delta = current_value - baseline_value
    return int(delta) if delta.is_integer() else delta


def _count_deltas(baseline: object, current: object) -> dict[str, int]:
    baseline_counts = baseline if isinstance(baseline, dict) else {}
    current_counts = current if isinstance(current, dict) else {}
    keys = sorted({str(key) for key in baseline_counts} | {str(key) for key in current_counts})
    return {
        key: int(current_counts.get(key, 0) or 0) - int(baseline_counts.get(key, 0) or 0)
        for key in keys
    }


def _scenario_deltas(baseline: object, current: object) -> dict[str, dict[str, object]]:
    baseline_aggregates = baseline if isinstance(baseline, dict) else {}
    current_aggregates = current if isinstance(current, dict) else {}
    scenarios = sorted({str(key) for key in baseline_aggregates} | {str(key) for key in current_aggregates})
    deltas: dict[str, dict[str, object]] = {}
    for scenario in scenarios:
        baseline_item = baseline_aggregates.get(scenario, {}) if isinstance(baseline_aggregates.get(scenario), dict) else {}
        current_item = current_aggregates.get(scenario, {}) if isinstance(current_aggregates.get(scenario), dict) else {}
        deltas[scenario] = {
            "case_count": _number_delta(baseline_item.get("case_count"), current_item.get("case_count")),
            "average_benefit_score": _number_delta(
                baseline_item.get("average_benefit_score"),
                current_item.get("average_benefit_score"),
            ),
            "max_benefit_score": _number_delta(
                baseline_item.get("max_benefit_score"),
                current_item.get("max_benefit_score"),
            ),
            "signal_counts": _count_deltas(
                baseline_item.get("signal_counts", {}),
                current_item.get("signal_counts", {}),
            ),
        }
    return deltas


def _case_tag_counts(cases: list[object], key: str) -> dict[str, int]:
    comparisons = [
        case.get("comparison", {})
        for case in cases
        if isinstance(case, dict) and isinstance(case.get("comparison", {}), dict)
    ]
    return _tag_counts(comparisons, key)


def _normalize_cases(requirements: list[str] | list[WorkflowEvidenceCase]) -> list[WorkflowEvidenceCase]:
    normalized: list[WorkflowEvidenceCase] = []
    for item in requirements:
        if isinstance(item, WorkflowEvidenceCase):
            normalized.append(item)
        else:
            normalized.append(WorkflowEvidenceCase(requirement=str(item)))
    return normalized


def _build_signals(
    *,
    team_session: Any,
    status_summary: dict[str, object],
    approved_plan: dict[str, object],
    provenance: dict[str, object],
    selected_provider_runtime: dict[str, object],
) -> dict[str, object]:
    doc_sync = team_session.doc_sync if isinstance(getattr(team_session, "doc_sync", None), dict) else {}
    compliance = team_session.compliance if isinstance(getattr(team_session, "compliance", None), dict) else {}
    recommended_commands = [str(item) for item in status_summary.get("recommended_commands", [])]
    recovery_actions = [str(item) for item in status_summary.get("recovery_actions", [])]
    fallback = _fallback_signals(status_summary, selected_provider_runtime)
    return {
        "provenance": {
            "present": bool(provenance),
            "matches_plan_session": provenance.get("plan_session_id") == team_session.id,
            "keys": sorted(provenance.keys()),
            "linked_execution_run_id": team_session.resume.linked_execution_run_id,
            "approved_plan_goal": provenance.get("approved_plan_goal"),
            "source_requirement": provenance.get("source_requirement"),
        },
        "recovery": {
            "has_guidance": bool(recommended_commands or recovery_actions),
            "actions": recovery_actions,
            "recommended_commands": recommended_commands,
            "block_source": status_summary.get("block_source"),
            "block_detail": status_summary.get("block_detail"),
            "resume_action": status_summary.get("resume_action"),
            "resume_reason": status_summary.get("resume_reason"),
        },
        "doc_sync": {
            "present": bool(doc_sync),
            "status": _doc_sync_signal_status(doc_sync, compliance),
            "missing_doc_count": _list_count(doc_sync.get("missing_docs")) if doc_sync else 0,
            "stale_doc_count": _list_count(doc_sync.get("stale_docs")) if doc_sync else 0,
            "changed_file_violation_count": _list_count(doc_sync.get("changed_file_doc_sync_violations"))
            if doc_sync
            else 0,
            "warning_count": _list_count(compliance.get("warnings")) if compliance else 0,
            "blocking_reason_count": _list_count(compliance.get("blocking_reasons")) if compliance else 0,
            "approved_plan_source": approved_plan.get("execution_contract", {}).get("source")
            if isinstance(approved_plan.get("execution_contract"), dict)
            else None,
        },
        "fallback": fallback,
    }


def _doc_sync_signal_status(doc_sync: dict[str, object], compliance: dict[str, object]) -> str:
    if not doc_sync:
        return "not_recorded"
    if compliance.get("blocking"):
        return "blocking"
    return "passed"


def _list_count(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


def _fallback_signals(
    status_summary: dict[str, object],
    selected_provider_runtime: dict[str, object],
) -> dict[str, object]:
    fields = {
        "author_fallback_from": selected_provider_runtime.get("author_fallback_from"),
        "author_fallback_reason": selected_provider_runtime.get("author_fallback_reason"),
        "author_fallback_detail": selected_provider_runtime.get("author_fallback_detail"),
        "reviewer_fallback_from": selected_provider_runtime.get("fallback_from"),
        "reviewer_fallback_reason": selected_provider_runtime.get("fallback_reason"),
        "reviewer_fallback_detail": selected_provider_runtime.get("fallback_detail"),
        "recovery_provider_fallback_from": status_summary.get("recovery_provider_fallback_from"),
        "recovery_provider_fallback_reason": status_summary.get("recovery_provider_fallback_reason"),
        "recovery_provider_fallback_detail": status_summary.get("recovery_provider_fallback_detail"),
    }
    return {
        "present": any(value for value in fields.values()),
        "selected_provider_runtime": dict(selected_provider_runtime),
        "recovery_provider": status_summary.get("recovery_provider"),
        "recovery_round_type": status_summary.get("recovery_round_type"),
        **fields,
    }


def _team_advantages(
    session: Any,
    status_summary: dict[str, object],
    approved_plan: dict[str, object],
    provenance: dict[str, object],
    signals: dict[str, object],
) -> list[str]:
    advantages: list[str] = []
    if approved_plan:
        advantages.append("approved_plan_artifact")
    if session.resume.linked_execution_run_id:
        advantages.append("linked_execution_run")
    if provenance.get("plan_session_id") == session.id:
        advantages.append("execution_provenance")
    if status_summary.get("recommended_commands"):
        advantages.append("recovery_guidance")
    if status_summary.get("next_executable_task"):
        advantages.append("task_next_visibility")
    if status_summary.get("approval_state"):
        advantages.append("approval_observability")
    if status_summary.get("execution_context_policy"):
        advantages.append("fresh_resume_policy")
    if status_summary.get("usage_cost"):
        advantages.append("usage_cost_placeholder")
    if getattr(session, "id", None):
        advantages.append("knowledge_artifacts")
    if session.review_rounds:
        advantages.append("role_contract_enforced")
    if session.decision_verdict is not None and session.decision_verdict.selected_provider_runtime:
        advantages.append("provider_runtime_selection")
        selected = session.decision_verdict.selected_provider_runtime
        if any(str(selected.get(key)) == "direct_api" for key in ("reviewer_runtime_mode", "adversarial_reviewer_runtime_mode")):
            advantages.append("direct_api_governance_roles")
        if str(selected.get("author_runtime_mode")) == "cli_inherit":
            advantages.append("cli_worker_default_preserved")
    doc_sync = signals.get("doc_sync", {}) if isinstance(signals.get("doc_sync"), dict) else {}
    if doc_sync.get("present"):
        advantages.append("doc_sync_snapshot")
    fallback = signals.get("fallback", {}) if isinstance(signals.get("fallback"), dict) else {}
    if "selected_provider_runtime" in fallback:
        advantages.append("fallback_signal_surface")
    return advantages


def _direct_limitations(payload: dict[str, object]) -> list[str]:
    limitations: list[str] = []
    metadata = payload.get("metadata", {}) if isinstance(payload.get("metadata"), dict) else {}
    if not metadata.get("approved_plan"):
        limitations.append("no_approved_plan_artifact")
    provenance = metadata.get("provenance", {}) if isinstance(metadata.get("provenance"), dict) else {}
    if "plan_session_id" not in provenance:
        limitations.append("no_plan_session_provenance")
    return limitations


def _build_summary(cases: list[dict[str, object]]) -> dict[str, object]:
    workflow_cases = [case.get("team_workflow", {}) for case in cases if isinstance(case, dict)]
    comparisons = [case.get("comparison", {}) for case in cases if isinstance(case, dict)]
    direct_runs = [case.get("direct_run", {}) for case in cases if isinstance(case, dict)]
    signals = [case.get("signals", {}) for case in cases if isinstance(case, dict)]
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "reportable_format": REPORTABLE_FORMAT,
        "case_count": len(cases),
        "team_cases_with_execution_run": sum(
            1 for item in workflow_cases if isinstance(item, dict) and item.get("linked_execution_run_id")
        ),
        "team_cases_with_provenance": sum(
            1
            for item in workflow_cases
            if isinstance(item, dict) and "plan_session_id" in list(item.get("execution_provenance_keys", []))
        ),
        "cases_showing_approved_plan_benefit": sum(
            1
            for item in comparisons
            if isinstance(item, dict) and "approved_plan_artifact" in list(item.get("team_advantages", []))
        ),
        "direct_runs_without_plan_metadata": sum(
            1 for item in direct_runs if isinstance(item, dict) and not item.get("has_approved_plan_metadata")
        ),
        "average_benefit_score": (
            sum(int(item.get("benefit_score", 0)) for item in comparisons if isinstance(item, dict)) / len(comparisons)
            if comparisons
            else 0.0
        ),
        "signal_counts": _signal_counts(signals),
        "reference_advantage_counts": _tag_counts(comparisons, "team_advantages"),
    }


def _build_report(cases: list[dict[str, object]]) -> dict[str, object]:
    direct_runs = [case.get("direct_run", {}) for case in cases if isinstance(case, dict)]
    team_runs = [case.get("team_workflow", {}) for case in cases if isinstance(case, dict)]
    comparisons = [case.get("comparison", {}) for case in cases if isinstance(case, dict)]
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "format": REPORTABLE_FORMAT,
        "team_status_counts": _status_counts(team_runs, "status"),
        "direct_final_state_counts": _status_counts(direct_runs, "final_state"),
        "scenario_type_counts": _status_counts(cases, "scenario_type"),
        "scenario_aggregates": _scenario_aggregates(cases),
        "benefit_score_by_case": {
            str(case.get("label") or case.get("requirement")): int(case.get("comparison", {}).get("benefit_score", 0))
            for case in cases
            if isinstance(case, dict)
        },
        "average_benefit_score_by_scenario": _average_benefit_score_by_scenario(cases),
        "max_benefit_score": max(
            (int(item.get("benefit_score", 0)) for item in comparisons if isinstance(item, dict)),
            default=0,
        ),
        "cases_with_recovery_guidance": sum(
            1
            for item in comparisons
            if isinstance(item, dict) and "recovery_guidance" in list(item.get("team_advantages", []))
        ),
    }


def _signal_counts(signals: list[object]) -> dict[str, int]:
    counts = {
        "provenance_present": 0,
        "provenance_matches_plan_session": 0,
        "recovery_guidance_present": 0,
        "doc_sync_present": 0,
        "fallback_present": 0,
    }
    for item in signals:
        if not isinstance(item, dict):
            continue
        provenance = item.get("provenance", {}) if isinstance(item.get("provenance"), dict) else {}
        recovery = item.get("recovery", {}) if isinstance(item.get("recovery"), dict) else {}
        doc_sync = item.get("doc_sync", {}) if isinstance(item.get("doc_sync"), dict) else {}
        fallback = item.get("fallback", {}) if isinstance(item.get("fallback"), dict) else {}
        if provenance.get("present"):
            counts["provenance_present"] += 1
        if provenance.get("matches_plan_session"):
            counts["provenance_matches_plan_session"] += 1
        if recovery.get("has_guidance"):
            counts["recovery_guidance_present"] += 1
        if doc_sync.get("present"):
            counts["doc_sync_present"] += 1
        if fallback.get("present"):
            counts["fallback_present"] += 1
    return counts


def _scenario_aggregates(cases: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for case in cases:
        if isinstance(case, dict):
            grouped.setdefault(str(case.get("scenario_type") or "unknown"), []).append(case)

    aggregates: dict[str, dict[str, object]] = {}
    for scenario, scenario_cases in grouped.items():
        comparisons = [
            case.get("comparison", {})
            for case in scenario_cases
            if isinstance(case.get("comparison", {}), dict)
        ]
        scores = [int(item.get("benefit_score", 0)) for item in comparisons]
        aggregates[scenario] = {
            "case_count": len(scenario_cases),
            "average_benefit_score": sum(scores) / len(scores) if scores else 0.0,
            "max_benefit_score": max(scores, default=0),
            "signal_counts": _signal_counts([case.get("signals", {}) for case in scenario_cases]),
            "team_advantage_counts": _tag_counts(comparisons, "team_advantages"),
            "direct_limitation_counts": _tag_counts(comparisons, "direct_limitations"),
        }
    return aggregates


def _tag_counts(items: list[dict[str, object]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        values = item.get(key, []) if isinstance(item, dict) else []
        if not isinstance(values, list):
            continue
        for value in values:
            name = str(value)
            counts[name] = counts.get(name, 0) + 1
    return counts


def _status_counts(items: list[dict[str, object]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get(key)
        name = str(value or "unknown")
        counts[name] = counts.get(name, 0) + 1
    return counts


def _infer_scenario_type(requirement: str) -> str:
    lowered = requirement.lower()
    if "followup" in lowered:
        return "followup"
    if "auth" in lowered or "migration" in lowered or "security" in lowered:
        return "high_risk"
    if "parallel" in lowered or "independent" in lowered or "multiple" in lowered:
        return "parallel"
    return "standard"


def _average_benefit_score_by_scenario(cases: list[dict[str, object]]) -> dict[str, float]:
    buckets: dict[str, list[int]] = {}
    for case in cases:
        if not isinstance(case, dict):
            continue
        scenario = str(case.get("scenario_type", "unknown"))
        comparison = case.get("comparison", {})
        if not isinstance(comparison, dict):
            continue
        buckets.setdefault(scenario, []).append(int(comparison.get("benefit_score", 0)))
    return {
        scenario: sum(scores) / len(scores)
        for scenario, scores in buckets.items()
        if scores
    }


def _format_score(value: object) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _format_signed(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if number.is_integer():
        return f"{int(number):+d}"
    return f"{number:+.2f}"


def _count_delta_lines(value: object) -> list[str]:
    counts = value if isinstance(value, dict) else {}
    if not counts:
        return ["- none"]
    return [f"- {key}: {_format_signed(counts[key])}" for key in sorted(counts)]

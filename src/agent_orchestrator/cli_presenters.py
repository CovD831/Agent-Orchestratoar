"""Formatting helpers for CLI session and execution output."""

from __future__ import annotations

import json
from typing import Any


def status_summary(payload: dict[str, object]) -> dict[str, object]:
    summary = payload.get("status_summary", {})
    return summary if isinstance(summary, dict) else {}


def blocker_summary(payload: dict[str, object]) -> dict[str, object]:
    summary = payload.get("blocker_summary", {})
    return summary if isinstance(summary, dict) else {}


def execution_session_summary(payload: dict[str, object]) -> dict[str, object]:
    summary = payload.get("session_summary", {})
    return summary if isinstance(summary, dict) else {}


def summary_list(summary: dict[str, object], key: str) -> list[object]:
    value = summary.get(key, [])
    return value if isinstance(value, list) else []


def summary_text(summary: dict[str, object], key: str, default: str = "") -> str:
    value = summary.get(key, default)
    return default if value is None else str(value)


def summary_bool(summary: dict[str, object], key: str, default: bool = False) -> bool:
    value = summary.get(key, default)
    return bool(value)


def summary_dict(summary: dict[str, object], key: str) -> dict[str, object]:
    value = summary.get(key, {})
    return value if isinstance(value, dict) else {}


def recovery_category(summary: dict[str, object]) -> str:
    semantics = summary_dict(summary, "recovery_semantics")
    category = summary_text(semantics, "category")
    if category:
        return category

    action = summary_text(summary, "resume_action") or summary_text(summary, "primary_action")
    block_source = summary_text(summary, "block_source")
    if action in {"retry_review", "retry_adversarial_review"}:
        return "retry"
    if action in {"approve", "execute"}:
        return "resume"
    if action == "human_decision":
        return "escalate"
    if block_source == "execution_run":
        return "inspect_before_rerun"
    recovery_actions = {str(action) for action in summary_list(summary, "recovery_actions")}
    if recovery_actions & {"retry_review", "retry_adversarial_review"}:
        return "retry"
    if "human_decision" in recovery_actions:
        return "escalate"
    if "inspect_execution" in recovery_actions and "inspect_blockers" in recovery_actions:
        return "inspect_before_rerun"
    if any(action.startswith("inspect_") for action in recovery_actions):
        return "inspect"
    if action in {"inspect_compliance", "inspect_blockers", "inspect_delegated_job", "inspect_execution"}:
        return "inspect"
    return ""


def recovery_action_description(action: str) -> str:
    descriptions = {
        "inspect_delegated_job": "inspect failed delegated job evidence",
        "retry_review": "retry delegated review",
        "retry_adversarial_review": "retry delegated adversarial review",
        "revise_plan": "revise plan or escalate manually if retry is unsafe",
        "revise": "revise plan and close required gaps",
        "approve": "resume by approving the reviewed plan",
        "execute": "resume by running approved execution",
        "inspect_execution": "inspect linked execution run before resume or re-run",
        "inspect_blockers": "inspect blockers before resume or re-run",
        "inspect_compliance": "inspect compliance blockers or warnings",
        "human_decision": "escalate for human decision",
        "wait_for_execution": "wait or inspect the linked run",
        "inspect_session": "inspect session state",
    }
    return descriptions.get(action, "inspect session state before continuing")


def recovery_guidance(summary: dict[str, object]) -> str:
    category = recovery_category(summary)
    recovery_actions = summary_list(summary, "recovery_actions")
    resume_action = summary_text(summary, "resume_action") or summary_text(summary, "primary_action")
    if not category and not recovery_actions and not resume_action:
        return ""

    mode_labels = {
        "inspect_before_rerun": "re-run",
        "retry": "retry",
        "resume": "resume",
        "escalate": "escalate",
        "inspect": "inspect",
        "manual": "manual",
    }
    mode = mode_labels.get(category, category or "manual")
    parts = [f"mode={mode}"]
    if resume_action:
        parts.append(f"resume_action={resume_action}")
    resume_reason = summary_text(summary, "resume_reason")
    if resume_reason:
        parts.append(f"reason={resume_reason}")
    block_source = summary_text(summary, "block_source")
    block_detail = summary_text(summary, "block_detail")
    if block_source and block_detail:
        parts.append(f"block={block_source}/{block_detail}")
    elif block_source:
        parts.append(f"block={block_source}")

    semantics = summary_dict(summary, "recovery_semantics")
    if semantics:
        if "auto_apply_allowed" in semantics:
            parts.append(f"auto_apply={'yes' if summary_bool(semantics, 'auto_apply_allowed') else 'no'}")
        if summary_bool(semantics, "human_escalation_required"):
            parts.append("human_escalation_required=yes")
    elif category == "escalate":
        parts.append("human_escalation_required=yes")
    if category == "inspect_before_rerun":
        parts.append("inspect before re-running execution")
    return "; ".join(parts)


def recovery_steps(summary: dict[str, object]) -> str:
    actions = [str(action) for action in summary_list(summary, "recovery_actions")]
    if not actions and recovery_category(summary):
        action = summary_text(summary, "resume_action") or summary_text(summary, "primary_action")
        if action:
            actions = [action]
    if not actions:
        return ""
    return " -> ".join(f"{action}={recovery_action_description(action)}" for action in actions)


def print_recovery_details(
    summary: dict[str, object],
    *,
    include_commands: bool = False,
    include_resume: bool = True,
) -> None:
    resume_action = summary_text(summary, "resume_action")
    resume_reason = summary_text(summary, "resume_reason")
    if include_resume and resume_action:
        detail = f"resume: {resume_action}"
        if resume_reason:
            detail += f" (reason={resume_reason})"
        print(detail)

    guidance = recovery_guidance(summary)
    if guidance:
        print(f"recovery_guidance: {guidance}")
    steps = recovery_steps(summary)
    if steps:
        print(f"recovery_steps: {steps}")
    if include_commands:
        commands = summary_list(summary, "recommended_commands")
        if commands:
            print(f"recovery_commands: {' | '.join(str(command) for command in commands)}")


def team_display_context(payload: dict[str, object], *, pick_primary_action: Any) -> dict[str, object]:
    summary = status_summary(payload)
    delegated_jobs = summary_list(summary, "delegated_jobs")
    failed_jobs = [
        job
        for job in delegated_jobs
        if isinstance(job, dict) and str(job.get("status")) == "failed"
    ]
    next_actions = [str(action) for action in summary_list(summary, "next_actions")]
    primary_action = summary_text(summary, "primary_action") or pick_primary_action(next_actions)
    primary_reason = summary_text(summary, "primary_reason") or summary_text(
        summary,
        "next_action_message",
        "inspect the current session state before continuing",
    )
    recommended_commands = [str(command) for command in summary_list(summary, "recommended_commands")]
    return {
        "status_summary": summary,
        "delegated_jobs": delegated_jobs,
        "failed_jobs": failed_jobs,
        "next_actions": next_actions,
        "primary_action": primary_action,
        "primary_reason": primary_reason,
        "recommended_commands": recommended_commands,
    }


def team_next_alternatives(status_summary: dict[str, object], primary_action: str) -> list[str]:
    recovery_actions = [str(action) for action in summary_list(status_summary, "recovery_actions")]
    return [action for action in recovery_actions if action != primary_action]


def pick_primary_action(actions: list[str]) -> str:
    priority = [
        "inspect_delegated_job",
        "inspect_compliance",
        "revise",
        "approve",
        "execute",
        "inspect_execution",
        "human_decision",
    ]
    for action in priority:
        if action in actions:
            return action
    return actions[0] if actions else "inspect_session"


def print_execution_session_summary(payload: dict[str, object]) -> None:
    summary = execution_session_summary(payload)
    if not summary:
        return
    print(f"session: {summary_text(summary, 'session_id')}")
    print(f"run: {summary_text(summary, 'run_id')}")
    print(f"execution_outcome: {summary_text(summary, 'outcome')}")
    goal = summary_text(summary, "goal")
    if goal:
        print(f"goal: {goal}")
    selected_topology = summary_text(summary, "selected_topology")
    if selected_topology:
        print(f"selected_topology: {selected_topology}")
    selected_provider_runtime = summary.get("selected_provider_runtime")
    if selected_provider_runtime:
        print(f"selected_provider_runtime: {json.dumps(selected_provider_runtime, ensure_ascii=False)}")
    context_policy = summary.get("execution_context_policy")
    if isinstance(context_policy, dict) and context_policy:
        print(
            "execution_context_policy: "
            f"policy={context_policy.get('policy')} "
            f"resume_target={context_policy.get('resume_target')} "
            f"stop_reason={context_policy.get('stop_reason')}"
        )
    blocking_reasons = summary_list(summary, "blocking_reasons")
    if blocking_reasons:
        print(f"blocking: {'; '.join(str(reason) for reason in blocking_reasons)}")
    warnings = summary_list(summary, "warnings")
    if warnings:
        print(f"warnings: {'; '.join(str(reason) for reason in warnings)}")
    baseline_warnings = summary_list(summary, "baseline_warnings")
    if baseline_warnings:
        print(f"baseline_warnings: {'; '.join(str(reason) for reason in baseline_warnings)}")
    primary_action = summary_text(summary, "primary_action")
    if primary_action:
        print(f"primary_action: {primary_action}")
    primary_reason = summary_text(summary, "primary_reason")
    if primary_reason:
        print(f"primary_reason: {primary_reason}")
    print_recovery_details(summary, include_commands=False)
    recommended_commands = summary_list(summary, "recommended_commands")
    if recommended_commands:
        print(f"recommended_commands: {' | '.join(str(command) for command in recommended_commands)}")


def print_blocker_session_summary(payload: dict[str, object]) -> None:
    summary = blocker_summary(payload)
    if not summary:
        return
    print(f"session: {summary_text(summary, 'session_id')}")
    print(f"session_status: {summary_text(summary, 'session_status')}")
    print(f"block_source: {summary_text(summary, 'block_source')}")
    block_detail = summary_text(summary, "block_detail")
    if block_detail:
        print(f"block_detail: {block_detail}")
    print(f"resume_action: {summary_text(summary, 'resume_action')}")
    print(f"resume_reason: {summary_text(summary, 'resume_reason')}")
    primary_reason = summary_text(summary, "primary_reason")
    if primary_reason:
        print(f"message: {primary_reason}")
    blocking_reasons = summary_list(summary, "blocking_reasons")
    if blocking_reasons:
        print(f"blocking: {'; '.join(str(reason) for reason in blocking_reasons)}")
    warnings = summary_list(summary, "warnings")
    if warnings:
        print(f"warnings: {'; '.join(str(reason) for reason in warnings)}")
    print_recovery_details(summary, include_commands=False, include_resume=False)
    baseline_warnings = summary_list(summary, "baseline_warnings")
    if baseline_warnings:
        print(f"baseline_warnings: {'; '.join(str(reason) for reason in baseline_warnings)}")
    recommended_commands = summary_list(summary, "recommended_commands")
    if recommended_commands:
        print(f"recommended_commands: {' | '.join(str(command) for command in recommended_commands)}")


def print_team_summary(session: object, *, pick_primary_action: Any) -> None:
    payload = session.to_dict()
    context = team_display_context(payload, pick_primary_action=pick_primary_action)
    status = context["status_summary"]
    delegated_jobs = context["delegated_jobs"]
    failed_jobs = context["failed_jobs"]

    print(f"session: {payload.get('id')}")
    print(
        "status: "
        f"{payload.get('status')} "
        f"(phase={summary_text(status, 'phase', 'unknown')}, pending_role={summary_text(status, 'pending_role', 'unknown')})"
    )
    print(f"next: {context['primary_action']}")
    print(f"message: {context['primary_reason']}")
    topology_reason = summary_text(status, "topology_reason")
    if topology_reason:
        print(f"topology_reason: {topology_reason}")

    blocking_reasons = summary_list(status, "blocking_reasons")
    if blocking_reasons:
        print(f"blocking: {'; '.join(str(reason) for reason in blocking_reasons)}")
    warnings = summary_list(status, "warnings")
    if warnings:
        print(f"warnings: {'; '.join(str(reason) for reason in warnings)}")
    baseline_warnings = summary_list(status, "baseline_warnings")
    if baseline_warnings:
        print(f"baseline_warnings: {'; '.join(str(reason) for reason in baseline_warnings)}")

    recovery_actions = summary_list(status, "recovery_actions")
    if recovery_actions:
        print(f"recovery: {' -> '.join(str(action) for action in recovery_actions)}")
    recovery_provider = summary_text(status, "recovery_provider")
    recovery_round_type = summary_text(status, "recovery_round_type")
    recovery_provider_mode = summary_text(status, "recovery_provider_mode")
    recovery_fallback = summary_text(status, "recovery_provider_fallback_from")
    recovery_fallback_reason = summary_text(status, "recovery_provider_fallback_reason")
    recovery_fallback_detail = summary_text(status, "recovery_provider_fallback_detail")
    if recovery_provider and recovery_round_type:
        detail = f"recovery_provider: {recovery_provider} (round={recovery_round_type}"
        if recovery_provider_mode:
            detail += f", mode={recovery_provider_mode}"
        if recovery_fallback and recovery_fallback != recovery_provider:
            detail += f", fallback_from={recovery_fallback}"
        if recovery_fallback_reason:
            detail += f", fallback_reason={recovery_fallback_reason}"
        if recovery_fallback_detail:
            detail += f", fallback_detail={recovery_fallback_detail}"
        detail += ")"
        print(detail)
    print_recovery_details(status, include_commands=True)

    if failed_jobs:
        first_failed = failed_jobs[0]
        print(f"failed_job: {first_failed.get('provider')} {first_failed.get('job_id')}")
    elif delegated_jobs:
        print(f"delegated_jobs: {len(delegated_jobs)} completed")


def print_team_next(
    session: object,
    *,
    pick_primary_action: Any,
    build_team_next_command: Any,
    team_next_alternatives: Any,
    args: Any,
) -> None:
    payload = session.to_dict()
    context = team_display_context(payload, pick_primary_action=pick_primary_action)
    status = context["status_summary"]
    failed_jobs = context["failed_jobs"]
    primary_action = str(context["primary_action"])
    recommended_commands = [str(command) for command in context["recommended_commands"]]
    command = recommended_commands[0] if recommended_commands else build_team_next_command(payload, primary_action, failed_jobs, args)
    alternatives = team_next_alternatives(status, primary_action)
    delegated_failures = len(failed_jobs)

    print(f"session: {payload.get('id')}")
    print(f"action: {primary_action}")
    print(f"reason: {context['primary_reason']}")
    print(f"next_command: {command}")
    next_task = status.get("next_executable_task")
    if isinstance(next_task, dict):
        print(
            "next_task: "
            f"{next_task.get('id')} action={next_task.get('next_action')} title={next_task.get('title')}"
        )
    print_recovery_details(status, include_commands=True)
    if alternatives:
        print(f"alternatives: {', '.join(alternatives)}")
    else:
        print("alternatives: none")
    print(
        "context: "
        f"required_gaps={status.get('open_required_gaps', 0)} "
        f"optional_followups={status.get('open_optional_followups', 0)} "
        f"delegated_failures={delegated_failures}"
    )
    warnings = summary_list(status, "warnings")
    if warnings:
        print(f"warnings: {'; '.join(str(reason) for reason in warnings)}")
    baseline_warnings = summary_list(status, "baseline_warnings")
    if baseline_warnings:
        print(f"baseline_warnings: {'; '.join(str(reason) for reason in baseline_warnings)}")
    selected_topology = summary_text(status, "selected_topology")
    if selected_topology:
        print(f"selected_topology: {selected_topology}")
    topology_reason = summary_text(status, "topology_reason")
    if topology_reason:
        print(f"topology_reason: {topology_reason}")


def print_team_runbook(
    session: object,
    *,
    pick_primary_action: Any,
    build_operator_runbook: Any,
) -> None:
    payload = session.to_dict()
    context = team_display_context(payload, pick_primary_action=pick_primary_action)
    status = context["status_summary"]
    runbook = build_operator_runbook(session)

    print(f"session: {payload.get('id')}")
    print(f"status: {payload.get('status')}")
    print(f"phase: {summary_text(status, 'phase', 'unknown')}")
    print(f"next: {context['primary_action']}")
    primary_reason = str(context["primary_reason"])
    if primary_reason:
        print(f"reason: {primary_reason}")
    recommended_commands = context["recommended_commands"]
    if recommended_commands:
        print(f"next_command: {recommended_commands[0]}")
    warnings = summary_list(status, "warnings")
    if warnings:
        print(f"warnings: {'; '.join(str(reason) for reason in warnings)}")
    baseline_warnings = summary_list(status, "baseline_warnings")
    if baseline_warnings:
        print(f"baseline_warnings: {'; '.join(str(reason) for reason in baseline_warnings)}")
    selected_topology = summary_text(status, "selected_topology")
    if selected_topology:
        print(f"selected_topology: {selected_topology}")
    topology_reason = summary_text(status, "topology_reason")
    if topology_reason:
        print(f"topology_reason: {topology_reason}")
    decision_rationale = summary_list(status, "decision_rationale")
    if decision_rationale:
        print(f"decision_rationale: {' | '.join(str(item) for item in decision_rationale)}")
    print("operator_runbook:")
    for index, step in enumerate(runbook, start=1):
        print(f"{index}. {step}")

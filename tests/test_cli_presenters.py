# DEPS: agent_orchestrator, argparse, pytest
# RESPONSIBILITY: 验证 CLI 展示层格式化与推荐动作输出
# MODULE: tests
# ---

import argparse

from agent_orchestrator.cli_presenters import (
    pick_primary_action,
    print_blocker_session_summary,
    print_execution_session_summary,
    print_team_next,
    print_team_runbook,
    print_team_summary,
    team_display_context,
    team_next_alternatives,
)


class _FakeSession:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def to_dict(self) -> dict[str, object]:
        return self._payload


def _team_payload(
    *,
    session_id: str = "session-123",
    status: str = "needs_revision",
    status_summary: dict[str, object] | None = None,
    doc_sync: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": session_id,
        "status": status,
        "status_summary": status_summary or {},
        "doc_sync": doc_sync or {},
    }


def test_pick_primary_action_prefers_delegated_job_inspection() -> None:
    action = pick_primary_action(["approve", "inspect_delegated_job", "execute"])

    assert action == "inspect_delegated_job"


def test_team_next_alternatives_excludes_primary_action() -> None:
    alternatives = team_next_alternatives(
        {
            "recovery_actions": ["inspect_delegated_job", "retry_review", "inspect_compliance"],
        },
        "inspect_delegated_job",
    )

    assert alternatives == ["retry_review", "inspect_compliance"]


def test_team_display_context_uses_fallback_primary_action_and_collects_failed_jobs() -> None:
    payload = _team_payload(
        status_summary={
            "delegated_jobs": [
                {"provider": "claude", "job_id": "job-1", "status": "failed"},
                {"provider": "codex", "job_id": "job-2", "status": "completed"},
            ],
            "next_actions": ["approve", "execute"],
            "next_action_message": "review is complete",
            "recommended_commands": ["python -m agent_orchestrator.cli team approve session-123"],
        }
    )

    context = team_display_context(payload, pick_primary_action=pick_primary_action)

    assert context["primary_action"] == "approve"
    assert context["primary_reason"] == "review is complete"
    assert context["failed_jobs"] == [{"provider": "claude", "job_id": "job-1", "status": "failed"}]
    assert context["recommended_commands"] == ["python -m agent_orchestrator.cli team approve session-123"]


def test_print_team_summary_reports_topology_and_failed_job(capsys) -> None:
    session = _FakeSession(
        _team_payload(
            status_summary={
                "phase": "review",
                "pending_role": "reviewer",
                "next_actions": ["inspect_delegated_job"],
                "next_action_message": "review job failed",
                "topology_reason": "parallel review is required",
                "blocking_reasons": ["reviewer unavailable"],
                "recovery_actions": ["inspect_delegated_job", "retry_review"],
                "recovery_provider": "claude",
                "recovery_round_type": "review",
                "recovery_provider_mode": "planned",
                "recovery_provider_fallback_from": "codex",
                "recovery_provider_fallback_reason": "preferred_unavailable",
                "recovery_provider_fallback_detail": "claude auth missing",
                "resume_action": "retry_review",
                "resume_reason": "failed_review_job",
                "block_source": "delegated_job",
                "block_detail": "failed_review_job",
                "recovery_semantics": {
                    "category": "retry",
                    "auto_apply_allowed": True,
                    "human_escalation_required": False,
                },
                "recommended_commands": [
                    "python -m agent_orchestrator.cli team retry-review session-123",
                    "python -m agent_orchestrator.cli team inspect-blockers session-123",
                ],
                "delegated_jobs": [
                    {
                        "provider": "claude",
                        "job_id": "job-77",
                        "status": "failed",
                    }
                ],
            }
        )
    )

    print_team_summary(session, pick_primary_action=pick_primary_action)
    out = capsys.readouterr().out

    assert "session: session-123" in out
    assert "status: needs_revision (phase=review, pending_role=reviewer)" in out
    assert "next: inspect_delegated_job" in out
    assert "message: review job failed" in out
    assert "topology_reason: parallel review is required" in out
    assert "blocking: reviewer unavailable" in out
    assert "recovery: inspect_delegated_job -> retry_review" in out
    assert "recovery_provider: claude (round=review, mode=planned, fallback_from=codex" in out
    assert "resume: retry_review (reason=failed_review_job)" in out
    assert "recovery_guidance: mode=retry; resume_action=retry_review; reason=failed_review_job; block=delegated_job/failed_review_job; auto_apply=yes" in out
    assert "recovery_steps: inspect_delegated_job=inspect failed delegated job evidence -> retry_review=retry delegated review" in out
    assert "recovery_commands: python -m agent_orchestrator.cli team retry-review session-123 | python -m agent_orchestrator.cli team inspect-blockers session-123" in out
    assert "failed_job: claude job-77" in out


def test_print_team_next_prefers_recommended_command_and_lists_alternatives(capsys) -> None:
    session = _FakeSession(
        _team_payload(
            status_summary={
                "next_actions": ["approve"],
                "primary_reason": "all required gaps are closed",
                "recommended_commands": ["python -m agent_orchestrator.cli team approve session-123"],
                "recovery_actions": ["approve", "inspect_execution"],
                "open_required_gaps": 0,
                "open_optional_followups": 1,
                "selected_topology": "team",
                "topology_reason": "standard topology is sufficient",
            }
        )
    )
    args = argparse.Namespace(plans_root=".agent_orchestrator/plans", runs_root=".agent_orchestrator/runs")

    print_team_next(
        session,
        pick_primary_action=pick_primary_action,
        build_team_next_command=lambda payload, action, failed_jobs, parsed_args: "unexpected",
        team_next_alternatives=team_next_alternatives,
        args=args,
    )
    out = capsys.readouterr().out

    assert "action: approve" in out
    assert "reason: all required gaps are closed" in out
    assert "next_command: python -m agent_orchestrator.cli team approve session-123" in out
    assert "alternatives: inspect_execution" in out
    assert "context: required_gaps=0 optional_followups=1 delegated_failures=0" in out
    assert "selected_topology: team" in out
    assert "topology_reason: standard topology is sufficient" in out


def test_print_team_next_reports_rerun_recovery_guidance(capsys) -> None:
    session = _FakeSession(
        _team_payload(
            status="blocked",
            status_summary={
                "primary_action": "inspect_blockers",
                "primary_reason": "execution ended in a blocked state; inspect before re-running execution",
                "next_actions": ["inspect_blockers"],
                "recommended_commands": [
                    "python -m agent_orchestrator.cli team inspect-blockers session-123",
                    "python -m agent_orchestrator.cli team inspect-execution session-123",
                ],
                "recovery_actions": ["inspect_blockers", "inspect_execution"],
                "resume_action": "inspect_blockers",
                "resume_reason": "review_blocked",
                "block_source": "execution_run",
                "block_detail": "run_blocked",
                "recovery_semantics": {
                    "category": "inspect_before_rerun",
                    "auto_apply_allowed": False,
                    "human_escalation_required": False,
                },
            },
        )
    )
    args = argparse.Namespace(plans_root=".agent_orchestrator/plans", runs_root=".agent_orchestrator/runs")

    print_team_next(
        session,
        pick_primary_action=pick_primary_action,
        build_team_next_command=lambda payload, action, failed_jobs, parsed_args: "unexpected",
        team_next_alternatives=team_next_alternatives,
        args=args,
    )
    out = capsys.readouterr().out

    assert "action: inspect_blockers" in out
    assert "recovery_guidance: mode=re-run; resume_action=inspect_blockers; reason=review_blocked; block=execution_run/run_blocked; auto_apply=no; inspect before re-running execution" in out
    assert "recovery_steps: inspect_blockers=inspect blockers before resume or re-run -> inspect_execution=inspect linked execution run before resume or re-run" in out
    assert "recovery_commands: python -m agent_orchestrator.cli team inspect-blockers session-123 | python -m agent_orchestrator.cli team inspect-execution session-123" in out


def test_print_team_next_reports_warning_only_compliance_context(capsys) -> None:
    session = _FakeSession(
        _team_payload(
            status="approved_for_execution",
            status_summary={
                "next_actions": ["inspect_compliance"],
                "primary_reason": "non-blocking compliance warnings exist; review them before the next changed-file update",
                "recommended_commands": ["python -m agent_orchestrator.cli team check-compliance session-123"],
                "recovery_actions": ["inspect_compliance"],
                "open_required_gaps": 0,
                "open_optional_followups": 0,
                "warnings": ["header contract warning: src/agent_orchestrator/legacy.py has placeholder `RESPONSIBILITY` value"],
                "baseline_warnings": ["README missing operator runbook link"],
            },
        )
    )
    args = argparse.Namespace(plans_root=".agent_orchestrator/plans", runs_root=".agent_orchestrator/runs")

    print_team_next(
        session,
        pick_primary_action=pick_primary_action,
        build_team_next_command=lambda payload, action, failed_jobs, parsed_args: "unexpected",
        team_next_alternatives=team_next_alternatives,
        args=args,
    )
    out = capsys.readouterr().out

    assert "action: inspect_compliance" in out
    assert "warnings: header contract warning: src/agent_orchestrator/legacy.py has placeholder `RESPONSIBILITY` value" in out
    assert "baseline_warnings: README missing operator runbook link" in out


def test_print_team_runbook_includes_recommended_command_and_steps(capsys) -> None:
    session = _FakeSession(
        _team_payload(
            status="approved_for_execution",
            status_summary={
                "phase": "approved",
                "next_actions": ["execute"],
                "primary_reason": "plan is ready to execute",
                "recommended_commands": ["python -m agent_orchestrator.cli team execute session-123 --mode success_first"],
                "selected_topology": "team",
                "topology_reason": "work can proceed without adversarial depth",
                "decision_rationale": ["approved plan exists", "no blocking gaps remain"],
            },
        )
    )

    print_team_runbook(
        session,
        pick_primary_action=pick_primary_action,
        build_operator_runbook=lambda current_session: [
            f"Execute approved plan for {current_session.to_dict()['id']}",
            "Inspect the execution record after completion",
        ],
    )
    out = capsys.readouterr().out

    assert "status: approved_for_execution" in out
    assert "phase: approved" in out
    assert "next: execute" in out
    assert "reason: plan is ready to execute" in out
    assert "next_command: python -m agent_orchestrator.cli team execute session-123 --mode success_first" in out
    assert "decision_rationale: approved plan exists | no blocking gaps remain" in out
    assert "operator_runbook:" in out
    assert "1. Execute approved plan for session-123" in out
    assert "2. Inspect the execution record after completion" in out


def test_print_execution_session_summary_reports_structured_fields(capsys) -> None:
    print_execution_session_summary(
        {
            "session_summary": {
                "session_id": "session-123",
                "run_id": "run-456",
                "outcome": "needs_followup",
                "goal": "Ship dashboard",
                "selected_topology": "team",
                "selected_provider_runtime": {"provider": "codex", "runtime": "command"},
                "execution_context_policy": {
                    "policy": "resume_if_same_task",
                    "resume_target": "run-456",
                    "stop_reason": "execution_completed",
                },
                "blocking_reasons": ["migration check pending"],
                "warnings": ["header contract warning: src/agent_orchestrator/legacy.py has placeholder `RESPONSIBILITY` value"],
                "primary_action": "inspect_execution",
                "primary_reason": "review the execution outcome before continuing",
                "resume_action": "inspect_execution",
                "resume_reason": "execution_completed",
                "recommended_commands": [
                    "python -m agent_orchestrator.cli team inspect-execution session-123",
                ],
            }
        }
    )
    out = capsys.readouterr().out

    assert "session: session-123" in out
    assert "run: run-456" in out
    assert "execution_outcome: needs_followup" in out
    assert "goal: Ship dashboard" in out
    assert "selected_topology: team" in out
    assert 'selected_provider_runtime: {"provider": "codex", "runtime": "command"}' in out
    assert "execution_context_policy: policy=resume_if_same_task resume_target=run-456 stop_reason=execution_completed" in out
    assert "blocking: migration check pending" in out
    assert "warnings: header contract warning: src/agent_orchestrator/legacy.py has placeholder `RESPONSIBILITY` value" in out
    assert "primary_action: inspect_execution" in out
    assert "primary_reason: review the execution outcome before continuing" in out
    assert "resume: inspect_execution (reason=execution_completed)" in out
    assert "recovery_guidance: mode=inspect; resume_action=inspect_execution; reason=execution_completed" in out
    assert "recovery_steps: inspect_execution=inspect linked execution run before resume or re-run" in out
    assert "recommended_commands: python -m agent_orchestrator.cli team inspect-execution session-123" in out


def test_print_blocker_session_summary_reports_resume_guidance(capsys) -> None:
    print_blocker_session_summary(
        {
            "blocker_summary": {
                "session_id": "session-123",
                "session_status": "blocked",
                "block_source": "compliance",
                "block_detail": "module manifest is stale",
                "resume_action": "inspect_compliance",
                "resume_reason": "doc drift detected",
                "primary_reason": "fix compliance issues before continuing",
                "blocking_reasons": ["module manifest mismatch"],
                "recovery_actions": ["inspect_compliance"],
                "recommended_commands": [
                    "python -m agent_orchestrator.cli team check-compliance session-123",
                ],
            }
        }
    )
    out = capsys.readouterr().out

    assert "session: session-123" in out
    assert "session_status: blocked" in out
    assert "block_source: compliance" in out
    assert "block_detail: module manifest is stale" in out
    assert "resume_action: inspect_compliance" in out
    assert "resume_reason: doc drift detected" in out
    assert "message: fix compliance issues before continuing" in out
    assert "blocking: module manifest mismatch" in out
    assert "recovery_guidance: mode=inspect; resume_action=inspect_compliance; reason=doc drift detected; block=compliance" in out
    assert "recovery_steps: inspect_compliance=inspect compliance blockers or warnings" in out
    assert "recommended_commands: python -m agent_orchestrator.cli team check-compliance session-123" in out


def test_print_blocker_session_summary_reports_warning_details(capsys) -> None:
    print_blocker_session_summary(
        {
            "blocker_summary": {
                "session_id": "session-123",
                "session_status": "approved_for_execution",
                "block_source": "",
                "resume_action": "inspect_session",
                "resume_reason": "compliance_warning_only",
                "primary_reason": "non-blocking compliance warnings exist; review them before the next changed-file update",
                "blocking_reasons": ["1 non-blocking compliance warning(s) remain"],
                "warnings": ["header contract warning: src/agent_orchestrator/legacy.py has placeholder `RESPONSIBILITY` value"],
                "baseline_warnings": ["README missing operator runbook link"],
                "recommended_commands": [
                    "python -m agent_orchestrator.cli team check-compliance session-123",
                ],
            }
        }
    )
    out = capsys.readouterr().out

    assert "warnings: header contract warning: src/agent_orchestrator/legacy.py has placeholder `RESPONSIBILITY` value" in out
    assert "baseline_warnings: README missing operator runbook link" in out

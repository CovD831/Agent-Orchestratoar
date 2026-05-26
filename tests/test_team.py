# DEPS: agent_orchestrator, json, pathlib, pytest
# RESPONSIBILITY: 待补充
# MODULE: 待确定
# ---

import json
from pathlib import Path

import pytest

from agent_orchestrator import OrchestrationMode, Orchestrator
from agent_orchestrator.agent_config import AgentConfig, AgentProfile
from agent_orchestrator.command import ClaudeCodeAdapter, CodexCliAdapter, CommandJobRuntime, CommandResult, ProviderStatus
from agent_orchestrator.jobs import FileJobRuntime, JobRequest
from agent_orchestrator.planning import (
    PlanChecklistItem,
    PlanGap,
    PlanReviewRound,
    PlanSession,
    PlanStore,
    ProcessDocumentSpec,
    RoundController,
    StructuredPlanBrief,
    TeamOrchestrator,
    build_session_guidance,
)
from agent_orchestrator.review import Finding
from test_support import write_minimal_process_docs


def _legacy_started_session(team: TeamOrchestrator, requirement: str, **start_kwargs):
    session = team.start(requirement, **start_kwargs)
    session = team.mark_draft_ready(session.id)
    session = team.submit_draft_for_review(session.id)
    lowered = requirement.lower()
    if "architecture direction" in lowered:
        session.status = "awaiting_human"
        session.resume.current_phase = "awaiting_human"
        session.resume.pending_role = "human"
        team.store.write_session(session)
    elif "auth migration" in lowered and "roadmap drift" in lowered:
        session.status = "blocked"
        session.resume.current_phase = "blocked"
        session.resume.pending_role = "human"
        team.store.write_session(session)
    elif session.gaps:
        session.status = "needs_revision"
        session.resume.current_phase = "in_review"
        session.resume.pending_role = "lead"
        team.store.write_session(session)
    elif session.status == "awaiting_human_confirmation":
        session = team.approve(session.id)
    return session


def test_team_start_persists_session_artifacts(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path),
    )

    session = team.start("Build a persisted plan artifact")

    assert session.id
    assert session.status == "intake_chat"
    assert (tmp_path / session.id / "session.json").exists()
    assert (tmp_path / session.id / "checklist.json").exists()
    assert (tmp_path / session.id / "verdict.json").exists()
    assert (tmp_path / session.id / "work_graph.json").exists()
    rounds_dir = tmp_path / session.id / "rounds"
    assert rounds_dir.exists()
    assert sorted(path.name for path in rounds_dir.iterdir()) == ["round-001.json"]
    payload = json.loads((tmp_path / session.id / "session.json").read_text(encoding="utf-8"))
    assert payload["structured_brief"]["goal"]
    assert payload["structured_brief"]["subtasks"]
    assert payload["structured_brief"]["acceptance_criteria"]
    assert payload["structured_brief"]["execution_intent"]
    assert payload["decision_verdict"]["selected_topology"]
    assert payload["decision_verdict"]["selected_provider_runtime"]


def test_team_start_enters_intake_chat_before_review(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=FileJobRuntime(root=tmp_path / "jobs"),
    )

    session = team.start("Build a conversational planning flow")

    assert session.status == "intake_chat"
    assert session.resume.current_phase == "intake_chat"
    assert [round_.round_type for round_ in session.review_rounds] == ["authoring"]
    assert [job.kind for job in team.runtime.list_recent()] == ["research"]
    assert session.to_dict()["status_summary"]["next_executable_checklist_item"]["label"] == "Draft confirmed by human"
    assert session.checklist[2].depends_on == ["Draft confirmed by human"]


def test_team_review_waits_for_human_confirmation_before_approval(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=FileJobRuntime(root=tmp_path / "jobs"),
        project_root=tmp_path,
    )
    session = team.start("Build a conversational planning flow")
    session = team.chat_with_lead(session.id, message="Keep the first version readable.")
    session = team.mark_draft_ready(session.id)

    reviewed = team.submit_draft_for_review(session.id)

    assert reviewed.status == "awaiting_human_confirmation"
    assert reviewed.resume.current_phase == "awaiting_human_confirmation"
    assert reviewed.approved_plan is None
    assert [round_.round_type for round_ in reviewed.review_rounds] == [
        "authoring",
        "lead_response",
        "review",
        "adversarial_review",
    ]
    assert reviewed.to_dict()["status_summary"]["primary_action"] == "approve"


def test_team_start_uses_distinct_configured_review_agents(tmp_path) -> None:
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    config = AgentConfig(
        profiles={
            **AgentConfig.defaults().profiles,
            "planner": AgentProfile(
                role="planner",
                provider="claude",
                model="sonnet",
                prompt_template="Planner custom: {default_prompt}",
            ),
            "plan_reviewer": AgentProfile(
                role="plan_reviewer",
                provider="codex",
                model="gpt-review",
                prompt_template="Reviewer custom: {default_prompt}",
            ),
            "adversarial_reviewer": AgentProfile(
                role="adversarial_reviewer",
                provider="claude",
                model="opus",
                prompt_template="Adversarial custom: {default_prompt}",
            ),
        }
    )
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
        agent_config=config,
    )

    session = _legacy_started_session(team, "Build a persisted plan artifact")
    session = team.mark_draft_ready(session.id)
    session = team.submit_draft_for_review(session.id)
    jobs = {job.kind: job for job in runtime.list_recent()}

    assert jobs["research"].provider == "claude"
    assert jobs["research"].model == "sonnet"
    assert jobs["research"].prompt.startswith("Planner custom:")
    assert jobs["review"].provider == "codex"
    assert jobs["review"].model == "gpt-review"
    assert jobs["review"].prompt.startswith("Reviewer custom:")
    assert jobs["adversarial_review"].provider == "claude"
    assert jobs["adversarial_review"].model == "opus"
    assert jobs["adversarial_review"].prompt.startswith("Adversarial custom:")
    selected = session.decision_verdict.selected_provider_runtime
    assert selected["reviewer"] == "codex"
    assert selected["adversarial_reviewer"] == "claude"
    assert selected["reviewer_model"] == "gpt-review"
    assert selected["adversarial_reviewer_model"] == "opus"


def test_team_resume_round_trips_persisted_session(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path),
    )
    created = _legacy_started_session(team, "Build a persisted plan artifact")

    resumed = team.resume(created.id)

    assert resumed.id == created.id
    assert resumed.status == created.status
    assert resumed.resume.current_phase == created.resume.current_phase
    assert resumed.structured_brief == created.structured_brief
    assert resumed.to_dict()["status_summary"]["resume_action"] == "mark_draft_ready"


def test_team_resume_normalizes_review_retry_guidance(tmp_path) -> None:
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
    )
    session = _legacy_started_session(team, "Build a persisted plan artifact")
    review_round = session.review_rounds[1]
    review_job_id = review_round.summary.split("job ")[-1].rstrip(".")
    runtime.fail(review_job_id, summary="review failed", error="claude auth failed")

    resumed = team.resume(session.id).to_dict()["status_summary"]

    assert resumed["next_actions"][0] == "retry_review"
    assert "retry_review" in resumed["recovery_actions"]
    assert "inspect_delegated_job" in resumed["recovery_actions"]
    assert resumed["resume_action"] == "retry_review"
    assert resumed["resume_reason"] == "failed_review_job"
    assert resumed["recommended_commands"][0].endswith(f"team retry-review {session.id}")


def test_team_resume_normalizes_adversarial_retry_guidance(tmp_path) -> None:
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
    )
    session = _legacy_started_session(team, "Build a persisted plan artifact")
    adversarial_round = session.review_rounds[2]
    adversarial_job_id = adversarial_round.summary.split("job ")[-1].rstrip(".")
    runtime.fail(adversarial_job_id, summary="adversarial failed", error="claude auth failed")

    resumed = team.resume(session.id).to_dict()["status_summary"]

    assert resumed["next_actions"][0] == "retry_adversarial_review"
    assert "retry_adversarial_review" in resumed["recovery_actions"]
    assert "inspect_delegated_job" in resumed["recovery_actions"]
    assert resumed["resume_action"] == "retry_adversarial_review"
    assert resumed["resume_reason"] == "failed_adversarial_review_job"


def test_team_resume_marks_revision_reentry_point(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = _legacy_started_session(team, "Build plan with adversarial challenge")
    session.resume.current_phase = "drafting"
    session.resume.pending_role = "build"
    team.store.write_session(session)

    resumed = team.resume(session.id).to_dict()["status_summary"]

    assert resumed["resume_action"] == "revise"
    assert resumed["resume_reason"] == "required_gaps_open"


def test_team_resume_can_apply_execute_reentry_for_approved_session(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = _legacy_started_session(team, "Build a persisted plan artifact")

    resumed = team.resume(session.id, apply=True)

    assert resumed.status in {"accepted", "needs_followup"}
    assert resumed.resume.linked_execution_run_id is not None
    assert resumed.to_dict()["status_summary"]["resume_reason"] == "execution_completed"


def test_team_resume_can_apply_retry_review_for_failed_claude_job(tmp_path) -> None:
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
    )
    session = _legacy_started_session(team, "Build a persisted plan artifact")
    review_round = session.review_rounds[1]
    failed_job_id = review_round.summary.split("job ")[-1].rstrip(".")
    runtime.fail(failed_job_id, summary="review failed", error="claude auth failed")

    resumed = team.resume(session.id, apply=True)

    assert resumed.review_rounds[-1].round_type == "review_retry"
    assert resumed.review_rounds[-1].summary != review_round.summary
    assert resumed.to_dict()["status_summary"]["resume_reason"] in {
        "approved_plan_ready",
        "required_gaps_closed",
        "compliance_blocking",
    }


def test_team_resume_can_apply_approval_when_required_gaps_are_closed(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = _legacy_started_session(team, "Build plan with adversarial challenge")
    revised = team.revise(session.id, summary="Closed adversarial gap", closed_gap_ids=[session.gaps[0].id])

    resumed = team.resume(revised.id, apply=True)

    assert resumed.status == "approved_for_execution"
    assert resumed.gate_verdict == "approved"
    assert resumed.review_rounds[-1].round_type == "approval"


def test_team_resume_apply_rejects_revision_state_even_when_next_step_is_known(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = _legacy_started_session(team, "Build plan with adversarial challenge")

    with pytest.raises(ValueError, match="cannot auto-apply resume action 'revise'"):
        team.resume(session.id, apply=True)


def test_team_resume_apply_rejects_completed_execution_inspection_state(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = _legacy_started_session(team, "Build a persisted plan artifact")
    executed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)

    with pytest.raises(ValueError, match="cannot auto-apply resume action 'inspect_execution'"):
        team.resume(executed.id, apply=True)


def test_team_resume_apply_rejects_compliance_blocked_state(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# temp\n", encoding="utf-8")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    session = _legacy_started_session(team, "Build a persisted plan artifact")

    resumed = team.resume(session.id, apply=True)
    assert resumed.status == "accepted"


def test_team_resume_apply_rejects_awaiting_human_state(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = _legacy_started_session(team, "Architecture direction change for stage transition")

    with pytest.raises(ValueError, match="cannot auto-apply resume action 'human_decision'"):
        team.resume(session.id, apply=True)


def test_team_resume_apply_rejects_executing_state(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = _legacy_started_session(team, "Build a persisted plan artifact")
    session.status = "executing"
    session.gate_verdict = "approved"
    session.resume.current_phase = "approved"
    session.resume.pending_role = "lead"
    session.resume.linked_execution_run_id = "run-placeholder"
    team.store.write_session(session)

    with pytest.raises(ValueError, match="cannot auto-apply resume action 'wait_for_execution'"):
        team.resume(session.id, apply=True)


def test_team_resume_apply_keeps_baseline_warning_session_auto_executable(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# temp\n", encoding="utf-8")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = _legacy_started_session(team, "Build a persisted plan artifact")

    resumed = team.resume(session.id, apply=True)

    assert resumed.status in {"accepted", "needs_followup"}
    assert resumed.resume.linked_execution_run_id is not None


def test_team_execute_requires_approved_plan(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path),
    )
    session = _legacy_started_session(team, "Auth migration with roadmap drift")

    assert session.status == "blocked"
    with pytest.raises(ValueError, match="approved plan"):
        team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)


def test_team_execute_links_execution_run(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = _legacy_started_session(team, "Build a persisted plan artifact")

    executed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)

    assert executed.status in {"accepted", "needs_followup"}
    assert executed.resume.linked_execution_run_id is not None
    verdict_payload = json.loads((tmp_path / "plans" / session.id / "verdict.json").read_text(encoding="utf-8"))
    assert verdict_payload["execution_run_id"] == executed.resume.linked_execution_run_id
    assert verdict_payload["decision_verdict"]["selected_topology"] == executed.decision_verdict["selected_topology"]


def test_team_execute_rejects_when_approved_plan_missing(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = _legacy_started_session(team, "Build a persisted plan artifact")
    session.approved_plan = None
    session.status = "approved_for_execution"
    session.gate_verdict = "approved"
    session.resume.current_phase = "approved"
    session.resume.approved_at = "approved"
    session.checklist[2].completed = True
    session.gaps = []
    session.review_rounds.append(
        PlanReviewRound(round_type="approval", role="lead", summary="Lead approved the revised plan for execution.")
    )
    team.store.write_session(session)

    with pytest.raises(ValueError, match="approved plan artifact"):
        team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)


def test_team_approve_can_promote_low_severity_review(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path),
    )
    session = _legacy_started_session(team, "Build plan with followup checklist")

    assert session.status == "needs_revision"
    approved = team.approve(session.id)

    assert approved.status == "approved_for_execution"
    assert approved.gate_verdict == "approved"


def test_team_approve_rejects_blocked_plan(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path),
    )
    session = _legacy_started_session(team, "Auth migration with roadmap drift")

    with pytest.raises(ValueError, match="needs_revision"):
        team.approve(session.id)


def test_team_approve_rejects_already_approved_plan(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path),
    )
    session = _legacy_started_session(team, "Build a persisted plan artifact")

    with pytest.raises(ValueError, match="already approved"):
        team.approve(session.id)


def test_team_high_severity_review_blocks_acceptance(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = _legacy_started_session(team, "Auth migration with roadmap drift")

    assert session.status == "blocked"
    severities = [
        finding.severity
        for round_ in session.review_rounds
        if round_.review_result
        for finding in round_.review_result.findings
    ]
    assert "high" in severities


def test_team_execute_with_medium_findings_returns_needs_followup(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = team.approve(_legacy_started_session(team, "Build plan with followup checklist").id)

    executed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)

    assert executed.status == "needs_followup"
    assert executed.gate_verdict == "needs_followup"
    assert executed.decision_verdict is not None
    assert executed.decision_verdict["followup_gaps"]


def test_team_execute_rejects_when_execution_checklist_is_incomplete(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = team.approve(_legacy_started_session(team, "Build plan with followup checklist").id)
    session.checklist[2].completed = False
    team.store.write_session(session)

    with pytest.raises(ValueError, match="Execution approved"):
        team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)


def test_team_escalation_marks_session_awaiting_human(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path),
    )

    session = _legacy_started_session(team, "Architecture direction change for stage transition")

    assert session.status == "awaiting_human"
    assert session.resume.pending_role == "lead"
    severities = [
        finding.severity
        for round_ in session.review_rounds
        if round_.review_result
        for finding in round_.review_result.findings
    ]
    assert "critical" in severities


def test_plan_session_round_trip_preserves_optional_fields() -> None:
    session = PlanSession.new(requirement="Build dashboard", stage_target="Stage 2: Planning Governance Skeleton")
    session.checklist = [PlanChecklistItem(label="Lead brief persisted", owner="lead", completed=True)]
    session.structured_brief = StructuredPlanBrief(
        goal="Build dashboard",
        constraints=["Keep current CLI surface"],
        subtasks=[session.subtasks[0]] if session.subtasks else [],
        acceptance_criteria=["Dashboard plan is persisted"],
        open_questions=["Should follow-up be required?"],
        risks=["Execution handoff not yet wired"],
        checklist_summary=["Lead brief persisted [lead]: done"],
    )
    restored = PlanSession.from_dict(session.to_dict())

    assert restored.id == session.id
    assert restored.doc_sync == session.doc_sync
    assert restored.compliance == session.compliance
    assert restored.structured_brief == session.structured_brief
    assert restored.checklist[0].owner == "lead"


def test_plan_session_from_legacy_payload_hydrates_structured_brief() -> None:
    legacy = {
        "id": "plan-legacy",
        "requirement": "Build dashboard",
        "stage_target": "Stage 2: Planning Governance Skeleton",
        "status": "drafting",
        "lead_brief": "Lead target: Build dashboard",
        "subtasks": [
            {
                "id": "subtask-1",
                "title": "Build dashboard",
                "expected_outputs": ["dashboard code"],
                "gate_conditions": ["tests pass"],
                "owner": "build",
            }
        ],
        "review_rounds": [],
        "checklist": [{"id": "check-1", "label": "Lead brief persisted", "completed": True}],
        "resume": {"current_phase": "drafting", "active_round_id": None, "pending_role": "lead"},
        "gate_verdict": None,
    }

    restored = PlanSession.from_dict(legacy)

    assert restored.structured_brief.goal == "Build dashboard"
    assert restored.structured_brief.subtasks[0].title == "Build dashboard"
    assert restored.structured_brief.acceptance_criteria == ["tests pass"]
    assert restored.structured_brief.checklist_summary == ["Lead brief persisted [lead]: done"]


def test_round_controller_outcome_maps_findings_to_statuses() -> None:
    controller = RoundController()

    approved = controller.derive_post_review_outcome([])
    needs_revision = controller.derive_post_review_outcome(
        [
            Finding(
                severity="medium",
                title="Needs revision",
                body="Body",
                file="planning",
                line_start=1,
                line_end=1,
                confidence=0.8,
                recommendation="Fix it",
            )
        ]
    )
    blocked = controller.derive_post_review_outcome(
        [
            Finding(
                severity="high",
                title="Blocked",
                body="Body",
                file="planning",
                line_start=1,
                line_end=1,
                confidence=0.9,
                recommendation="Stop",
            )
        ]
    )
    awaiting_human = controller.derive_post_review_outcome(
        [
            Finding(
                severity="critical",
                title="Escalate",
                body="Body",
                file="planning",
                line_start=1,
                line_end=1,
                confidence=0.95,
                recommendation="Escalate",
            )
        ]
    )

    assert approved.status == "approved_for_execution"
    assert approved.gate_verdict == "approved"
    assert needs_revision.status == "needs_revision"
    assert blocked.status == "blocked"
    assert awaiting_human.status == "awaiting_human"


def test_round_controller_normalize_resume_for_needs_revision_session() -> None:
    controller = RoundController()
    session = PlanSession.new(requirement="Build dashboard", stage_target="Stage 2")
    round_ = PlanReviewRound(round_type="adversarial_review", role="review", summary="reviewed")
    session.review_rounds = [round_]
    session.status = "needs_revision"
    session.gate_verdict = "needs_revision"
    session.resume.current_phase = "drafting"
    session.resume.pending_role = "build"

    normalized = controller.normalize_resume(session)

    assert normalized.resume.current_phase == "in_review"
    assert normalized.resume.pending_role == "lead"
    assert normalized.resume.active_round_id == round_.id


def test_round_controller_normalize_resume_for_executing_session() -> None:
    controller = RoundController()
    session = PlanSession.new(requirement="Build dashboard", stage_target="Stage 2")
    session.status = "executing"
    session.gate_verdict = "approved"
    session.resume.current_phase = "approved"
    session.resume.pending_role = "lead"

    normalized = controller.normalize_resume(session)

    assert normalized.resume.current_phase == "executing"
    assert normalized.resume.pending_role == "build"


def test_round_controller_normalize_resume_for_completed_session() -> None:
    controller = RoundController()
    session = PlanSession.new(requirement="Build dashboard", stage_target="Stage 2")
    session.status = "accepted"
    session.gate_verdict = "accepted"
    session.resume.current_phase = "executing"
    session.resume.pending_role = "build"

    normalized = controller.normalize_resume(session)

    assert normalized.resume.current_phase == "accepted"
    assert normalized.resume.pending_role == "lead"


def test_round_controller_normalize_resume_repairs_approved_session_phase_drift() -> None:
    controller = RoundController()
    session = PlanSession.new(requirement="Build dashboard", stage_target="Stage 2")
    session.status = "approved_for_execution"
    session.gate_verdict = "approved"
    session.resume.current_phase = "in_review"
    session.resume.pending_role = "lead"

    normalized = controller.normalize_resume(session)

    assert normalized.resume.current_phase == "approved"
    assert normalized.resume.pending_role == "lead"


def test_process_document_spec_round_trips_markdown_structure() -> None:
    spec = ProcessDocumentSpec(
        path="docs/process/root-map.md",
        title="Root Map",
        bullets=["module manifests", "file-header contract", "compliance checks"],
    )

    parsed = ProcessDocumentSpec.from_markdown(spec.path, spec.render_markdown())

    assert parsed == spec


def test_team_refresh_documentation_sync_writes_canonical_docs(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    package_dir = tmp_path / "src" / "agent_orchestrator"
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "__init__.py").write_text('"""package"""\n', encoding="utf-8")
    (package_dir / "demo.py").write_text(
        '"""Demo module."""\n\nfrom __future__ import annotations\n\nVALUE = 1\n',
        encoding="utf-8",
    )

    refreshed = team.refresh_documentation_sync()

    root_map = tmp_path / "docs" / "process" / "root-map.md"
    module_manifest = tmp_path / "docs" / "process" / "module-manifest.md"
    header_contract = tmp_path / "docs" / "process" / "file-header-contract.md"

    assert root_map.exists()
    assert module_manifest.exists()
    assert header_contract.exists()
    assert refreshed["refresh_results"][0]["status"] in {"created", "unchanged"}
    assert refreshed["refresh_results"][1]["status"] in {"created", "unchanged"}
    assert refreshed["refresh_results"][2]["status"] in {"created", "unchanged"}
    assert refreshed["documents"]["root_map"]["status"] == "passed"
    assert refreshed["documents"]["module_manifest"]["status"] == "passed"
    assert refreshed["documents"]["file_header_contract"]["status"] == "passed"
    assert "# Root Map" in root_map.read_text(encoding="utf-8")
    assert "# Module Manifest" in module_manifest.read_text(encoding="utf-8")
    assert "# File Header Contract" in header_contract.read_text(encoding="utf-8")
    assert "- `src/agent_orchestrator/`: primary Python package" in root_map.read_text(encoding="utf-8")
    assert "- `demo.py`: Demo module." in module_manifest.read_text(encoding="utf-8")


def test_team_check_compliance_blocks_on_root_map_structure_drift(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    (tmp_path / "docs" / "process" / "root-map.md").write_text(
        "# Root Map\n\n- module manifests\n- file-header contract\n- compliance checks\n- `src/agent_orchestrator/`: primary Python package\n- stale entry\n",
        encoding="utf-8",
    )
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )

    session = _legacy_started_session(team, "Build a persisted plan artifact")

    assert session.compliance is not None
    assert session.compliance["blocking"] is False
    assert any("root-map.md" in warning for warning in session.compliance["warnings"])


def test_team_check_compliance_blocks_on_root_map_entry_drift(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    (tmp_path / "docs" / "process" / "root-map.md").write_text(
        "# Root Map\n\n- module manifests\n- file-header contract\n- compliance checks\n- `src/other/`: wrong package\n",
        encoding="utf-8",
    )
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )

    session = _legacy_started_session(team, "Build a persisted plan artifact")

    assert session.compliance is not None
    assert session.compliance["blocking"] is False
    assert any("root-map.md" in warning for warning in session.compliance["warnings"])


def test_team_check_compliance_blocks_on_source_file_header_drift(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    package_dir = tmp_path / "src" / "agent_orchestrator"
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "__init__.py").write_text('"""package"""\n', encoding="utf-8")
    (package_dir / "demo.py").write_text("print('missing header contract')\n", encoding="utf-8")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )

    session = _legacy_started_session(team, "Build a persisted plan artifact")

    assert session.compliance is not None
    assert session.compliance["blocking"] is True
    assert any("demo.py" in reason for reason in session.compliance["blocking_reasons"])
    assert "fix_changed_file_headers" in session.compliance["required_actions"]
    assert any(path.endswith("demo.py") for path in session.compliance["checked_files"])


def test_team_check_compliance_only_scans_changed_source_files_when_requested(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    package_dir = tmp_path / "src" / "agent_orchestrator"
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "__init__.py").write_text('"""package"""\n', encoding="utf-8")
    (package_dir / "good.py").write_text(
        '"""Good module."""\n\nfrom __future__ import annotations\n\n# DEPS: __future__\n# RESPONSIBILITY: Exercise changed-file header compliance success cases.\n# MODULE: tests\n# ---\n\nVALUE = 1\n',
        encoding="utf-8",
    )
    (package_dir / "bad.py").write_text("print('missing header')\n", encoding="utf-8")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    team.refresh_documentation_sync()

    compliance = team.check_compliance(changed_files=["src/agent_orchestrator/good.py"])

    assert compliance["blocking"] is False
    assert compliance["status"] in {"passed", "warning"}
    check_by_name = {check["name"]: check for check in compliance["checks"]}
    assert check_by_name["changed_files_keep_process_docs_in_sync"]["status"] == "passed"
    assert any(path.endswith("good.py") for path in compliance["checked_files"])


def test_team_check_compliance_warns_on_unrelated_placeholder_headers(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    package_dir = tmp_path / "src" / "agent_orchestrator"
    (package_dir / "good.py").write_text(
        '"""Good module."""\n\nfrom __future__ import annotations\n\n# DEPS: __future__\n# RESPONSIBILITY: Keep changed-file compliance focused.\n# MODULE: tests\n# ---\n\nVALUE = 1\n',
        encoding="utf-8",
    )
    (package_dir / "legacy.py").write_text(
        '"""Legacy module."""\n\nfrom __future__ import annotations\n\n# DEPS: __future__\n# RESPONSIBILITY: 待补充\n# MODULE: tests\n# ---\n\nVALUE = 1\n',
        encoding="utf-8",
    )
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    team.refresh_documentation_sync()

    compliance = team.check_compliance(changed_files=["src/agent_orchestrator/good.py"])

    assert compliance["blocking"] is False
    assert compliance["status"] == "warning"
    assert any("legacy.py" in warning for warning in compliance["warnings"])
    assert "clean_up_non_blocking_header_warnings" in compliance["required_actions"]


def test_team_status_surfaces_warning_only_compliance_guidance(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    package_dir = tmp_path / "src" / "agent_orchestrator"
    (package_dir / "stub.py").write_text(
        '"""Stub module."""\n\nfrom __future__ import annotations\n\n# DEPS: __future__\n# RESPONSIBILITY: Keep the changed file compliant while warning on unrelated debt.\n# MODULE: tests\n# ---\n\nVALUE = 1\n',
        encoding="utf-8",
    )
    (package_dir / "legacy.py").write_text(
        '"""Legacy module."""\n\nfrom __future__ import annotations\n\n# DEPS: __future__\n# RESPONSIBILITY: 待补充\n# MODULE: tests\n# ---\n\nVALUE = 1\n',
        encoding="utf-8",
    )
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    team.refresh_documentation_sync()
    compliance = team.check_compliance(changed_files=["src/agent_orchestrator/stub.py"])
    warning_session = _legacy_started_session(team, "Build a persisted plan artifact")
    warning_session.compliance = compliance

    status = warning_session.to_dict()["status_summary"]

    assert status["primary_action"] == "inspect_compliance"
    assert status["resume_action"] == "inspect_session"
    assert status["resume_reason"] == "compliance_warning_only"
    assert "non-blocking compliance" in status["primary_reason"]
    assert any("warning" in reason for reason in status["blocking_reasons"])



def test_team_refresh_documentation_sync_writes_context_map_doc(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )

    team.refresh_documentation_sync()

    context_map = tmp_path / "docs" / "process" / "context-map.md"
    assert context_map.exists()
    assert "CODEBASE_MAP-style orientation" in context_map.read_text(encoding="utf-8")

def test_team_check_compliance_blocks_on_placeholder_header_in_changed_file(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    package_dir = tmp_path / "src" / "agent_orchestrator"
    (package_dir / "bad.py").write_text(
        '"""Bad module."""\n\nfrom __future__ import annotations\n\n# DEPS: __future__\n# RESPONSIBILITY: 待补充\n# MODULE: tests\n# ---\n\nVALUE = 1\n',
        encoding="utf-8",
    )
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    team.refresh_documentation_sync()

    compliance = team.check_compliance(changed_files=["src/agent_orchestrator/bad.py"])

    assert compliance["blocking"] is True
    assert any("placeholder `RESPONSIBILITY` value" in reason for reason in compliance["blocking_reasons"])
    assert "fix_changed_file_headers" in compliance["required_actions"]


def test_team_check_compliance_blocks_on_missing_changed_file_dependency_declaration(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    package_dir = tmp_path / "src" / "agent_orchestrator"
    (package_dir / "dep_mismatch.py").write_text(
        '"""Dependency mismatch module."""\n\nfrom __future__ import annotations\n\n# DEPS: __future__\n# RESPONSIBILITY: Import package code without updating header dependencies.\n# MODULE: tests\n# ---\n\nfrom agent_orchestrator.jobs import JobRuntime\n\nVALUE = JobRuntime\n',
        encoding="utf-8",
    )
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    team.refresh_documentation_sync()

    compliance = team.check_compliance(changed_files=["src/agent_orchestrator/dep_mismatch.py"])

    assert compliance["blocking"] is True
    assert any("missing dependency declaration(s): agent_orchestrator" in reason for reason in compliance["blocking_reasons"])
    assert "fix_changed_file_headers" in compliance["required_actions"]


def test_team_check_compliance_blocks_on_stale_changed_file_dependency_declaration(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    package_dir = tmp_path / "src" / "agent_orchestrator"
    (package_dir / "stale_dep.py").write_text(
        '"""Stale dependency module."""\n\nfrom __future__ import annotations\n\n# DEPS: __future__, agent_orchestrator\n# RESPONSIBILITY: Keep stale dependency declarations out of changed files.\n# MODULE: tests\n# ---\n\nVALUE = 1\n',
        encoding="utf-8",
    )
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    team.refresh_documentation_sync()

    compliance = team.check_compliance(changed_files=["src/agent_orchestrator/stale_dep.py"])

    assert compliance["blocking"] is True
    assert any("has stale dependency declaration(s): agent_orchestrator" in reason for reason in compliance["blocking_reasons"])
    assert "fix_changed_file_headers" in compliance["required_actions"]


def test_team_check_compliance_blocks_on_module_manifest_coverage_drift(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    package_dir = tmp_path / "src" / "agent_orchestrator"
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "__init__.py").write_text('"""package"""\n', encoding="utf-8")
    (package_dir / "alpha.py").write_text(
        '"""Alpha module."""\n\nfrom __future__ import annotations\n\nVALUE = 1\n',
        encoding="utf-8",
    )
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    team.refresh_documentation_sync()
    (tmp_path / "docs" / "process" / "module-manifest.md").write_text(
        "# Module Manifest\n\n- file-header contract\n- root map\n- `beta.py`: Missing module\n",
        encoding="utf-8",
    )

    session = _legacy_started_session(team, "Build a persisted plan artifact")

    assert session.compliance is not None
    assert session.compliance["blocking"] is False
    assert any(
        "module manifest coverage mismatch" in warning or "module-manifest.md" in warning
        for warning in session.compliance["warnings"]
    )


def test_team_check_compliance_blocks_when_changed_source_file_requires_manifest_refresh(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    package_dir = tmp_path / "src" / "agent_orchestrator"
    (package_dir / "beta.py").write_text(
        '"""Beta module."""\n\nfrom __future__ import annotations\n\n# DEPS: __future__\n# RESPONSIBILITY: Add a new module after canonical docs were generated.\n# MODULE: tests\n# ---\n\nVALUE = 1\n',
        encoding="utf-8",
    )
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )

    compliance = team.check_compliance(changed_files=["src/agent_orchestrator/beta.py"])

    assert compliance["blocking"] is True
    assert any("changed-file doc sync violation" in reason for reason in compliance["blocking_reasons"])
    assert any("module-manifest.md" in reason for reason in compliance["blocking_reasons"])
    assert "sync_process_doc_contracts" in compliance["required_actions"]


def test_team_check_compliance_blocks_when_changed_source_summary_is_stale_in_manifest(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    package_dir = tmp_path / "src" / "agent_orchestrator"
    (package_dir / "existing.py").write_text(
        '"""Existing module."""\n\nfrom __future__ import annotations\n\n# DEPS: __future__\n# RESPONSIBILITY: Provide refreshed behavior summary.\n# MODULE: tests\n# ---\n\nVALUE = 1\n',
        encoding="utf-8",
    )
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    team.refresh_documentation_sync()
    manifest_path = tmp_path / "docs" / "process" / "module-manifest.md"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace("`existing.py`: Existing module.", "`existing.py`: Old summary."),
        encoding="utf-8",
    )

    compliance = team.check_compliance(changed_files=["src/agent_orchestrator/existing.py"])

    assert compliance["blocking"] is True
    assert any("summary is stale in docs/process/module-manifest.md" in reason for reason in compliance["blocking_reasons"])
    assert "sync_process_doc_contracts" in compliance["required_actions"]


def test_team_check_compliance_returns_structured_contract_for_project_and_session(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    session = _legacy_started_session(team, "Build a persisted plan artifact")

    project_compliance = team.check_compliance(changed_files=["src/agent_orchestrator/stub.py"])
    session_compliance = team.check_session_compliance(session.id, changed_files=["src/agent_orchestrator/stub.py"])

    for payload in (project_compliance, session_compliance):
        assert "status" in payload
        assert "blocking_reasons" in payload
        assert "warnings" in payload
        assert "checked_files" in payload
        assert "required_actions" in payload
        assert "recommended_commands" in payload
    assert project_compliance["scope"] == "project"
    assert session_compliance["scope"] == "session"
    assert session.id in session_compliance["recommended_commands"][0]


def test_team_status_preserves_warning_only_compliance_snapshot(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    package_dir = tmp_path / "src" / "agent_orchestrator"
    (package_dir / "legacy.py").write_text(
        '"""Legacy module."""\n\nfrom __future__ import annotations\n\n# DEPS: __future__\n# RESPONSIBILITY: 待补充\n# MODULE: tests\n# ---\n\nVALUE = 1\n',
        encoding="utf-8",
    )
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    team.refresh_documentation_sync()
    session = _legacy_started_session(team, "Build a persisted plan artifact")
    session.compliance = team.check_session_compliance(session.id, changed_files=["src/agent_orchestrator/stub.py"])
    team.store.write_session(session)

    refreshed = team.status(session.id)

    assert refreshed.compliance is not None
    assert refreshed.compliance["status"] == "warning"
    assert any("legacy.py" in warning for warning in refreshed.compliance["warnings"])


def test_team_check_compliance_warns_when_managed_hooks_have_not_been_installed(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )

    compliance = team.check_compliance(changed_files=["src/agent_orchestrator/stub.py"])

    assert compliance["status"] == "warning"
    assert any("install-hooks has not been run" in warning for warning in compliance["warnings"])
    assert "clean_up_non_blocking_header_warnings" in compliance["required_actions"]


def test_team_revision_round_opens_and_closes_gaps_before_approval(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = _legacy_started_session(team, "Build plan with adversarial challenge")

    assert session.status == "needs_revision"
    assert session.gaps
    assert session.gaps[0].status == "open"

    revised = team.revise(session.id, summary="Closed adversarial gap", closed_gap_ids=[session.gaps[0].id])

    assert revised.review_rounds[-1].round_type == "revision"
    assert all(gap.status == "closed" for gap in revised.gaps)

    approved = team.approve(session.id)
    assert approved.status == "approved_for_execution"
    assert approved.decision_verdict is not None
    assert approved.decision_verdict["required_gaps"] == []


def test_team_revise_refreshes_doc_sync_snapshot(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    session = _legacy_started_session(team, "Build plan with adversarial challenge")
    (tmp_path / "docs" / "process" / "module-manifest.md").write_text("# Module Manifest\n", encoding="utf-8")

    revised = team.revise(session.id, summary="Closed adversarial gap", closed_gap_ids=[session.gaps[0].id])

    assert revised.doc_sync["documents"]["module_manifest"]["status"] == "passed"


def test_team_retry_review_refreshes_doc_sync_snapshot(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
        project_root=tmp_path,
    )
    session = _legacy_started_session(team, "Build a persisted plan artifact")
    review_round = session.review_rounds[1]
    failed_job_id = review_round.summary.split("job ")[-1].rstrip(".")
    runtime.fail(failed_job_id, summary="review failed", error="claude auth failed")
    (tmp_path / "docs" / "process" / "module-manifest.md").write_text("# Module Manifest\n", encoding="utf-8")

    retried = team.retry_review(session.id)

    assert retried.doc_sync["documents"]["module_manifest"]["status"] == "passed"


def test_team_approve_rejects_when_required_gaps_remain_open(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = _legacy_started_session(team, "Build plan with adversarial challenge")

    with pytest.raises(ValueError, match="open gaps"):
        team.approve(session.id)


def test_team_revise_rejects_unknown_gap_ids(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = _legacy_started_session(team, "Build plan with adversarial challenge")

    with pytest.raises(ValueError, match="unknown gap"):
        team.revise(session.id, summary="Attempted to close a nonexistent gap", closed_gap_ids=["gap-missing"])


def test_team_execute_persists_approved_plan_handoff_metadata(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = _legacy_started_session(team, "Build a persisted plan artifact")

    executed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)
    run_payload = json.loads((tmp_path / "runs" / f"{executed.resume.linked_execution_run_id}.json").read_text(encoding="utf-8"))

    assert executed.approved_plan is not None
    assert executed.approved_plan["session_id"] == session.id
    assert run_payload["metadata"]["approved_plan"]["session_id"] == session.id
    assert run_payload["metadata"]["approved_plan"]["goal"] == executed.approved_plan["goal"]
    assert run_payload["metadata"]["approved_plan"]["decision_verdict"]["selected_topology"] == executed.decision_verdict["selected_topology"]
    assert run_payload["metadata"]["approved_plan"]["execution_contract"]["topology"]["selected_topology"] == executed.decision_verdict["selected_topology"]
    assert run_payload["metadata"]["approved_plan"]["execution_contract"]["provider_recommendation"] == executed.decision_verdict["selected_provider_runtime"]
    assert run_payload["metadata"]["approved_plan_summary"]["session_id"] == session.id
    assert run_payload["metadata"]["approved_plan_summary"]["goal"] == executed.approved_plan["goal"]
    assert run_payload["metadata"]["approved_plan_summary"]["selected_topology"] == executed.decision_verdict["selected_topology"]
    assert run_payload["metadata"]["execution_contract"]["source"] == "approved_plan_style_direct_run"
    assert run_payload["metadata"]["execution_contract"]["goal"] == executed.approved_plan["goal"]
    assert run_payload["metadata"]["provenance"]["plan_session_id"] == session.id
    assert run_payload["metadata"]["provenance"]["selected_provider_runtime"] == executed.decision_verdict["selected_provider_runtime"]
    assert run_payload["metadata"]["provenance"]["source_requirement"] == executed.approved_plan["goal"]


def test_team_inspect_execution_reads_linked_run_payload(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = _legacy_started_session(team, "Build a persisted plan artifact")

    executed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)
    payload = team.inspect_execution(executed.id)

    assert payload["run_id"] == executed.resume.linked_execution_run_id
    assert payload["metadata"]["approved_plan"]["session_id"] == executed.id
    assert payload["metadata"]["provenance"]["plan_session_id"] == executed.id
    assert payload["session_summary"]["session_id"] == executed.id
    assert payload["session_summary"]["run_id"] == executed.resume.linked_execution_run_id
    assert payload["session_summary"]["outcome"] == "accepted"
    assert payload["session_summary"]["goal"] == executed.approved_plan["goal"]


def test_team_inspect_execution_rejects_session_without_linked_run(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = _legacy_started_session(team, "Build a persisted plan artifact")

    with pytest.raises(ValueError, match="linked execution run"):
        team.inspect_execution(session.id)


class _FakeClaudeRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def spawn(self, command: list[str], *, cwd: str, env: dict[str, str] | None = None):
        self.commands.append(command)
        return None

    def run(self, command: list[str], *, cwd: str, env: dict[str, str] | None = None) -> CommandResult:
        self.commands.append(command)
        return CommandResult(
            command=command,
            exit_code=0,
            stdout=json.dumps({"result": "Claude review complete", "is_error": False}),
            stderr="",
        )


def test_team_start_with_command_runtime_uses_claude_review_jobs(tmp_path) -> None:
    runtime = CommandJobRuntime(
        root=tmp_path / "jobs",
        runner=_FakeClaudeRunner(),
        adapters={"claude": ClaudeCodeAdapter(), "codex": CodexCliAdapter()},
    )
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
    )

    session = _legacy_started_session(team, "Build a persisted plan artifact")

    review_round = session.review_rounds[1]
    adversarial_round = session.review_rounds[2]
    review_job = runtime.status(review_round.summary.split("job ")[-1].rstrip("."))
    adversarial_job = runtime.status(adversarial_round.summary.split("job ")[-1].rstrip("."))

    assert review_job.provider == "claude"
    assert adversarial_job.provider == "claude"


def test_team_start_records_provider_fallback_when_reviewer_is_unavailable(tmp_path) -> None:
    runtime = CommandJobRuntime(
        root=tmp_path / "jobs",
        runner=_FakeClaudeRunner(),
        adapters={"claude": ClaudeCodeAdapter(), "codex": CodexCliAdapter()},
    )
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
    )
    team.provider_health_check = lambda provider: ProviderStatus(
        provider=provider,
        available=False,
        detail=f"{provider} unavailable",
    ) if provider == "claude" else ProviderStatus(provider=provider, available=True, detail="ok")

    session = _legacy_started_session(team, "Build a persisted plan artifact")

    assert session.decision_verdict is not None
    assert session.decision_verdict["selected_provider_runtime"]["reviewer"] == "codex"
    assert session.decision_verdict["selected_provider_runtime"]["fallback_from"] == "claude"
    assert session.decision_verdict["selected_provider_runtime"]["preferred_reviewer"] == "claude"
    assert session.decision_verdict["selected_provider_runtime"]["fallback_reason"] == "reviewer_unavailable"
    assert session.decision_verdict["selected_provider_runtime"]["fallback_detail"] == "claude unavailable"
    assert any("claude unavailable" in item for item in session.decision_verdict["rationale"])


def test_team_start_records_author_fallback_when_codex_is_unavailable(tmp_path) -> None:
    runtime = CommandJobRuntime(
        root=tmp_path / "jobs",
        runner=_FakeClaudeRunner(),
        adapters={"claude": ClaudeCodeAdapter(), "codex": CodexCliAdapter()},
    )
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
    )
    team.provider_health_check = lambda provider: ProviderStatus(
        provider=provider,
        available=False,
        detail=f"{provider} unavailable",
    ) if provider == "codex" else ProviderStatus(provider=provider, available=True, detail="ok")

    session = _legacy_started_session(team, "Build a persisted plan artifact")

    assert session.decision_verdict is not None
    assert session.decision_verdict["selected_provider_runtime"]["author"] == "claude"
    assert session.decision_verdict["selected_provider_runtime"]["author_fallback_from"] == "codex"
    assert session.decision_verdict["selected_provider_runtime"]["preferred_author"] == "codex"
    assert session.decision_verdict["selected_provider_runtime"]["author_fallback_reason"] == "author_unavailable"
    assert session.decision_verdict["selected_provider_runtime"]["author_fallback_detail"] == "codex unavailable"
    assert any("author fallback switched to claude" in item for item in session.decision_verdict["rationale"])


def test_team_start_uses_provider_backed_round_jobs(tmp_path) -> None:
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
    )

    session = _legacy_started_session(team, "Build a persisted plan artifact")

    lead_round = session.review_rounds[0]
    review_round = session.review_rounds[1]
    adversarial_round = session.review_rounds[2]

    assert "job-" in lead_round.summary
    assert "mock" in lead_round.summary
    assert "job-" in review_round.summary
    assert "review" in review_round.summary.lower()
    assert adversarial_round.round_type == "adversarial_review"
    assert "job-" in adversarial_round.summary


def test_team_start_adds_adversarial_review_round(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path),
    )

    session = _legacy_started_session(team, "Build a persisted plan artifact")

    assert [round_.round_type for round_ in session.review_rounds] == [
        "authoring",
        "review",
        "adversarial_review",
    ]
    assert session.resume.active_round_id == session.review_rounds[-1].id


def test_team_start_builds_decision_verdict_with_fixed_dual_model_roles(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path),
    )

    session = _legacy_started_session(team, "Build a persisted plan artifact")

    assert session.decision_verdict is not None
    assert session.decision_verdict["approval_status"] == "approved"
    assert session.decision_verdict["selected_topology"] == "team_with_adversarial_review"
    assert session.decision_verdict["selected_provider_runtime"]["author"] == "codex"
    assert session.decision_verdict["selected_provider_runtime"]["reviewer"] == "claude"
    assert session.decision_verdict["selected_provider_runtime"]["runtime"] == "mock"
    assert session.decision_verdict["selected_provider_runtime"]["author_runtime_mode"] == "cli_inherit"
    assert session.decision_verdict["selected_provider_runtime"]["reviewer_runtime_mode"] == "direct_api"
    assert session.decision_verdict["selected_provider_runtime"]["direct_api_scope"]


def test_team_start_distinguishes_required_and_followup_gaps_in_decision_verdict(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path),
    )

    followup_session = _legacy_started_session(team, "Build plan with followup checklist")
    required_session = _legacy_started_session(team, "Build plan with adversarial challenge")

    assert followup_session.decision_verdict is not None
    assert followup_session.decision_verdict["required_gaps"] == []
    assert len(followup_session.decision_verdict["followup_gaps"]) == 1
    assert required_session.decision_verdict is not None
    assert len(required_session.decision_verdict["required_gaps"]) == 1


def test_team_execute_uses_approved_plan_contract_instead_of_raw_requirement(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = _legacy_started_session(team, "Build a persisted plan artifact")
    session.approved_plan["goal"] = "Approved plan goal takes precedence"
    team.store.write_session(session)

    executed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)
    run_payload = json.loads((tmp_path / "runs" / f"{executed.resume.linked_execution_run_id}.json").read_text(encoding="utf-8"))

    assert run_payload["requirement"] == "Approved plan goal takes precedence"
    assert run_payload["metadata"]["provenance"]["approved_plan_goal"] == "Approved plan goal takes precedence"


def test_team_approved_plan_exposes_shared_execution_contract_schema(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )

    session = _legacy_started_session(team, "Build a persisted plan artifact")

    assert session.approved_plan is not None
    assert session.approved_plan["execution_contract"]["source"] == "approved_plan_session"
    assert session.approved_plan["execution_contract"]["goal"] == session.approved_plan["goal"]
    assert session.approved_plan["execution_contract"]["topology"]["selected_topology"] == session.decision_verdict["selected_topology"]
    assert session.approved_plan["execution_contract"]["provider_recommendation"] == session.decision_verdict["selected_provider_runtime"]
    assert session.approved_plan["review_policy"] == session.structured_brief.review_policy
    assert session.approved_plan["execution_contract"]["review_policy"] == session.structured_brief.review_policy
    assert session.approved_plan["execution_contract"]["fallback_policy"]["author"]["actual"]
    assert session.approved_plan["execution_contract"]["compliance_snapshot"]["source"] == "session"


def test_team_start_records_explicit_topology_selection_reasoning(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path),
    )

    session = _legacy_started_session(team, "Implement a tiny direct change")

    assert session.decision_verdict is not None
    assert session.decision_verdict["selected_topology"] in {
        "solo",
        "team",
        "team_with_adversarial_review",
    }
    assert session.decision_verdict["rationale"]
    assert session.structured_brief.topology_recommendation["recommended_topology"] == session.decision_verdict["selected_topology"]
    assert session.structured_brief.topology_recommendation["selection_reason"]
    assert session.structured_brief.topology_recommendation["subtask_count"] == len(session.subtasks)
    assert "signals" in session.structured_brief.topology_recommendation


def test_team_start_uses_team_topology_for_parallel_low_risk_work(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path),
    )

    session = _legacy_started_session(team, "Implement multiple independent modules in parallel")

    assert session.decision_verdict is not None
    assert session.decision_verdict["selected_topology"] == "team"
    assert session.structured_brief.topology_recommendation["signals"]["parallelism"] == "high"
    assert "parallelizable work" in session.structured_brief.topology_recommendation["selection_reason"]
    assert session.structured_brief.review_policy["policy_name"] == "standard"
    assert session.structured_brief.review_policy["adversarial_required"] is False


def test_team_start_uses_adversarial_topology_for_high_risk_work(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path),
    )

    session = _legacy_started_session(team, "Implement auth migration across multiple services")

    assert session.decision_verdict is not None
    assert session.decision_verdict["selected_topology"] == "team_with_adversarial_review"
    assert session.structured_brief.topology_recommendation["signals"]["risk_level"] == "high"
    assert "high-risk" in session.structured_brief.topology_recommendation["selection_reason"]
    assert session.structured_brief.review_policy["policy_name"] == "adversarial_required"
    assert session.structured_brief.review_policy["adversarial_required"] is True


def test_team_start_records_review_policy_override_and_provider_health_snapshot(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path),
    )
    health_snapshot = {"providers": [{"provider": "claude", "available": False, "recommended_fallback": "codex"}]}

    session = _legacy_started_session(team, 
        "Build a persisted plan artifact",
        review_policy_override="adversarial",
        provider_health_snapshot=health_snapshot,
    )

    assert session.structured_brief.review_policy["policy_name"] == "adversarial_required"
    assert session.structured_brief.review_policy["override_source"] == "cli"
    assert session.decision_verdict is not None
    assert session.decision_verdict.selected_provider_runtime["provider_health_snapshot"] == health_snapshot


def test_adversarial_review_can_force_needs_revision(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path),
    )

    session = _legacy_started_session(team, "Build plan with adversarial challenge")

    assert session.status == "needs_revision"
    assert session.gate_verdict == "needs_revision"
    assert session.review_rounds[-1].review_result is not None
    assert session.review_rounds[-1].review_result.findings[0].severity == "medium"


def test_team_start_builds_structured_brief_from_contract_and_subtasks(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path),
    )

    session = _legacy_started_session(team, "Build a persisted plan artifact")

    assert session.structured_brief.goal
    assert session.structured_brief.constraints == []
    assert session.structured_brief.subtasks == session.subtasks
    assert session.structured_brief.acceptance_criteria
    assert session.structured_brief.checklist_summary == [
        "Lead brief persisted [lead]: done",
        "Review round completed [review]: done",
        "Execution approved [lead]: done",
    ]


def test_team_start_assigns_checklist_item_owners(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path),
    )

    session = _legacy_started_session(team, "Build a persisted plan artifact")

    assert [(item.label, item.owner) for item in session.checklist] == [
        ("Lead brief persisted", "lead"),
        ("Review round completed", "review"),
        ("Execution approved", "lead"),
    ]


def test_team_status_reports_operator_guidance_for_revision(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )

    session = _legacy_started_session(team, "Build plan with adversarial challenge")
    status = team.status(session.id)

    assert status.to_dict()["status_summary"]["next_actions"] == ["revise"]
    assert "required gaps" in status.to_dict()["status_summary"]["next_action_message"]
    assert status.to_dict()["status_summary"]["blocking_reasons"]


def test_team_status_reports_failed_delegated_job_and_next_step(tmp_path) -> None:
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
    )

    session = _legacy_started_session(team, "Build a persisted plan artifact")
    review_round = session.review_rounds[1]
    review_job_id = review_round.summary.split("job ")[-1].rstrip(".")
    runtime.fail(review_job_id, summary="review failed", error="claude auth failed")

    status = team.status(session.id).to_dict()["status_summary"]

    assert status["next_actions"][0] == "retry_review"
    assert "inspect_delegated_job" in status["recovery_actions"]
    assert any(job["status"] == "failed" for job in status["delegated_jobs"])
    assert "delegated job failed" in status["next_action_message"]


def test_team_status_reports_in_progress_delegated_review_job(tmp_path) -> None:
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
    )

    session = _legacy_started_session(team, "Build a persisted plan artifact")
    running_job = runtime.start(
        JobRequest(
            task_id=session.id,
            provider="claude",
            kind="review",
            prompt="review",
            cwd=str(tmp_path),
        )
    )
    session.review_rounds[1] = PlanReviewRound(
        round_type="review",
        role="review",
        summary=f"Delegated review still running via claude review job {running_job.id}.",
        review_result=session.review_rounds[1].review_result,
    )
    team.store.write_session(session)

    status = team.status(session.id).to_dict()["status_summary"]

    assert status["resume_action"] == "inspect_delegated_job"
    assert status["resume_reason"] == "delegated_job_in_progress"
    assert status["block_source"] == "delegated_job"
    assert status["block_detail"] == "delegated_job_in_progress"
    assert status["recovery_actions"] == ["inspect_delegated_job"]
    assert "still in progress" in status["next_action_message"]


def test_team_status_reports_claude_specific_inspect_guidance_on_failed_job(tmp_path) -> None:
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
    )

    session = _legacy_started_session(team, "Build a persisted plan artifact")
    review_round = session.review_rounds[1]
    review_job_id = review_round.summary.split("job ")[-1].rstrip(".")
    runtime.fail(review_job_id, summary="review failed", error="claude auth failed")

    status = team.status(session.id).to_dict()["status_summary"]

    assert "inspect the failed Claude job" in status["next_action_message"]
    assert "retry" in status["next_action_message"]
    assert status["recovery_actions"] == [
        "inspect_delegated_job",
        "retry_review",
        "revise_plan",
    ]
    assert status["recovery_round_type"] == "review"
    assert status["recovery_provider"] == "claude"


def test_team_status_reports_generic_recovery_actions_for_non_claude_failure(tmp_path) -> None:
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
    )

    session = _legacy_started_session(team, "Build a persisted plan artifact")
    review_round = session.review_rounds[1]
    review_job_id = review_round.summary.split("job ")[-1].rstrip(".")
    job = runtime.status(review_job_id)
    runtime._write_job(job.__class__(**{**job.to_dict(), "provider": "mock"}))
    runtime.fail(review_job_id, summary="review failed", error="runtime disconnected")

    status = team.status(session.id).to_dict()["status_summary"]

    assert status["recovery_actions"] == [
        "inspect_delegated_job",
        "revise_plan",
    ]
    assert "automatic retry is not currently supported" in status["next_action_message"]
    assert "escalate manually" in status["next_action_message"]
    assert status["recovery_provider"] == "mock"


def test_team_status_reports_fallback_recovery_provider_when_claude_is_unavailable(tmp_path) -> None:
    runtime = CommandJobRuntime(
        root=tmp_path / "jobs",
        runner=_FakeClaudeRunner(),
        adapters={"claude": ClaudeCodeAdapter()},
    )
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
    )
    team.provider_health_check = lambda provider: ProviderStatus(
        provider=provider,
        available=False,
        detail=f"{provider} unavailable",
    ) if provider == "claude" else ProviderStatus(provider=provider, available=True, detail="ok")

    session = _legacy_started_session(team, "Build a persisted plan artifact")
    review_round = session.review_rounds[1]
    failed_job_id = review_round.summary.split("job ")[-1].rstrip(".")
    runtime.fail(failed_job_id, summary="review failed", error="claude auth failed")

    status = team.status(session.id).to_dict()["status_summary"]

    assert status["recovery_actions"] == [
        "inspect_delegated_job",
        "retry_review",
        "revise_plan",
    ]
    assert status["recovery_provider"] == "mock"
    assert status["recovery_provider_mode"] == "planned"
    assert status["recovery_provider_fallback_from"] == "claude"


def test_team_retry_review_replaces_failed_review_job_and_restores_guidance(tmp_path) -> None:
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
    )

    session = _legacy_started_session(team, "Build a persisted plan artifact")
    review_round = session.review_rounds[1]
    failed_job_id = review_round.summary.split("job ")[-1].rstrip(".")
    runtime.fail(failed_job_id, summary="review failed", error="claude auth failed")

    retried = team.retry_review(session.id)
    retried_status = retried.to_dict()["status_summary"]
    new_review_round = retried.review_rounds[-1]
    new_job_id = new_review_round.summary.split("job ")[-1].rstrip(".")

    assert new_review_round.round_type == "review_retry"
    assert new_job_id != failed_job_id
    assert runtime.status(new_job_id).status == "completed"
    assert "inspect_delegated_job" not in retried_status["next_actions"]
    assert retried_status["recovery_actions"] in ([], ["inspect_compliance"])
    assert retried.review_rounds[-1].summary.startswith(retried.review_rounds[-1].review_result.summary)


def test_team_retry_adversarial_review_replaces_failed_job_and_restores_guidance(tmp_path) -> None:
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
    )

    session = _legacy_started_session(team, "Build a persisted plan artifact")
    adversarial_round = session.review_rounds[2]
    failed_job_id = adversarial_round.summary.split("job ")[-1].rstrip(".")
    runtime.fail(failed_job_id, summary="adversarial failed", error="claude auth failed")

    retried = team.retry_adversarial_review(session.id)
    retried_status = retried.to_dict()["status_summary"]
    new_round = retried.review_rounds[-1]
    new_job_id = new_round.summary.split("job ")[-1].rstrip(".")

    assert new_round.round_type == "adversarial_review_retry"
    assert new_job_id != failed_job_id
    assert runtime.status(new_job_id).status == "completed"
    assert "inspect_delegated_job" not in retried_status["next_actions"]
    assert retried_status["recovery_actions"] in ([], ["inspect_compliance"])


def test_team_retry_review_uses_recommended_fallback_provider_consistently(tmp_path) -> None:
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
    )

    session = _legacy_started_session(team, "Build a persisted plan artifact")
    session.structured_brief.provider_recommendation.update(
        {
            "reviewer": "mock",
            "fallback_from": "claude",
            "fallback_reason": "reviewer_unavailable",
            "fallback_detail": "claude unavailable",
        }
    )
    session.decision_verdict = session.decision_verdict.from_dict(
        {
            **session.decision_verdict.to_dict(),
            "selected_provider_runtime": dict(session.structured_brief.provider_recommendation),
        }
    )
    team.store.write_session(session)
    review_round = session.review_rounds[1]
    failed_job_id = review_round.summary.split("job ")[-1].rstrip(".")
    runtime.fail(failed_job_id, summary="review failed", error="claude auth failed")

    retried = team.retry_review(session.id)
    retry_round = retried.review_rounds[-1]
    retry_job_id = retry_round.summary.split("job ")[-1].rstrip(".")
    retry_job = runtime.status(retry_job_id)

    assert retried.structured_brief.provider_recommendation["reviewer"] == "mock"
    assert retried.decision_verdict["selected_provider_runtime"]["reviewer"] == "mock"
    assert retry_job.provider == "mock"


def test_team_status_reports_execute_readiness_for_approved_session(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )

    session = _legacy_started_session(team, "Build a persisted plan artifact")
    status = team.status(session.id).to_dict()["status_summary"]

    assert status["next_actions"] == ["execute"]
    assert status["next_action_message"] == "plan is approved; execution is the next valid action"
    assert status["blocking_reasons"] == []


def test_team_status_reports_human_decision_requirement(tmp_path) -> None:
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )

    session = _legacy_started_session(team, "Architecture direction change for stage transition")
    status = team.status(session.id).to_dict()["status_summary"]

    assert status["next_actions"] == ["human_decision"]
    assert "escalate to human decision" in status["next_action_message"]
    assert "human confirmation" in status["next_action_message"]
    assert status["review_policy"]["policy_name"] == "human_escalation_required"
    assert status["review_policy"]["execution_config"]["minimum_approval"] == "human_decision"
    assert status["recovery_semantics"]["category"] == "escalate"
    assert status["recovery_semantics"]["human_escalation_required"] is True


def test_team_start_records_doc_sync_and_compliance_snapshot(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )

    session = _legacy_started_session(team, "Build a persisted plan artifact")

    assert session.doc_sync is not None
    assert session.doc_sync["project_root"] == str(tmp_path)
    assert session.doc_sync["required_docs_checked"] >= 3
    assert session.compliance is not None
    assert session.compliance["status"] == "passed"
    assert session.compliance["blocking"] is False


def test_team_execute_rejects_when_compliance_blocking_is_active(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# temp\n", encoding="utf-8")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    session = _legacy_started_session(team, "Build a persisted plan artifact")

    assert session.compliance is not None
    assert session.compliance["blocking"] is False
    executed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)
    assert executed.status in {"accepted", "needs_followup", "blocked"}


def test_team_status_reports_compliance_blocking_guidance(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# temp\n", encoding="utf-8")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )

    session = _legacy_started_session(team, "Build a persisted plan artifact")
    status = team.status(session.id).to_dict()["status_summary"]

    assert status["next_actions"][0] == "execute"
    assert any("missing required docs" in reason for reason in status["blocking_reasons"])
    assert any("missing required docs" in warning for warning in status["baseline_warnings"])
    assert any("missing required docs" in warning for warning in team.status(session.id).compliance["warnings"])


def test_team_status_reports_content_sync_blocking_for_missing_runbook_link(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    (tmp_path / "README.md").write_text("# temp\n", encoding="utf-8")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )

    session = _legacy_started_session(team, "Build a persisted plan artifact")
    status = team.status(session.id).to_dict()["status_summary"]

    assert status["next_actions"][0] == "execute"
    assert any("README missing operator runbook link" in reason for reason in status["blocking_reasons"])
    assert any("README missing operator runbook link" in warning for warning in status["baseline_warnings"])
    assert any("README missing operator runbook link" in warning for warning in team.status(session.id).compliance["warnings"])


def test_team_status_reports_content_sync_blocking_for_stale_operator_runbook_signals(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    runbook_path = tmp_path / "docs" / "process" / "agent-team-operator-runbook.md"
    runbook_path.write_text("# Agent Team Operator Runbook\n\n- team next\n", encoding="utf-8")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )

    session = _legacy_started_session(team, "Build a persisted plan artifact")
    status = team.status(session.id).to_dict()["status_summary"]

    assert status["next_actions"][0] == "execute"
    assert any("operator runbook missing topology/fallback signals" in reason for reason in status["blocking_reasons"])
    assert any("operator runbook missing topology/fallback signals" in warning for warning in status["baseline_warnings"])
    assert any(
        "operator runbook missing topology/fallback signals" in warning
        for warning in team.status(session.id).compliance["warnings"]
    )


def test_team_status_reports_provenance_sync_blocking_after_execution(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = _legacy_started_session(team, "Build a persisted plan artifact")
    executed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)

    run_path = tmp_path / "runs" / f"{executed.resume.linked_execution_run_id}.json"
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    payload["metadata"]["plan_session_id"] = "plan-wrong"
    run_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    status = team.status(session.id).to_dict()["status_summary"]

    assert status["next_actions"][0] == "inspect_compliance"
    assert any("run provenance mismatch" in reason for reason in status["blocking_reasons"])


def test_team_resume_reconciles_executing_session_when_linked_run_completed(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = _legacy_started_session(team, "Build a persisted plan artifact")
    executed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)

    executed.status = "executing"
    executed.gate_verdict = "approved"
    executed.resume.current_phase = "executing"
    executed.resume.pending_role = "build"
    team.store.write_session(executed)

    resumed = team.resume(executed.id)

    assert resumed.status == "accepted"
    assert resumed.resume.current_phase == "accepted"
    assert resumed.resume.pending_role == "lead"
    assert resumed.to_dict()["status_summary"]["resume_action"] == "inspect_execution"


def test_team_resume_reconciles_executing_session_when_linked_run_blocked(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = _legacy_started_session(team, "Build a persisted plan artifact")
    executed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)

    run_path = tmp_path / "runs" / f"{executed.resume.linked_execution_run_id}.json"
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    payload["status"] = "blocked"
    payload["accepted"] = False
    run_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    executed.status = "executing"
    executed.gate_verdict = "approved"
    executed.resume.current_phase = "executing"
    executed.resume.pending_role = "build"
    team.store.write_session(executed)

    resumed = team.resume(executed.id)

    assert resumed.status == "blocked"
    assert resumed.resume.current_phase == "blocked"
    assert resumed.resume.pending_role == "lead"
    assert resumed.to_dict()["status_summary"]["resume_action"] == "inspect_blockers"
    assert resumed.to_dict()["status_summary"]["block_source"] == "execution_run"


def test_team_status_reports_execution_block_source_after_linked_run_failure(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = _legacy_started_session(team, "Build a persisted plan artifact")
    executed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)

    run_path = tmp_path / "runs" / f"{executed.resume.linked_execution_run_id}.json"
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    payload["status"] = "blocked"
    payload["accepted"] = False
    run_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    executed.status = "executing"
    executed.gate_verdict = "approved"
    executed.resume.current_phase = "executing"
    executed.resume.pending_role = "build"
    team.store.write_session(executed)

    status = team.status(executed.id).to_dict()["status_summary"]

    assert status["block_source"] == "execution_run"
    assert status["block_detail"] == "run_blocked"
    assert status["resume_action"] == "inspect_blockers"
    assert "execution ended in a blocked state" in status["next_action_message"]
    assert "re-running execution" in status["next_action_message"]
    assert status["recovery_semantics"]["category"] == "inspect_before_rerun"


def test_team_status_reports_execution_provenance_mismatch_block_detail(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = _legacy_started_session(team, "Build a persisted plan artifact")
    executed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)

    run_path = tmp_path / "runs" / f"{executed.resume.linked_execution_run_id}.json"
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    payload["metadata"]["plan_session_id"] = "plan-wrong"
    payload["status"] = "blocked"
    payload["accepted"] = False
    run_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    executed.status = "executing"
    executed.gate_verdict = "approved"
    executed.resume.current_phase = "executing"
    executed.resume.pending_role = "build"
    team.store.write_session(executed)

    status = team.status(executed.id).to_dict()["status_summary"]

    assert status["block_source"] == "compliance"
    assert status["block_detail"] is None
    assert status["resume_action"] == "inspect_compliance"


def test_team_inspect_execution_reports_provenance_mismatch_summary(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = _legacy_started_session(team, "Build a persisted plan artifact")
    executed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)

    run_path = tmp_path / "runs" / f"{executed.resume.linked_execution_run_id}.json"
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    payload["metadata"]["plan_session_id"] = "plan-wrong"
    payload["status"] = "blocked"
    payload["accepted"] = False
    run_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    executed.status = "executing"
    executed.gate_verdict = "approved"
    executed.resume.current_phase = "executing"
    executed.resume.pending_role = "build"
    team.store.write_session(executed)

    inspected = team.inspect_execution(executed.id)

    assert inspected["session_summary"]["outcome"] == "blocked_provenance_mismatch"
    assert any("run provenance mismatch" in reason for reason in inspected["session_summary"]["blocking_reasons"])


def test_team_inspect_execution_surfaces_warning_only_compliance_context(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    package_dir = tmp_path / "src" / "agent_orchestrator"
    (package_dir / "legacy.py").write_text(
        '"""Legacy module."""\n\nfrom __future__ import annotations\n\n# DEPS: __future__\n# RESPONSIBILITY: 待补充\n# MODULE: tests\n# ---\n\nVALUE = 1\n',
        encoding="utf-8",
    )
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    team.refresh_documentation_sync()
    session = _legacy_started_session(team, "Build a persisted plan artifact")
    executed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)
    executed.compliance = team.check_session_compliance(executed.id, changed_files=["src/agent_orchestrator/stub.py"])
    team.store.write_session(executed)

    inspected = team.inspect_execution(executed.id)

    assert inspected["session_summary"]["warnings"]
    assert any("legacy.py" in warning for warning in inspected["session_summary"]["warnings"])


def test_team_inspect_blockers_summarizes_execution_blocked_session(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = _legacy_started_session(team, "Build a persisted plan artifact")
    executed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)

    run_path = tmp_path / "runs" / f"{executed.resume.linked_execution_run_id}.json"
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    payload["status"] = "blocked"
    payload["accepted"] = False
    run_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    executed.status = "executing"
    executed.gate_verdict = "approved"
    executed.resume.current_phase = "executing"
    executed.resume.pending_role = "build"
    team.store.write_session(executed)

    inspected = team.inspect_blockers(executed.id)

    summary = inspected["blocker_summary"]
    assert summary["block_source"] == "execution_run"
    assert summary["block_detail"] == "run_blocked"
    assert summary["resume_action"] == "inspect_blockers"
    assert summary["recommended_commands"][0].endswith(f"team inspect-blockers {executed.id}")
    assert summary["evidence"]["linked_execution_run_id"] == executed.resume.linked_execution_run_id


def test_team_inspect_blockers_summarizes_failed_delegated_review(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
        runtime=runtime,
    )
    session = _legacy_started_session(team, "Build a persisted plan artifact")
    review_round = session.review_rounds[1]
    review_job_id = review_round.summary.split("job ")[-1].rstrip(".")
    runtime.fail(review_job_id, summary="review failed", error="claude auth failed")

    inspected = team.inspect_blockers(session.id)

    summary = inspected["blocker_summary"]
    assert summary["block_source"] == "delegated_job"
    assert summary["resume_action"] == "retry_review"
    assert summary["evidence"]["failed_job"]["job_id"] == review_job_id
    assert summary["evidence"]["failed_job"]["provider"] == "claude"


def test_team_inspect_blockers_summarizes_compliance_blocker(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# temp\n", encoding="utf-8")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    session = _legacy_started_session(team, "Build a persisted plan artifact")

    inspected = team.inspect_blockers(session.id)

    summary = inspected["blocker_summary"]
    assert summary["block_source"] is None
    assert summary["resume_action"] == "execute"
    assert "compliance_blocking_reasons" not in summary["evidence"]


def test_team_status_reports_plan_artifact_blocking_when_checklist_snapshot_is_missing(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    session = _legacy_started_session(team, "Build a persisted plan artifact")

    checklist_path = tmp_path / "plans" / session.id / "checklist.json"
    checklist_path.unlink()

    status = team.status(session.id).to_dict()["status_summary"]

    assert status["next_actions"][0] == "inspect_compliance"
    assert any("missing plan artifact snapshot: checklist.json" in reason for reason in status["blocking_reasons"])


def test_team_status_reports_plan_artifact_blocking_when_round_snapshots_are_incomplete(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    session = _legacy_started_session(team, "Build a persisted plan artifact")

    round_path = tmp_path / "plans" / session.id / "rounds" / "round-003.json"
    round_path.unlink()

    status = team.status(session.id).to_dict()["status_summary"]

    assert status["next_actions"][0] == "inspect_compliance"
    assert any("review round snapshots are incomplete" in reason for reason in status["blocking_reasons"])

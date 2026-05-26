"""Planning governance models and team orchestration helpers."""
from __future__ import annotations

# DEPS: __future__, agent_orchestrator, dataclasses, json, pathlib, tempfile, typing, uuid
# RESPONSIBILITY: Coordinate planning governance sessions, compliance gating, and approved-plan execution handoff.
# MODULE: decision_core
# ---


import json
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal, TypedDict
from uuid import uuid4

from agent_orchestrator.agent_config import AgentConfig, AgentProfile
from agent_orchestrator.jobs import FileJobRuntime, JobRequest, JobRuntime
from agent_orchestrator.guards import (
    validate_artifact_write,
    validate_execution_gate,
    validate_role_state_action,
)
from agent_orchestrator.command import ProviderHealthCheck, ProviderStatus
from agent_orchestrator.events import EventStore
from agent_orchestrator.ideation import run_ideation
from agent_orchestrator.memory import MemoryStore
from agent_orchestrator.messages import MessageRouter, MessageStore
from agent_orchestrator.orchestrator import Orchestrator
from agent_orchestrator.policies import OrchestrationMode, get_policy
from agent_orchestrator.planning_support import (
    ProcessDocumentationBundle,
    ProcessDocumentSpec,
    SessionGuidance,
    build_compliance_status_for_session,
    build_doc_sync_status_for_project,
    build_session_guidance as _build_session_guidance_support,
    checklist_item_completed as _checklist_item_completed_support,
    collect_delegated_jobs as _collect_delegated_jobs_support,
    compliance_blocking_reasons as _compliance_blocking_reasons_support,
    compliance_warnings as _compliance_warnings_support,
    canonical_process_documentation_bundle,
    delegated_round_family as _delegated_round_family_support,
    execution_block_detail,
    extract_job_id as _extract_job_id_support,
    has_failed_delegated_family as _has_failed_delegated_family_support,
    latest_round as _latest_round_support,
    read_delegated_job_status as _read_delegated_job_status_support,
    resume_guidance_command as _resume_guidance_command_support,
)
from agent_orchestrator.review import Finding, ReviewResult
from agent_orchestrator.tasks import ExecutionContract
from agent_orchestrator.topology import TopologyName
from agent_orchestrator.work_graph import WorkGraphStore, build_initial_work_graph

TeamRole = Literal["lead", "build", "review"]
GapStatus = Literal["open", "acknowledged", "closed"]
PlanSessionStatus = Literal[
    "intake_chat",
    "draft_ready",
    "adversarial_review",
    "awaiting_human_confirmation",
    "drafting",
    "in_review",
    "needs_revision",
    "approved_for_execution",
    "executing",
    "accepted",
    "needs_followup",
    "blocked",
    "awaiting_human",
]
GateVerdict = Literal["approved", "needs_revision", "blocked", "accepted", "needs_followup"]
ApprovalStatus = Literal["approved", "needs_revision", "blocked", "accepted", "needs_followup"]
RoundType = Literal[
    "authoring",
    "lead_response",
    "review",
    "review_retry",
    "adversarial_review",
    "adversarial_review_retry",
    "revision",
    "approval",
]


class DelegatedJobSummary(TypedDict):
    round_type: str
    provider: str | None
    job_id: str
    status: str
    summary: str
    error: str | None


class PlanStatusSummary(TypedDict):
    phase: str
    pending_role: str | None
    open_required_gaps: int
    open_optional_followups: int
    next_actions: list[str]
    next_action_message: str
    primary_action: str
    primary_reason: str
    recommended_commands: list[str]
    recovery_actions: list[str]
    recovery_round_type: str | None
    recovery_provider: str | None
    recovery_provider_mode: str | None
    recovery_provider_fallback_from: str | None
    recovery_provider_fallback_reason: str | None
    recovery_provider_fallback_detail: str | None
    review_policy: dict[str, object]
    recovery_semantics: dict[str, object]
    blocking_reasons: list[str]
    warnings: list[str]
    baseline_warnings: list[str]
    block_source: str | None
    block_detail: str | None
    resume_action: str
    resume_reason: str
    delegated_jobs: list[DelegatedJobSummary]
    selected_topology: str | None
    topology_reason: str | None
    decision_rationale: list[str]
    approved_plan_ready: bool
    approved_plan_source: str | None


class BlockerEvidenceFailedJob(TypedDict):
    job_id: str | None
    provider: str | None
    round_type: str | None
    error: str | None


class BlockerEvidence(TypedDict, total=False):
    required_open_gaps: int
    optional_open_followups: int
    failed_job: BlockerEvidenceFailedJob
    linked_execution_run_id: str
    compliance_blocking_reasons: list[str]


class BlockerSessionSummary(TypedDict):
    session_id: str
    session_status: str
    block_source: str | None
    block_detail: str | None
    primary_action: str
    primary_reason: str
    resume_action: str
    resume_reason: str
    blocking_reasons: list[str]
    warnings: list[str]
    recommended_commands: list[str]
    recovery_actions: list[str]
    evidence: BlockerEvidence


class ExecutionSessionSummary(TypedDict):
    session_id: str
    run_id: str | None
    session_status: str
    outcome: str
    goal: str
    selected_topology: str | None
    selected_provider_runtime: dict[str, object] | None
    blocking_reasons: list[str]
    warnings: list[str]
    primary_action: str
    primary_reason: str
    resume_action: str
    resume_reason: str
    recommended_commands: list[str]


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:8]}"


@dataclass(slots=True)
class PlanSubtask:
    title: str
    expected_outputs: list[str]
    gate_conditions: list[str]
    owner: TeamRole = "build"
    id: str = field(default_factory=lambda: _new_id("subtask"))

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "expected_outputs": self.expected_outputs,
            "gate_conditions": self.gate_conditions,
            "owner": self.owner,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "PlanSubtask":
        return cls(
            title=str(data["title"]),
            expected_outputs=list(data.get("expected_outputs", [])),
            gate_conditions=list(data.get("gate_conditions", [])),
            owner=data.get("owner", "build"),
            id=str(data["id"]),
        )


@dataclass(slots=True)
class StructuredPlanBrief:
    goal: str
    constraints: list[str]
    subtasks: list[PlanSubtask]
    acceptance_criteria: list[str]
    open_questions: list[str]
    risks: list[str]
    checklist_summary: list[str]
    execution_intent: str = ""
    topology_recommendation: dict[str, object] = field(default_factory=dict)
    provider_recommendation: dict[str, object] = field(default_factory=dict)
    review_policy: dict[str, object] = field(default_factory=dict)
    decision_rationale: list[str] = field(default_factory=list)
    review_disputes: list[str] = field(default_factory=list)
    gating_requirements: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "goal": self.goal,
            "constraints": self.constraints,
            "subtasks": [subtask.to_dict() for subtask in self.subtasks],
            "acceptance_criteria": self.acceptance_criteria,
            "open_questions": self.open_questions,
            "risks": self.risks,
            "checklist_summary": self.checklist_summary,
            "execution_intent": self.execution_intent,
            "topology_recommendation": self.topology_recommendation,
            "provider_recommendation": self.provider_recommendation,
            "review_policy": self.review_policy,
            "decision_rationale": self.decision_rationale,
            "review_disputes": self.review_disputes,
            "gating_requirements": self.gating_requirements,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "StructuredPlanBrief":
        return cls(
            goal=str(data.get("goal", "")),
            constraints=[str(item) for item in data.get("constraints", [])],
            subtasks=[PlanSubtask.from_dict(item) for item in data.get("subtasks", [])],
            acceptance_criteria=[str(item) for item in data.get("acceptance_criteria", [])],
            open_questions=[str(item) for item in data.get("open_questions", [])],
            risks=[str(item) for item in data.get("risks", [])],
            checklist_summary=[str(item) for item in data.get("checklist_summary", [])],
            execution_intent=str(data.get("execution_intent", "")),
            topology_recommendation=dict(data.get("topology_recommendation", {})),
            provider_recommendation=dict(data.get("provider_recommendation", {})),
            review_policy=dict(data.get("review_policy", {})),
            decision_rationale=[str(item) for item in data.get("decision_rationale", [])],
            review_disputes=[str(item) for item in data.get("review_disputes", [])],
            gating_requirements=[str(item) for item in data.get("gating_requirements", [])],
        )


@dataclass(slots=True)
class DecisionVerdict:
    approval_status: ApprovalStatus
    required_gaps: list[dict[str, object]]
    followup_gaps: list[dict[str, object]]
    selected_topology: TopologyName
    selected_provider_runtime: dict[str, object]
    rationale: list[str]

    def __getitem__(self, key: str) -> object:
        return self.to_dict()[key]

    def to_dict(self) -> dict[str, object]:
        return {
            "approval_status": self.approval_status,
            "required_gaps": self.required_gaps,
            "followup_gaps": self.followup_gaps,
            "selected_topology": self.selected_topology,
            "selected_provider_runtime": self.selected_provider_runtime,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "DecisionVerdict":
        return cls(
            approval_status=data.get("approval_status", "needs_revision"),
            required_gaps=list(data.get("required_gaps", [])),
            followup_gaps=list(data.get("followup_gaps", [])),
            selected_topology=data.get("selected_topology", "team"),
            selected_provider_runtime=dict(data.get("selected_provider_runtime", {})),
            rationale=[str(item) for item in data.get("rationale", [])],
        )


@dataclass(slots=True)
class PlanChecklistItem:
    label: str
    owner: TeamRole = "lead"
    completed: bool = False
    depends_on: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: _new_id("check"))

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "owner": self.owner,
            "completed": self.completed,
            "status": "done" if self.completed else "pending",
            "depends_on": self.depends_on,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "PlanChecklistItem":
        return cls(
            label=str(data["label"]),
            owner=data.get("owner", "lead"),
            completed=bool(data.get("completed", False)),
            depends_on=[str(item) for item in data.get("depends_on", [])] if isinstance(data.get("depends_on", []), list) else [],
            id=str(data["id"]),
        )


@dataclass(slots=True)
class PlanGap:
    title: str
    severity: str
    recommendation: str
    required: bool = True
    status: GapStatus = "open"
    finding_round_id: str | None = None
    id: str = field(default_factory=lambda: _new_id("gap"))

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "severity": self.severity,
            "recommendation": self.recommendation,
            "required": self.required,
            "status": self.status,
            "finding_round_id": self.finding_round_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "PlanGap":
        return cls(
            id=str(data["id"]),
            title=str(data["title"]),
            severity=str(data["severity"]),
            recommendation=str(data.get("recommendation", "")),
            required=bool(data.get("required", True)),
            status=data.get("status", "open"),
            finding_round_id=data.get("finding_round_id"),
        )


@dataclass(slots=True)
class PlanResumeState:
    current_phase: str
    active_round_id: str | None
    pending_role: TeamRole
    submitted_at: str | None = None
    approved_at: str | None = None
    linked_execution_run_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "current_phase": self.current_phase,
            "active_round_id": self.active_round_id,
            "pending_role": self.pending_role,
            "submitted_at": self.submitted_at,
            "approved_at": self.approved_at,
            "linked_execution_run_id": self.linked_execution_run_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "PlanResumeState":
        return cls(
            current_phase=str(data["current_phase"]),
            active_round_id=data.get("active_round_id"),
            pending_role=data.get("pending_role", "lead"),
            submitted_at=data.get("submitted_at"),
            approved_at=data.get("approved_at"),
            linked_execution_run_id=data.get("linked_execution_run_id"),
        )


@dataclass(slots=True)
class PlanReviewRound:
    round_type: RoundType
    role: TeamRole
    summary: str
    id: str = field(default_factory=lambda: _new_id("round"))
    review_result: ReviewResult | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "round_type": self.round_type,
            "role": self.role,
            "summary": self.summary,
            "review_result": self.review_result.to_dict() if self.review_result else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "PlanReviewRound":
        review_payload = data.get("review_result")
        review_result = None
        if review_payload:
            review_result = ReviewResult(
                verdict=review_payload["verdict"],
                summary=str(review_payload["summary"]),
                findings=[
                    Finding(
                        severity=finding["severity"],
                        title=str(finding["title"]),
                        body=str(finding["body"]),
                        file=str(finding["file"]),
                        line_start=int(finding["line_start"]),
                        line_end=int(finding["line_end"]),
                        confidence=float(finding["confidence"]),
                        recommendation=str(finding["recommendation"]),
                    )
                    for finding in review_payload.get("findings", [])
                ],
                next_steps=list(review_payload.get("next_steps", [])),
            )
        return cls(
            id=str(data["id"]),
            round_type=data["round_type"],
            role=data["role"],
            summary=str(data["summary"]),
            review_result=review_result,
        )


@dataclass(slots=True, frozen=True)
class RoundOutcome:
    status: PlanSessionStatus
    gate_verdict: GateVerdict


@dataclass(slots=True)
class RoundController:
    def derive_post_review_outcome(self, findings: list[Finding]) -> RoundOutcome:
        if any(finding.severity == "critical" for finding in findings):
            return RoundOutcome(status="awaiting_human", gate_verdict="blocked")
        if any(finding.severity == "high" for finding in findings):
            return RoundOutcome(status="blocked", gate_verdict="blocked")
        if findings:
            return RoundOutcome(status="needs_revision", gate_verdict="needs_revision")
        return RoundOutcome(status="approved_for_execution", gate_verdict="approved")

    def validate_approve(self, session: "PlanSession") -> None:
        if session.status == "approved_for_execution":
            raise ValueError("team approve cannot re-approve a plan that is already approved")
        if session.status not in {"needs_revision", "awaiting_human_confirmation"}:
            raise ValueError("team approve requires a reviewed plan session awaiting human confirmation")
        if session.resume.current_phase not in {"in_review", "awaiting_human_confirmation"} or session.resume.pending_role != "lead":
            raise ValueError("team approve requires the lead review handoff to be active")
        if not _checklist_item_completed_support(session.checklist, "Review round completed"):
            raise ValueError("team approve requires the review round completed checklist item")
        if any(gap.required and gap.status != "closed" for gap in session.gaps):
            raise ValueError("team approve requires all open gaps to be closed before approval")

    def validate_execute(self, session: "PlanSession") -> None:
        validate_execution_gate(status=session.status, gate_verdict=session.gate_verdict)
        if session.resume.current_phase != "approved":
            raise ValueError("team execute requires a session in the approved phase before execution")
        if not _checklist_item_completed_support(session.checklist, "Execution approved"):
            raise ValueError("team execute requires the Execution approved checklist item")

    def normalize_resume(self, session: "PlanSession") -> "PlanSession":
        if (
            session.status in {"intake_chat", "draft_ready", "adversarial_review", "awaiting_human_confirmation"}
        ):
            session.resume.current_phase = session.status
            session.resume.pending_role = "lead"
            return session
        if (
            session.status == "executing"
            and session.resume.linked_execution_run_id
            and session.gate_verdict == "approved"
        ):
            session.resume.current_phase = "executing"
            session.resume.pending_role = "build"
            return session
        if session.status == "needs_revision":
            active_round_id = session.review_rounds[-1].id if session.review_rounds else None
            session.resume.current_phase = "in_review"
            session.resume.pending_role = "lead"
            session.resume.active_round_id = active_round_id
            return session
        if session.status == "approved_for_execution":
            if session.gate_verdict != "approved":
                raise ValueError("inconsistent approved session: verdict must be approved")
            if session.resume.current_phase not in {"approved", "drafting", "executing", "in_review"}:
                raise ValueError("inconsistent approved session: unexpected resume phase")
            session.resume.current_phase = "approved"
            session.resume.pending_role = "lead"
            return session
        if session.status == "executing":
            if session.gate_verdict != "approved":
                raise ValueError("inconsistent executing session: verdict must remain approved before completion")
            session.resume.current_phase = "executing"
            session.resume.pending_role = "build"
            return session
        if session.status in {"accepted", "needs_followup"}:
            session.resume.current_phase = session.status
            session.resume.pending_role = "lead"
            return session
        if session.status in {"blocked", "awaiting_human"}:
            session.resume.pending_role = "lead"
            return session
        return session

    def validate_revision(self, session: "PlanSession", closed_gap_ids: list[str]) -> None:
        if session.status not in {"needs_revision", "awaiting_human_confirmation"} or session.gate_verdict != "needs_revision":
            raise ValueError("team revise requires a reviewed plan session with open revision gaps")
        if not closed_gap_ids:
            raise ValueError("team revise requires at least one gap to close")
        known_gap_ids = {gap.id for gap in session.gaps}
        open_gap_ids = {gap.id for gap in session.gaps if gap.status != "closed"}
        unknown_gap_ids = [gap_id for gap_id in closed_gap_ids if gap_id not in known_gap_ids]
        if unknown_gap_ids:
            raise ValueError(f"team revise cannot close unknown gap ids: {', '.join(unknown_gap_ids)}")
        matched_open_gap_ids = [gap_id for gap_id in closed_gap_ids if gap_id in open_gap_ids]
        if not matched_open_gap_ids:
            raise ValueError("team revise requires at least one open gap to close")


@dataclass(slots=True)
class PlanSession:
    id: str
    requirement: str
    stage_target: str
    status: PlanSessionStatus
    lead_brief: str
    structured_brief: StructuredPlanBrief
    subtasks: list[PlanSubtask]
    gaps: list[PlanGap]
    approved_plan: dict[str, object] | None
    review_rounds: list[PlanReviewRound]
    checklist: list[PlanChecklistItem]
    resume: PlanResumeState
    gate_verdict: GateVerdict | None
    decision_verdict: DecisionVerdict | None = None
    doc_sync: dict[str, object] | None = None
    compliance: dict[str, object] | None = None

    @classmethod
    def new(cls, *, requirement: str, stage_target: str) -> "PlanSession":
        return cls(
            id=_new_id("plan"),
            requirement=requirement,
            stage_target=stage_target,
            status="drafting",
            lead_brief="",
            structured_brief=StructuredPlanBrief(
                goal="",
                constraints=[],
                subtasks=[],
                acceptance_criteria=[],
                open_questions=[],
                risks=[],
                checklist_summary=[],
            ),
            subtasks=[],
            gaps=[],
            approved_plan=None,
            review_rounds=[],
            checklist=[],
            resume=PlanResumeState(current_phase="drafting", active_round_id=None, pending_role="lead"),
            gate_verdict=None,
            decision_verdict=None,
            doc_sync=None,
            compliance=None,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "requirement": self.requirement,
            "stage_target": self.stage_target,
            "status": self.status,
            "lead_brief": self.lead_brief,
            "structured_brief": self.structured_brief.to_dict(),
            "subtasks": [subtask.to_dict() for subtask in self.subtasks],
            "gaps": [gap.to_dict() for gap in self.gaps],
            "approved_plan": self.approved_plan,
            "review_rounds": [round_.to_dict() for round_ in self.review_rounds],
            "checklist": [item.to_dict() for item in self.checklist],
            "resume": self.resume.to_dict(),
            "gate_verdict": self.gate_verdict,
            "decision_verdict": self.decision_verdict.to_dict() if self.decision_verdict else None,
            "status_summary": _build_status_summary(self),
            "doc_sync": self.doc_sync,
            "compliance": self.compliance,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "PlanSession":
        subtasks = [PlanSubtask.from_dict(item) for item in data.get("subtasks", [])]
        checklist = [PlanChecklistItem.from_dict(item) for item in data.get("checklist", [])]
        structured_brief_payload = data.get("structured_brief")
        structured_brief = (
            StructuredPlanBrief.from_dict(structured_brief_payload)
            if structured_brief_payload
            else _hydrate_legacy_structured_brief(data, subtasks, checklist)
        )
        return cls(
            id=str(data["id"]),
            requirement=str(data["requirement"]),
            stage_target=str(data["stage_target"]),
            status=data["status"],
            lead_brief=str(data.get("lead_brief", "")),
            structured_brief=structured_brief,
            subtasks=subtasks,
            gaps=[PlanGap.from_dict(item) for item in data.get("gaps", [])],
            approved_plan=data.get("approved_plan"),
            review_rounds=[PlanReviewRound.from_dict(item) for item in data.get("review_rounds", [])],
            checklist=checklist,
            resume=PlanResumeState.from_dict(data["resume"]),
            gate_verdict=data.get("gate_verdict"),
            decision_verdict=DecisionVerdict.from_dict(data["decision_verdict"]) if data.get("decision_verdict") else None,
            doc_sync=data.get("doc_sync"),
            compliance=data.get("compliance"),
        )


def _hydrate_legacy_structured_brief(
    data: dict[str, object],
    subtasks: list[PlanSubtask],
    checklist: list[PlanChecklistItem],
) -> StructuredPlanBrief:
    lead_brief = str(data.get("lead_brief", "")).strip()
    requirement = str(data.get("requirement", "")).strip()
    goal = requirement
    if lead_brief.startswith("Lead target:"):
        goal = lead_brief.split(":", 1)[1].strip() or requirement
    elif lead_brief:
        goal = lead_brief
    acceptance_criteria = _dedupe_preserve_order(
        gate_condition
        for subtask in subtasks
        for gate_condition in subtask.gate_conditions
    )
    checklist_summary = [
        f"{item.label} [{item.owner}]: {'done' if item.completed else 'pending'}"
        for item in checklist
    ]
    return StructuredPlanBrief(
        goal=goal,
        constraints=[],
        subtasks=subtasks,
        acceptance_criteria=acceptance_criteria,
        open_questions=[],
        risks=[],
        checklist_summary=checklist_summary,
        execution_intent="Turn the approved plan into the execution contract without going back to the raw requirement.",
    )


def _dedupe_preserve_order(items: list[str] | Any) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        value = str(item)
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


@dataclass(slots=True)
class PlanStore:
    root: Path | str = ".agent_orchestrator/plans"

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    def write_session(self, session: PlanSession) -> None:
        session_dir = self.root / session.id
        rounds_dir = session_dir / "rounds"
        rounds_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(session_dir / "session.json", session.to_dict())
        self._write_json(session_dir / "checklist.json", {"items": [item.to_dict() for item in session.checklist]})
        self._write_json(
            session_dir / "verdict.json",
            {
                "gate_verdict": session.gate_verdict,
                "status": session.status,
                "execution_run_id": session.resume.linked_execution_run_id,
                "decision_verdict": session.decision_verdict.to_dict() if session.decision_verdict else None,
            },
        )
        for index, round_ in enumerate(session.review_rounds, start=1):
            self._write_json(rounds_dir / f"round-{index:03d}.json", round_.to_dict())
        self.write_work_graph(session)
        EventStore(self.root.parent / "events").append(
            type="session.updated",
            scope="session",
            scope_id=session.id,
            message=f"Plan session {session.id} saved with status {session.status}.",
            payload={
                "session_id": session.id,
                "status": session.status,
                "phase": session.resume.current_phase,
                "work_graph_path": str(session_dir / "work_graph.json"),
            },
        )
        memory_store = MemoryStore(self.root.parent / "memory")
        memory_store.append(
            namespace="plan_session",
            session_id=session.id,
            record_type="session_snapshot",
            role="lead",
            provider="decision_core",
            summary=f"{session.status}: {session.requirement}",
            payload={
                "status": session.status,
                "gate_verdict": session.gate_verdict,
                "review_round_count": len(session.review_rounds),
                "gap_count": len(session.gaps),
            },
        )
        if session.status in {"blocked", "awaiting_human"}:
            memory_store.append(
                namespace="postmortem",
                session_id=session.id,
                record_type="postmortem",
                role="lead",
                provider="decision_core",
                summary=f"Session {session.id} blocked with status {session.status}.",
                payload={"status": session.status, "blocking_reasons": session.to_dict()["status_summary"]["blocking_reasons"]},
            )

    def read_session(self, session_id: str) -> PlanSession:
        session_dir = self.root / session_id
        payload = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
        return PlanSession.from_dict(payload)

    def write_work_graph(self, session: PlanSession) -> None:
        graph_store = WorkGraphStore(self.root)
        graph_store.write(
            build_initial_work_graph(
                session,
                existing=graph_store.read_optional(session.id),
                message_store=self.message_store(),
            )
        )

    def message_store(self) -> MessageStore:
        return MessageStore(self.root.parent / "messages")

    @staticmethod
    def _write_json(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(payload, ensure_ascii=False, indent=2)
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)
        tmp_path.replace(path)


@dataclass(slots=True)
class TeamOrchestrator:
    orchestrator: Orchestrator
    store: PlanStore = field(default_factory=PlanStore)
    stage_target: str = "Stage 2: Planning Governance Skeleton"
    runtime: JobRuntime = field(default_factory=FileJobRuntime)
    round_controller: RoundController = field(default_factory=RoundController)
    project_root: Path | str = field(default_factory=Path.cwd)
    provider_health_check: Any = field(default_factory=ProviderHealthCheck)
    agent_config: AgentConfig = field(default_factory=AgentConfig.defaults)

    def __post_init__(self) -> None:
        self.project_root = Path(self.project_root)

    def start(
        self,
        requirement: str,
        *,
        review_policy_override: str | None = None,
        provider_health_snapshot: dict[str, object] | None = None,
    ) -> PlanSession:
        policy = get_policy(OrchestrationMode.SUCCESS_FIRST)
        contract = self.orchestrator.planner.clarify(requirement, policy)
        work_units = self.orchestrator.decomposer.decompose(contract, policy)
        session = PlanSession.new(requirement=requirement, stage_target=self.stage_target)
        session.status = "intake_chat"
        session.resume.current_phase = "intake_chat"
        session.resume.pending_role = "lead"
        session.doc_sync = self._build_doc_sync_status()
        session.compliance = build_compliance_status_for_session(
            project_root=self.project_root,
            doc_sync=session.doc_sync,
            session=session,
            run_store=self.orchestrator.run_store,
            plans_root=self.store.root,
        )
        session.lead_brief = f"Lead target: {contract.goal}"
        session.subtasks = [
            PlanSubtask(
                title=work_unit.goal,
                expected_outputs=work_unit.outputs,
                gate_conditions=work_unit.acceptance_criteria,
            )
            for work_unit in work_units
        ]
        session.structured_brief = StructuredPlanBrief(
            goal=contract.goal,
            constraints=[],
            subtasks=session.subtasks,
            acceptance_criteria=_dedupe_preserve_order(
                gate_condition
                for subtask in session.subtasks
                for gate_condition in subtask.gate_conditions
            ),
            open_questions=[],
            risks=[],
            checklist_summary=[],
            execution_intent="Use the approved plan as the execution contract source of truth.",
            topology_recommendation=_recommend_topology(policy, requirement, session.subtasks),
            provider_recommendation=_recommend_provider_runtime(self.runtime),
            review_policy={},
            decision_rationale=[],
            review_disputes=[],
            gating_requirements=[
                "Required gaps must be closed before execution.",
                "Execution must start from the approved plan, not from the raw requirement.",
            ],
        )
        session.checklist = [
            PlanChecklistItem(label="Lead brief persisted", owner="lead", completed=True),
            PlanChecklistItem(label="Draft confirmed by human", owner="human", completed=False, depends_on=["Lead brief persisted"]),
            PlanChecklistItem(label="Review round completed", owner="review", completed=False, depends_on=["Draft confirmed by human"]),
            PlanChecklistItem(label="Execution approved", owner="lead", completed=False, depends_on=["Review round completed"]),
        ]
        planner_profile = self.agent_config.profile("planner")
        review_profile = self.agent_config.profile("plan_reviewer")
        adversarial_profile = self.agent_config.profile("adversarial_reviewer")
        research_provider = planner_profile.provider
        provider_recommendation = self._build_provider_recommendation()
        runtime_review_provider = str(provider_recommendation.get("reviewer", review_profile.provider))
        adversarial_review_provider = str(
            provider_recommendation.get("adversarial_reviewer", adversarial_profile.provider)
        )
        session.structured_brief.provider_recommendation = _recommend_provider_runtime(
            self.runtime,
            author_provider=str(provider_recommendation.get("author", "codex")),
            reviewer_provider=runtime_review_provider,
            adversarial_reviewer_provider=adversarial_review_provider,
            worker_model=self.agent_config.profile("worker").model,
            reviewer_model=review_profile.model,
            adversarial_reviewer_model=adversarial_profile.model,
            author_runtime_mode=self.agent_config.profile("worker").runtime_mode,
            reviewer_runtime_mode=review_profile.runtime_mode,
            adversarial_reviewer_runtime_mode=adversarial_profile.runtime_mode,
            author_fallback_from=_string_or_none(provider_recommendation.get("author_fallback_from")),
            author_fallback_reason=_string_or_none(provider_recommendation.get("author_fallback_reason")),
            author_fallback_detail=_string_or_none(provider_recommendation.get("author_fallback_detail")),
            reviewer_fallback_from=_string_or_none(provider_recommendation.get("fallback_from")),
            reviewer_fallback_reason=_string_or_none(provider_recommendation.get("fallback_reason")),
            reviewer_fallback_detail=_string_or_none(provider_recommendation.get("fallback_detail")),
        )
        if provider_health_snapshot:
            session.structured_brief.provider_recommendation["provider_health_snapshot"] = provider_health_snapshot
        session.structured_brief.review_policy = _recommend_review_policy(
            requirement,
            session.structured_brief.topology_recommendation,
        )
        session.structured_brief.review_policy = _apply_team_review_policy_override(
            session.structured_brief.review_policy,
            review_policy_override,
        )
        message_router = MessageRouter(self.store.message_store())

        lead_job = self._start_job(
            JobRequest(
                task_id=session.id,
                provider=research_provider,
                kind="research",
                prompt=planner_profile.render_prompt(
                    f"Lead planning round: {requirement}",
                    requirement=requirement,
                    session_id=session.id,
                ),
                cwd=str(Path.cwd()),
                model=planner_profile.model,
                reasoning_effort=planner_profile.reasoning_effort,  # type: ignore[arg-type]
                sandbox=planner_profile.sandbox,  # type: ignore[arg-type]
                runtime_mode=planner_profile.runtime_mode,
                metadata={"stage_target": self.stage_target, "role": "lead", "agent_profile": planner_profile.to_dict()},
            )
        )
        if hasattr(self.runtime, "complete"):
            lead_job = getattr(self.runtime, "complete")(
                lead_job.id,
                summary=f"Lead planning round completed for {session.id}.",
                stdout=session.lead_brief,
                parsed_payload={"lead_brief": session.lead_brief},
                phase="done",
            )
        author_round = PlanReviewRound(
            round_type="authoring",
            role="lead",
            summary=(
                f"Lead selected stage target {self.stage_target} and drafted {len(session.subtasks)} subtasks "
                f"via {lead_job.provider} job {lead_job.id}."
            ),
        )
        session.review_rounds = [author_round]
        session.resume.active_round_id = author_round.id
        validate_artifact_write("lead", "draft_plan")
        message_router.build_handoff(
            session_id=session.id,
            from_role="human",
            to_role="lead",
            content=requirement,
            work_unit_id=session.id,
            payload={"artifact_kind": "user_requirement", "stage": "intake_chat"},
        )
        message_router.build_handoff(
            session_id=session.id,
            from_role="lead",
            to_role="human",
            content=session.lead_brief,
            work_unit_id=author_round.id,
            payload={
                "artifact_kind": "draft_plan",
                "plan_version": len(session.review_rounds),
                "goal": session.structured_brief.goal,
                "subtask_ids": [subtask.id for subtask in session.subtasks],
                "next_stage": "draft_ready",
            },
        )
        session.structured_brief.risks = []
        session.structured_brief.review_disputes = _summarize_review_disputes(session.review_rounds)
        session.structured_brief.decision_rationale = _build_decision_rationale(requirement, session, policy)
        session.structured_brief.checklist_summary = _build_checklist_summary(session.checklist)
        session.decision_verdict = _build_decision_verdict(session, runtime=self.runtime)

        self.store.write_session(session)
        session.compliance = build_compliance_status_for_session(
            project_root=self.project_root,
            doc_sync=session.doc_sync,
            session=session,
            run_store=self.orchestrator.run_store,
            plans_root=self.store.root,
        )
        self.store.write_session(session)
        return session

    def chat_with_lead(self, session_id: str, *, message: str) -> PlanSession:
        session = self.store.read_session(session_id)
        if session.status not in {"intake_chat", "draft_ready", "awaiting_human_confirmation", "needs_revision"}:
            raise ValueError("lead chat is only available before approval or while human confirmation is pending")
        text = message.strip()
        if not text:
            raise ValueError("lead chat requires a non-empty message")
        validate_role_state_action("lead", "respond_to_user")
        router = MessageRouter(self.store.message_store())
        router.build_handoff(
            session_id=session.id,
            from_role="human",
            to_role="lead",
            content=text,
            work_unit_id=session.id,
            payload={"stage": session.status, "artifact_kind": "human_feedback"},
            requires_response=True,
        )
        response = f"Lead incorporated human feedback into draft plan: {text}"
        response_round = PlanReviewRound(round_type="lead_response", role="lead", summary=response)
        session.review_rounds.append(response_round)
        session.resume.active_round_id = response_round.id
        session.resume.current_phase = session.status
        session.resume.pending_role = "lead"
        session.lead_brief = f"{session.lead_brief}\nHuman feedback: {text}".strip()
        validate_artifact_write("lead", "lead_response")
        router.build_handoff(
            session_id=session.id,
            from_role="lead",
            to_role="human",
            content=response,
            work_unit_id=response_round.id,
            payload={
                "artifact_kind": "lead_response",
                "plan_version": len(session.review_rounds),
                "stage": session.status,
            },
        )
        session.structured_brief.checklist_summary = _build_checklist_summary(session.checklist)
        self.store.write_session(session)
        return session

    def mark_draft_ready(self, session_id: str) -> PlanSession:
        session = self.store.read_session(session_id)
        if session.status not in {"intake_chat", "draft_ready"}:
            raise ValueError("team draft-ready requires an intake_chat session")
        validate_role_state_action("lead", "submit_draft")
        session.status = "draft_ready"
        session.resume.current_phase = "draft_ready"
        session.resume.pending_role = "lead"
        session.resume.submitted_at = "draft_ready"
        _set_checklist_completed(session, "Draft confirmed by human", True)
        session.structured_brief.checklist_summary = _build_checklist_summary(session.checklist)
        MessageRouter(self.store.message_store()).build_handoff(
            session_id=session.id,
            from_role="human",
            to_role="lead",
            content="First draft confirmed for adversarial review.",
            work_unit_id=session.id,
            payload={"artifact_kind": "draft_plan", "stage": "draft_ready"},
        )
        self.store.write_session(session)
        return session

    def submit_draft_for_review(self, session_id: str) -> PlanSession:
        session = self.store.read_session(session_id)
        if session.status != "draft_ready":
            raise ValueError("team submit-review requires a draft_ready plan session")
        validate_role_state_action("lead", "submit_draft")
        review_profile = self.agent_config.profile("plan_reviewer")
        adversarial_profile = self.agent_config.profile("adversarial_reviewer")
        provider_recommendation = self._build_provider_recommendation()
        runtime_review_provider = str(provider_recommendation.get("reviewer", review_profile.provider))
        adversarial_review_provider = str(
            provider_recommendation.get("adversarial_reviewer", adversarial_profile.provider)
        )
        router = MessageRouter(self.store.message_store())

        session.status = "adversarial_review"
        session.resume.current_phase = "adversarial_review"
        session.resume.pending_role = "review"
        self.store.write_session(session)

        review_result = _review_plan(session.requirement, session)
        review_request = router.build_review_request(
            session_id=session.id,
            to_role="reviewer",
            content=f"Review the human-confirmed draft plan for: {session.requirement}",
            work_unit_id=session.id,
            payload={
                "artifact_kind": "draft_plan",
                "goal": session.structured_brief.goal,
                "subtask_ids": [subtask.id for subtask in session.subtasks],
                "round_type": "review",
            },
        )
        review_job = self._start_job(
            JobRequest(
                task_id=session.id,
                provider=runtime_review_provider,
                kind="review",
                prompt=review_profile.render_prompt(
                    f"Review human-confirmed draft plan: {session.requirement}",
                    requirement=session.requirement,
                    session_id=session.id,
                ),
                cwd=str(Path.cwd()),
                model=review_profile.model,
                reasoning_effort=review_profile.reasoning_effort,  # type: ignore[arg-type]
                sandbox=review_profile.sandbox,  # type: ignore[arg-type]
                runtime_mode=review_profile.runtime_mode,
                metadata={
                    "stage_target": self.stage_target,
                    "role": "reviewer",
                    "agent_profile": review_profile.to_dict(),
                    "work_unit_id": session.id,
                    "message_ids": [review_request.id],
                },
            )
        )
        if hasattr(self.runtime, "complete"):
            review_job = getattr(self.runtime, "complete")(
                review_job.id,
                summary=f"Review round completed for {session.id}.",
                stdout=review_result.summary,
                parsed_payload={"review_result": review_result.to_dict()},
                phase="reviewing",
            )
        review_round = PlanReviewRound(
            round_type="review",
            role="review",
            summary=f"{review_result.summary} via {review_job.provider} review job {review_job.id}.",
            review_result=review_result,
        )
        validate_artifact_write("reviewer", "review_findings")
        router.build_review_result(
            session_id=session.id,
            from_role="reviewer",
            content=review_result.summary,
            work_unit_id=review_round.id,
            payload={
                "artifact_kind": "review_findings",
                "job_id": review_job.id,
                "review_round_id": review_round.id,
                "review_result": review_result.to_dict(),
                "reply_to_message_id": review_request.id,
            },
        )

        adversarial_result = _adversarial_review_plan(session.requirement, session)
        adversarial_request = router.build_review_request(
            session_id=session.id,
            to_role="adversarial_reviewer",
            content=f"Challenge the reviewed draft plan adversarially for: {session.requirement}",
            work_unit_id=session.id,
            payload={
                "artifact_kind": "draft_plan",
                "goal": session.structured_brief.goal,
                "subtask_ids": [subtask.id for subtask in session.subtasks],
                "round_type": "adversarial_review",
                "counterpart_round_id": review_round.id,
            },
        )
        adversarial_job = self._start_job(
            JobRequest(
                task_id=session.id,
                provider=adversarial_review_provider,
                kind="adversarial_review",
                prompt=adversarial_profile.render_prompt(
                    f"Adversarial review human-confirmed draft plan: {session.requirement}",
                    requirement=session.requirement,
                    session_id=session.id,
                    counterpart_provider=runtime_review_provider,
                    counterpart_model=review_profile.model or "",
                ),
                cwd=str(Path.cwd()),
                model=adversarial_profile.model,
                reasoning_effort=adversarial_profile.reasoning_effort,  # type: ignore[arg-type]
                sandbox=adversarial_profile.sandbox,  # type: ignore[arg-type]
                runtime_mode=adversarial_profile.runtime_mode,
                metadata={
                    "stage_target": self.stage_target,
                    "role": "adversarial_reviewer",
                    "round_type": "adversarial_review",
                    "agent_profile": adversarial_profile.to_dict(),
                    "work_unit_id": session.id,
                    "message_ids": [adversarial_request.id],
                },
            )
        )
        if hasattr(self.runtime, "complete"):
            adversarial_job = getattr(self.runtime, "complete")(
                adversarial_job.id,
                summary=f"Adversarial review round completed for {session.id}.",
                stdout=adversarial_result.summary,
                parsed_payload={"review_result": adversarial_result.to_dict()},
                phase="reviewing",
            )
        adversarial_round = PlanReviewRound(
            round_type="adversarial_review",
            role="review",
            summary=f"{adversarial_result.summary} via {adversarial_job.provider} adversarial_review job {adversarial_job.id}.",
            review_result=adversarial_result,
        )
        validate_artifact_write("adversarial_reviewer", "review_findings")
        router.build_review_result(
            session_id=session.id,
            from_role="adversarial_reviewer",
            content=adversarial_result.summary,
            work_unit_id=adversarial_round.id,
            payload={
                "artifact_kind": "review_findings",
                "job_id": adversarial_job.id,
                "review_round_id": adversarial_round.id,
                "review_result": adversarial_result.to_dict(),
                "reply_to_message_id": adversarial_request.id,
            },
        )

        session.review_rounds.extend([review_round, adversarial_round])
        session.gaps = _build_plan_gaps(session.review_rounds)
        all_findings = [
            finding
            for round_ in session.review_rounds
            if round_.review_result
            for finding in round_.review_result.findings
        ]
        outcome = self.round_controller.derive_post_review_outcome(all_findings)
        session.status = "awaiting_human_confirmation"
        session.gate_verdict = outcome.gate_verdict
        session.resume.current_phase = "awaiting_human_confirmation"
        session.resume.pending_role = "lead"
        session.resume.active_round_id = adversarial_round.id
        _set_checklist_completed(session, "Review round completed", True)
        _set_checklist_completed(session, "Execution approved", False)
        session.structured_brief.risks = _summarize_plan_risks(all_findings)
        session.structured_brief.review_disputes = _summarize_review_disputes(session.review_rounds)
        session.structured_brief.checklist_summary = _build_checklist_summary(session.checklist)
        session.decision_verdict = _build_decision_verdict(session, runtime=self.runtime)
        self.store.write_session(session)
        return session

    def ideate(self, requirement: str) -> PlanSession:
        session = PlanSession.new(requirement=requirement, stage_target=self.stage_target)
        session.status = "drafting"
        session.resume.current_phase = "ideation"
        session.resume.pending_role = "lead"
        session.lead_brief = f"Ideation target: {requirement}"
        round_ = run_ideation(
            requirement=requirement,
            session_id=session.id,
            message_store=self.store.message_store(),
        )
        session.structured_brief.goal = requirement
        session.structured_brief.open_questions = [
            "Confirm the target user and success criteria before execution planning.",
            "Decide whether the skeptical risks require deeper research.",
        ]
        session.structured_brief.decision_rationale = [
            round_.proponent_summary,
            round_.skeptic_summary,
            round_.lead_synthesis,
        ]
        session.checklist = [
            PlanChecklistItem(label="Ideation debate completed", owner="lead", completed=True),
            PlanChecklistItem(label="Formal plan started", owner="lead", completed=False),
        ]
        self.store.write_session(session)
        retrieved_memory = MemoryStore(self.store.root.parent / "memory").search(requirement, limit=5)
        MemoryStore(self.store.root.parent / "memory").append(
            namespace="ideation",
            session_id=session.id,
            record_type="debate",
            role="lead",
            provider="decision_core",
            summary=round_.lead_synthesis,
            payload={**round_.to_dict(), "retrieved_memory": retrieved_memory},
        )
        return session

    def refresh_documentation_sync(self) -> dict[str, object]:
        bundle = canonical_process_documentation_bundle(self.project_root)
        refresh_results: list[dict[str, object]] = []
        for name, spec in bundle.iter_specs():
            path = self.project_root / spec.path
            expected = spec.render_markdown()
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current != expected:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(expected, encoding="utf-8")
                refresh_status = "created" if current is None else "updated"
            else:
                refresh_status = "unchanged"
            refresh_results.append(
                {
                    "name": name,
                    "path": spec.path,
                    "status": refresh_status,
                }
            )
        snapshot = build_doc_sync_status_for_project(self.project_root, self.runtime, refresh_results=refresh_results)
        return snapshot

    def _review_provider(self) -> str:
        return self._review_provider_status("claude").provider

    def _review_provider_status(self, provider: str) -> ProviderStatus:
        if self.runtime.__class__.__name__ != "CommandJobRuntime":
            return ProviderStatus(provider=provider, available=True, detail="mock runtime uses deterministic reviewer")
        checker = self.provider_health_check
        status = checker(provider) if callable(checker) else checker.check(provider)
        if status.available:
            return status
        return ProviderStatus(provider="mock", available=False, detail=status.detail)

    def _configured_review_provider(self, session: PlanSession, *, role: str = "reviewer") -> str | None:
        key = "adversarial_reviewer" if role == "adversarial_reviewer" else "reviewer"
        configured = session.structured_brief.provider_recommendation.get(key)
        return configured if isinstance(configured, str) and configured else None

    def _build_provider_recommendation(self) -> dict[str, object]:
        author_profile = self.agent_config.profile("worker")
        reviewer_profile = self.agent_config.profile("plan_reviewer")
        adversarial_profile = self.agent_config.profile("adversarial_reviewer")
        author_provider, author_status, author_fallback_from = self._resolve_provider_choice(
            preferred=author_profile.provider,
            fallbacks=("claude", "mock"),
        )
        reviewer_provider, reviewer_status, reviewer_fallback_from = self._resolve_provider_choice(
            preferred=reviewer_profile.provider,
            fallbacks=("codex", "claude", "mock"),
        )
        adversarial_provider, adversarial_status, adversarial_fallback_from = self._resolve_provider_choice(
            preferred=adversarial_profile.provider,
            fallbacks=("codex", "mock"),
        )
        recommendation: dict[str, object] = {
            "author": author_provider,
            "reviewer": reviewer_provider,
            "adversarial_reviewer": adversarial_provider,
            "author_model": author_profile.model,
            "reviewer_model": reviewer_profile.model,
            "adversarial_reviewer_model": adversarial_profile.model,
        }
        if author_fallback_from is not None and author_fallback_from != author_provider:
            recommendation["author_fallback_from"] = author_fallback_from
            recommendation["preferred_author"] = author_fallback_from
            recommendation["author_fallback_reason"] = "author_unavailable"
            recommendation["author_fallback_detail"] = author_status.detail
        if reviewer_fallback_from is not None and reviewer_fallback_from != reviewer_provider:
            recommendation["fallback_from"] = reviewer_fallback_from
            recommendation["preferred_reviewer"] = reviewer_fallback_from
            recommendation["fallback_reason"] = "reviewer_unavailable"
            recommendation["fallback_detail"] = reviewer_status.detail
        if adversarial_fallback_from is not None and adversarial_fallback_from != adversarial_provider:
            recommendation["adversarial_fallback_from"] = adversarial_fallback_from
            recommendation["preferred_adversarial_reviewer"] = adversarial_fallback_from
            recommendation["adversarial_fallback_reason"] = "adversarial_reviewer_unavailable"
            recommendation["adversarial_fallback_detail"] = adversarial_status.detail
        return recommendation

    def _resolve_provider_choice(
        self,
        *,
        preferred: str,
        fallbacks: tuple[str, ...],
    ) -> tuple[str, ProviderStatus, str | None]:
        preferred_status = self._provider_status(preferred)
        if preferred_status.available:
            return preferred, preferred_status, None
        for provider in fallbacks:
            fallback_status = self._provider_status(provider)
            if fallback_status.available:
                return provider, preferred_status, preferred
        return "mock", preferred_status, preferred

    def _provider_status(self, provider: str) -> ProviderStatus:
        if provider == "mock":
            return ProviderStatus(provider="mock", available=True, detail="mock provider is always available")
        if not _runtime_supports_provider(self.runtime, provider):
            return ProviderStatus(provider=provider, available=False, detail=f"{provider} runtime adapter unavailable")
        if self.runtime.__class__.__name__ != "CommandJobRuntime":
            return ProviderStatus(provider=provider, available=True, detail="non-command runtime accepts provider hints")
        checker = self.provider_health_check
        status = checker(provider) if callable(checker) else checker.check(provider)
        return status

    def _selected_retry_provider(self, session: PlanSession, runtime_status: Any, *, role: str = "reviewer") -> str:
        configured = self._configured_review_provider(session, role=role)
        if configured:
            return configured
        provider = getattr(runtime_status, "provider", None)
        if isinstance(provider, str) and provider:
            return provider
        return "mock"

    def _apply_retry_round_outcome(
        self,
        session: PlanSession,
        *,
        retry_round: PlanReviewRound,
        replaced_round_types: set[str],
        findings: list[Finding],
    ) -> PlanSession:
        session.review_rounds.append(retry_round)
        session.resume.active_round_id = retry_round.id
        session.resume.current_phase = "in_review"
        session.resume.pending_role = "lead"
        _set_checklist_completed(session, "Review round completed", True)

        historical_rounds = [round_ for round_ in session.review_rounds if round_.round_type not in replaced_round_types]
        session.gaps = _build_plan_gaps([*historical_rounds, retry_round])
        all_findings = [
            finding
            for round_ in historical_rounds
            if round_.review_result
            for finding in round_.review_result.findings
        ]
        all_findings.extend(findings)

        outcome = self.round_controller.derive_post_review_outcome(all_findings)
        session.status = outcome.status
        session.gate_verdict = outcome.gate_verdict
        if outcome.status == "approved_for_execution":
            session.resume.current_phase = "approved"
            session.resume.approved_at = "approved"
            _set_checklist_completed(session, "Execution approved", True)
        else:
            _set_checklist_completed(session, "Execution approved", False)

        session.structured_brief.risks = _summarize_plan_risks(all_findings)
        session.structured_brief.review_disputes = _summarize_review_disputes(session.review_rounds)
        session.structured_brief.checklist_summary = _build_checklist_summary(session.checklist)
        session.decision_verdict = _build_decision_verdict(session, runtime=self.runtime)
        if session.status == "approved_for_execution":
            session.approved_plan = _build_approved_plan(session)
        if self._can_refresh_process_docs():
            session.doc_sync = self.refresh_documentation_sync()
        self.store.write_session(session)
        session.compliance = build_compliance_status_for_session(
            project_root=self.project_root,
            doc_sync=session.doc_sync,
            session=session,
            run_store=self.orchestrator.run_store,
            plans_root=self.store.root,
        )
        self.store.write_session(session)
        return session

    def status(self, session_id: str) -> PlanSession:
        session = self.store.read_session(session_id)
        session.doc_sync = self._build_doc_sync_status()
        session.compliance = _merge_compliance_warning_snapshot(
            existing=session.compliance,
            refreshed=build_compliance_status_for_session(
                project_root=self.project_root,
                doc_sync=session.doc_sync,
                session=session,
                run_store=self.orchestrator.run_store,
                plans_root=self.store.root,
            ),
        )
        session = _reconcile_linked_execution_state(session, self.orchestrator.run_store)
        session.structured_brief.checklist_summary = _build_checklist_summary(session.checklist)
        return session

    def resume(self, session_id: str, apply: bool = False) -> PlanSession:
        session = self.store.read_session(session_id)
        session.doc_sync = self._build_doc_sync_status()
        session.compliance = _merge_compliance_warning_snapshot(
            existing=session.compliance,
            refreshed=build_compliance_status_for_session(
                project_root=self.project_root,
                doc_sync=session.doc_sync,
                session=session,
                run_store=self.orchestrator.run_store,
                plans_root=self.store.root,
            ),
        )
        session = _reconcile_linked_execution_state(session, self.orchestrator.run_store)
        normalized = self.round_controller.normalize_resume(session)
        normalized.structured_brief.checklist_summary = _build_checklist_summary(normalized.checklist)
        if apply:
            return _resume_apply_action(self, normalized)
        return normalized

    def approve(self, session_id: str) -> PlanSession:
        session = self.store.read_session(session_id)
        session.doc_sync = self._build_doc_sync_status()
        session.compliance = build_compliance_status_for_session(
            project_root=self.project_root,
            doc_sync=session.doc_sync,
            session=session,
            run_store=self.orchestrator.run_store,
            plans_root=self.store.root,
        )
        _validate_compliance_ready(session)
        self.round_controller.validate_approve(session)
        validate_role_state_action("lead", "approve_plan")
        session.status = "approved_for_execution"
        session.gate_verdict = "approved"
        session.resume.current_phase = "approved"
        session.resume.approved_at = "approved"
        _set_checklist_completed(session, "Execution approved", True)
        approval_round = PlanReviewRound(
            round_type="approval",
            role="lead",
            summary="Lead approved the revised plan for execution.",
        )
        session.review_rounds.append(approval_round)
        session.resume.active_round_id = approval_round.id
        session.structured_brief.decision_rationale = _build_decision_rationale(session.requirement, session, get_policy(OrchestrationMode.SUCCESS_FIRST))
        session.decision_verdict = _build_decision_verdict(session, runtime=self.runtime)
        validate_artifact_write("lead", "approved_plan")
        session.approved_plan = _build_approved_plan(session)
        session.structured_brief.checklist_summary = _build_checklist_summary(session.checklist)
        if self._can_refresh_process_docs():
            session.doc_sync = self.refresh_documentation_sync()
            session.compliance = build_compliance_status_for_session(
                project_root=self.project_root,
                doc_sync=session.doc_sync,
                session=session,
                run_store=self.orchestrator.run_store,
                plans_root=self.store.root,
            )
        self.store.write_session(session)
        MessageRouter(self.store.message_store()).build_handoff(
            session_id=session.id,
            from_role="lead",
            to_role="runtime",
            content="Plan approved for execution.",
            work_unit_id=session.id,
            payload={"artifact_kind": "approved_plan", "status": session.status, "gate_verdict": session.gate_verdict},
        )
        return session

    def revise(self, session_id: str, *, summary: str, closed_gap_ids: list[str]) -> PlanSession:
        session = self.store.read_session(session_id)
        session.doc_sync = self._build_doc_sync_status()
        session.compliance = build_compliance_status_for_session(
            project_root=self.project_root,
            doc_sync=session.doc_sync,
            session=session,
            run_store=self.orchestrator.run_store,
            plans_root=self.store.root,
        )
        self.round_controller.validate_revision(session, closed_gap_ids)
        closed_ids = set(closed_gap_ids)
        for gap in session.gaps:
            if gap.id in closed_ids:
                gap.status = "closed"
        revision_round = PlanReviewRound(
            round_type="revision",
            role="lead",
            summary=summary,
        )
        session.review_rounds.append(revision_round)
        session.resume.active_round_id = revision_round.id
        session.resume.current_phase = "in_review"
        session.resume.pending_role = "lead"
        session.decision_verdict = _build_decision_verdict(session, runtime=self.runtime)
        session.structured_brief.checklist_summary = _build_checklist_summary(session.checklist)
        session.doc_sync = self.refresh_documentation_sync()
        self.store.write_session(session)
        MessageRouter(self.store.message_store()).build_handoff(
            session_id=session.id,
            from_role="lead",
            to_role="reviewer",
            content=summary,
            work_unit_id=revision_round.id,
            payload={"closed_gap_ids": closed_gap_ids, "round_type": "revision"},
            requires_response=True,
        )
        session.compliance = build_compliance_status_for_session(
            project_root=self.project_root,
            doc_sync=session.doc_sync,
            session=session,
            run_store=self.orchestrator.run_store,
            plans_root=self.store.root,
        )
        self.store.write_session(session)
        return session

    def execute(
        self,
        session_id: str,
        mode: OrchestrationMode | None = OrchestrationMode.SUCCESS_FIRST,
        *,
        review_policy_override: str | None = None,
        provider_health_snapshot: dict[str, object] | None = None,
    ) -> PlanSession:
        session = self.store.read_session(session_id)
        session.doc_sync = self._build_doc_sync_status()
        session.compliance = build_compliance_status_for_session(
            project_root=self.project_root,
            doc_sync=session.doc_sync,
            session=session,
            run_store=self.orchestrator.run_store,
            plans_root=self.store.root,
        )
        _validate_compliance_ready(session)
        self.round_controller.validate_execute(session)
        validate_role_state_action("builder", "execute_work_unit")
        if session.approved_plan is None:
            raise ValueError("team execute requires an approved plan artifact before execution")
        if review_policy_override not in {None, "", "auto"} or provider_health_snapshot:
            session.structured_brief.review_policy = _apply_team_review_policy_override(
                session.structured_brief.review_policy,
                review_policy_override,
            )
            if provider_health_snapshot:
                session.structured_brief.provider_recommendation["provider_health_snapshot"] = provider_health_snapshot
            session.decision_verdict = _build_decision_verdict(session, runtime=self.runtime, approval_status="approved")
            session.approved_plan = _build_approved_plan(session)

        self.orchestrator.run_store.__post_init__()
        session.status = "executing"
        session.resume.current_phase = "executing"
        session.resume.pending_role = "build"
        self.store.write_session(session)

        execution_requirement = session.approved_plan["goal"] if session.approved_plan else session.requirement
        run = self.orchestrator.run(
            execution_requirement,
            mode,
            review_policy_override=review_policy_override,
            provider_health_snapshot=provider_health_snapshot,
        )
        payload = run.to_dict()
        metadata = dict(payload.get("metadata", {}))
        provenance = dict(metadata.get("provenance", {}))
        provenance.update(
            {
                "plan_session_id": session.id,
                "approved_plan_goal": execution_requirement,
                "selected_topology": session.decision_verdict.selected_topology if session.decision_verdict else None,
                "selected_provider_runtime": session.decision_verdict.selected_provider_runtime if session.decision_verdict else {},
                "decision_rationale": session.decision_verdict.rationale if session.decision_verdict else [],
            }
        )
        metadata.update(
            {
                "approved_plan": session.approved_plan,
                "plan_session_id": session.id,
                "approved_plan_summary": {
                    "session_id": session.id,
                    "goal": session.approved_plan.get("goal") if session.approved_plan else execution_requirement,
                    "selected_topology": session.decision_verdict.selected_topology if session.decision_verdict else None,
                    "selected_provider_runtime": session.decision_verdict.selected_provider_runtime if session.decision_verdict else {},
                    "review_policy": session.approved_plan.get("review_policy", {}) if session.approved_plan else {},
                    "fallback_policy": session.approved_plan.get("execution_contract", {}).get("fallback_policy", {}) if session.approved_plan else {},
                },
                "provider_health_snapshot": provider_health_snapshot or metadata.get("provider_health_snapshot"),
                "provenance": provenance,
            }
        )
        payload["metadata"] = metadata
        self.orchestrator.run_store.write(run.run_id, payload)
        session.resume.linked_execution_run_id = run.run_id
        lead_verdict = _finalize_execution(session, run)
        session.gate_verdict = lead_verdict
        session.status = lead_verdict
        session.resume.current_phase = lead_verdict
        session.resume.pending_role = "lead"
        session.decision_verdict = _build_decision_verdict(session, runtime=self.runtime, approval_status=lead_verdict)
        session.approved_plan = _build_approved_plan(session)
        session.structured_brief.checklist_summary = _build_checklist_summary(session.checklist)
        session.doc_sync = self.refresh_documentation_sync()
        self.store.write_session(session)
        MessageRouter(self.store.message_store()).build_handoff(
            session_id=session.id,
            from_role="lead",
            to_role="runtime",
            content=f"Execution started from approved plan via run {run.run_id}.",
            work_unit_id=run.run_id,
            payload={"run_id": run.run_id, "status": session.status},
        )
        session.compliance = build_compliance_status_for_session(
            project_root=self.project_root,
            doc_sync=session.doc_sync,
            session=session,
            run_store=self.orchestrator.run_store,
            plans_root=self.store.root,
        )
        self.store.write_session(session)
        return session

    def inspect_execution(self, session_id: str) -> dict[str, object]:
        session = self.store.read_session(session_id)
        run_id = session.resume.linked_execution_run_id
        if not run_id:
            raise ValueError("team inspect-execution requires a session with a linked execution run")
        if not self.orchestrator.run_store.exists(run_id):
            raise ValueError("team inspect-execution could not find the linked execution run artifact")
        session.doc_sync = self._build_doc_sync_status()
        session.compliance = _merge_compliance_warning_snapshot(
            existing=session.compliance,
            refreshed=build_compliance_status_for_session(
                project_root=self.project_root,
                doc_sync=session.doc_sync,
                session=session,
                run_store=self.orchestrator.run_store,
                plans_root=self.store.root,
            ),
        )
        session = _reconcile_linked_execution_state(session, self.orchestrator.run_store)
        payload = self.orchestrator.run_store.read(run_id)
        if isinstance(payload, dict):
            payload["session_summary"] = _build_execution_session_summary(session, payload)
        return payload

    def inspect_blockers(self, session_id: str) -> dict[str, object]:
        session = self.store.read_session(session_id)
        session.doc_sync = self._build_doc_sync_status()
        session.compliance = _merge_compliance_warning_snapshot(
            existing=session.compliance,
            refreshed=build_compliance_status_for_session(
                project_root=self.project_root,
                doc_sync=session.doc_sync,
                session=session,
                run_store=self.orchestrator.run_store,
                plans_root=self.store.root,
            ),
        )
        session = _reconcile_linked_execution_state(session, self.orchestrator.run_store)
        payload = session.to_dict()
        payload["blocker_summary"] = _build_blocker_session_summary(session)
        if session.resume.linked_execution_run_id and self.orchestrator.run_store.exists(session.resume.linked_execution_run_id):
            payload["linked_execution_run"] = {
                "run_id": session.resume.linked_execution_run_id,
                "exists": True,
            }
        return payload

    def _build_doc_sync_status(self) -> dict[str, object]:
        return build_doc_sync_status_for_project(self.project_root, self.runtime)

    def _can_refresh_process_docs(self) -> bool:
        return (self.project_root / "docs" / "process").exists()

    def _build_compliance_status(
        self,
        doc_sync: dict[str, object] | None,
        *,
        session: PlanSession | None = None,
        changed_files: list[str] | None = None,
    ) -> dict[str, object]:
        return build_compliance_status_for_session(
            project_root=self.project_root,
            doc_sync=doc_sync,
            session=session,
            run_store=self.orchestrator.run_store if session is not None else None,
            plans_root=self.store.root,
            changed_files=changed_files,
        )

    def check_compliance(self, changed_files: list[str] | None = None) -> dict[str, object]:
        doc_sync = build_doc_sync_status_for_project(
            self.project_root,
            self.runtime,
            changed_files=changed_files,
        )
        return build_compliance_status_for_session(
            project_root=self.project_root,
            doc_sync=doc_sync,
            plans_root=self.store.root,
            changed_files=changed_files,
        )

    def check_session_compliance(self, session_id: str, changed_files: list[str] | None = None) -> dict[str, object]:
        session = self.store.read_session(session_id)
        doc_sync = build_doc_sync_status_for_project(
            self.project_root,
            self.runtime,
            changed_files=changed_files,
        )
        return build_compliance_status_for_session(
            project_root=self.project_root,
            doc_sync=doc_sync,
            session=session,
            run_store=self.orchestrator.run_store,
            plans_root=self.store.root,
            changed_files=changed_files,
        )

    def retry_review(self, session_id: str) -> PlanSession:
        session = self.store.read_session(session_id)
        review_round = _latest_round_support(session.review_rounds, "review")
        if review_round is None:
            raise ValueError("team retry-review requires an existing review round")
        review_job_id = _extract_job_id_support(review_round.summary)
        runtime_status = _read_delegated_job_status_support(session, review_job_id) if review_job_id else None
        if runtime_status is None or runtime_status.status != "failed":
            raise ValueError("team retry-review requires a failed delegated review job")

        review_profile = self.agent_config.profile("plan_reviewer")
        review_provider = self._selected_retry_provider(session, runtime_status)
        review_result = _review_plan(session.requirement, session)
        message_router = MessageRouter(self.store.message_store())
        retry_request = message_router.build_review_request(
            session_id=session.id,
            to_role="reviewer",
            content=f"Retry review for: {session.requirement}",
            work_unit_id=review_round.id,
            payload={"round_type": "review_retry", "failed_job_id": review_job_id},
        )
        review_job = self._start_job(
            JobRequest(
                task_id=session.id,
                provider=review_provider,
                kind="review",
                prompt=review_profile.render_prompt(
                    f"Retry review planning round: {session.requirement}",
                    requirement=session.requirement,
                    session_id=session.id,
                ),
                cwd=str(Path.cwd()),
                model=review_profile.model,
                reasoning_effort=review_profile.reasoning_effort,  # type: ignore[arg-type]
                sandbox=review_profile.sandbox,  # type: ignore[arg-type]
                runtime_mode=review_profile.runtime_mode,
                metadata={
                    "stage_target": self.stage_target,
                    "role": "reviewer",
                    "round_type": "review_retry",
                    "agent_profile": review_profile.to_dict(),
                    "work_unit_id": review_round.id,
                    "message_ids": [retry_request.id],
                },
            )
        )
        if hasattr(self.runtime, "complete"):
            review_job = getattr(self.runtime, "complete")(
                review_job.id,
                summary=f"Retry review round completed for {session.id}.",
                stdout=review_result.summary,
                parsed_payload={"review_result": review_result.to_dict()},
                phase="reviewing",
            )

        retry_round = PlanReviewRound(
            round_type="review_retry",
            role="review",
            summary=f"{review_result.summary} via {review_job.provider} review job {review_job.id}.",
            review_result=review_result,
        )
        message_router.build_review_result(
            session_id=session.id,
            from_role="reviewer",
            content=review_result.summary,
            work_unit_id=retry_round.id,
            payload={
                "job_id": review_job.id,
                "review_round_id": retry_round.id,
                "review_result": review_result.to_dict(),
                "reply_to_message_id": retry_request.id,
            },
        )
        return self._apply_retry_round_outcome(
            session,
            retry_round=retry_round,
            replaced_round_types={"review", "review_retry"},
            findings=list(review_result.findings),
        )

    def retry_adversarial_review(self, session_id: str) -> PlanSession:
        session = self.store.read_session(session_id)
        adversarial_round = _latest_round_support(session.review_rounds, "adversarial_review")
        if adversarial_round is None:
            raise ValueError("team retry-adversarial-review requires an existing adversarial review round")
        job_id = _extract_job_id_support(adversarial_round.summary)
        runtime_status = _read_delegated_job_status_support(session, job_id) if job_id else None
        if runtime_status is None or runtime_status.status != "failed":
            raise ValueError("team retry-adversarial-review requires a failed delegated adversarial review job")

        adversarial_profile = self.agent_config.profile("adversarial_reviewer")
        review_provider = self._selected_retry_provider(session, runtime_status, role="adversarial_reviewer")
        adversarial_result = _adversarial_review_plan(session.requirement, session)
        message_router = MessageRouter(self.store.message_store())
        retry_request = message_router.build_review_request(
            session_id=session.id,
            to_role="adversarial_reviewer",
            content=f"Retry adversarial review for: {session.requirement}",
            work_unit_id=adversarial_round.id,
            payload={"round_type": "adversarial_review_retry", "failed_job_id": job_id},
        )
        retry_job = self._start_job(
            JobRequest(
                task_id=session.id,
                provider=review_provider,
                kind="adversarial_review",
                prompt=adversarial_profile.render_prompt(
                    f"Retry adversarial review planning round: {session.requirement}",
                    requirement=session.requirement,
                    session_id=session.id,
                ),
                cwd=str(Path.cwd()),
                model=adversarial_profile.model,
                reasoning_effort=adversarial_profile.reasoning_effort,  # type: ignore[arg-type]
                sandbox=adversarial_profile.sandbox,  # type: ignore[arg-type]
                runtime_mode=adversarial_profile.runtime_mode,
                metadata={
                    "stage_target": self.stage_target,
                    "role": "adversarial_reviewer",
                    "round_type": "adversarial_review_retry",
                    "agent_profile": adversarial_profile.to_dict(),
                    "work_unit_id": adversarial_round.id,
                    "message_ids": [retry_request.id],
                },
            )
        )
        if hasattr(self.runtime, "complete"):
            retry_job = getattr(self.runtime, "complete")(
                retry_job.id,
                summary=f"Retry adversarial review round completed for {session.id}.",
                stdout=adversarial_result.summary,
                parsed_payload={"review_result": adversarial_result.to_dict()},
                phase="reviewing",
            )

        retry_round = PlanReviewRound(
            round_type="adversarial_review_retry",
            role="review",
            summary=f"{adversarial_result.summary} via {retry_job.provider} adversarial_review job {retry_job.id}.",
            review_result=adversarial_result,
        )
        message_router.build_review_result(
            session_id=session.id,
            from_role="adversarial_reviewer",
            content=adversarial_result.summary,
            work_unit_id=retry_round.id,
            payload={
                "job_id": retry_job.id,
                "review_round_id": retry_round.id,
                "review_result": adversarial_result.to_dict(),
                "reply_to_message_id": retry_request.id,
            },
        )
        return self._apply_retry_round_outcome(
            session,
            retry_round=retry_round,
            replaced_round_types={"adversarial_review", "adversarial_review_retry"},
            findings=list(adversarial_result.findings),
        )

    def _start_job(self, request: JobRequest) -> Any:
        if request.provider == "mock" and not _runtime_supports_provider(self.runtime, "mock"):
            return FileJobRuntime.start(self.runtime, request)
        return self.runtime.start(request)


def _merge_compliance_warning_snapshot(
    *,
    existing: dict[str, object] | None,
    refreshed: dict[str, object],
) -> dict[str, object]:
    if not isinstance(existing, dict):
        return refreshed
    refreshed_blocking_reasons = [str(item) for item in refreshed.get("blocking_reasons", [])]
    if refreshed_blocking_reasons:
        return refreshed

    existing_warnings = [str(item) for item in existing.get("warnings", [])]
    if not existing_warnings:
        return refreshed

    merged = dict(refreshed)
    warning_values = [str(item) for item in refreshed.get("warnings", [])]
    for warning in existing_warnings:
        if warning not in warning_values:
            warning_values.append(warning)
    merged["warnings"] = warning_values
    if warning_values and str(merged.get("status", "passed")) == "passed":
        merged["status"] = "warning"
    return merged


def _find_module_docstring_end(lines: list[str]) -> int | None:
    if not lines:
        return None
    first = lines[0].strip()
    if not first.startswith('"""'):
        return None
    if first.count('"""') >= 2 and first != '"""':
        return 0
    for index, line in enumerate(lines[1:], start=1):
        if '"""' in line:
            return index
    return None

def _runtime_supports_provider(runtime: JobRuntime, provider: str) -> bool:
    adapters = getattr(runtime, "adapters", None)
    if isinstance(adapters, dict):
        return provider in adapters
    return True


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _reconcile_linked_execution_state(session: PlanSession, run_store: Any | None) -> PlanSession:
    if session.status != "executing" or not session.resume.linked_execution_run_id or run_store is None:
        return session
    try:
        payload = run_store.read(session.resume.linked_execution_run_id)
    except Exception:
        payload = {}
    if not isinstance(payload, dict) or not payload:
        return session
    run_status = str(payload.get("status", ""))
    if run_status not in {"completed", "blocked", "failed", "cancelled"}:
        return session
    metadata = payload.get("metadata", {}) if isinstance(payload.get("metadata"), dict) else {}
    approved_plan = metadata.get("approved_plan", {}) if isinstance(metadata.get("approved_plan"), dict) else {}
    if approved_plan and approved_plan.get("session_id") not in {None, session.id}:
        return session

    if run_status == "completed":
        session.status = _finalize_execution_from_payload(session, payload)
        session.gate_verdict = session.status
        session.resume.current_phase = session.status
        session.resume.pending_role = "lead"
        if session.decision_verdict is not None:
            session.decision_verdict = DecisionVerdict(
                approval_status=session.status,
                required_gaps=[gap.to_dict() for gap in session.gaps if gap.required and gap.status != "closed"],
                followup_gaps=[gap.to_dict() for gap in session.gaps if not gap.required and gap.status != "closed"],
                selected_topology=session.decision_verdict.selected_topology,
                selected_provider_runtime=session.decision_verdict.selected_provider_runtime,
                rationale=session.decision_verdict.rationale,
            )
        return session

    session.status = "blocked"
    session.gate_verdict = "blocked"
    session.resume.current_phase = "blocked"
    session.resume.pending_role = "lead"
    if session.decision_verdict is not None:
        session.decision_verdict = DecisionVerdict(
            approval_status="blocked",
            required_gaps=[gap.to_dict() for gap in session.gaps if gap.required and gap.status != "closed"],
            followup_gaps=[gap.to_dict() for gap in session.gaps if not gap.required and gap.status != "closed"],
            selected_topology=session.decision_verdict.selected_topology,
            selected_provider_runtime=session.decision_verdict.selected_provider_runtime,
            rationale=session.decision_verdict.rationale,
        )
    return session


def _finalize_execution_from_payload(session: PlanSession, payload: dict[str, object]) -> Literal["accepted", "needs_followup", "blocked"]:
    findings = [
        finding
        for round_ in session.review_rounds
        if round_.review_result
        for finding in round_.review_result.findings
    ]
    if any(finding.severity in {"high", "critical"} for finding in findings):
        return "blocked"
    if any(finding.severity in {"low", "medium"} for finding in findings):
        return "needs_followup"
    return "accepted" if bool(payload.get("accepted", False)) else "blocked"


def _execution_block_detail(session: PlanSession) -> str | None:
    if not session.compliance or not isinstance(session.compliance, dict):
        return None
    reasons = [str(item) for item in session.compliance.get("blocking_reasons", [])]
    if any("run provenance mismatch" in reason for reason in reasons):
        return "provenance_mismatch"
    if session.resume.linked_execution_run_id and session.status == "blocked":
        return "run_blocked"
    return None


@dataclass(frozen=True, slots=True)
class SessionGuidance:
    session_id: str
    primary_action: str
    primary_reason: str
    resume_action: str
    resume_reason: str
    block_source: str | None
    block_detail: str | None
    recommended_commands: list[str]
    recovery_actions: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "primary_action": self.primary_action,
            "primary_reason": self.primary_reason,
            "resume_action": self.resume_action,
            "resume_reason": self.resume_reason,
            "block_source": self.block_source,
            "block_detail": self.block_detail,
            "recommended_commands": list(self.recommended_commands),
            "recovery_actions": list(self.recovery_actions),
        }


BLOCK_SOURCES = {"compliance", "delegated_job", "execution_run", "review", "awaiting_human"}


def build_session_guidance(session: PlanSession) -> SessionGuidance:
    return _build_session_guidance_support(session)


def _build_execution_session_summary(session: PlanSession, payload: dict[str, object]) -> ExecutionSessionSummary:
    guidance = build_session_guidance(session)
    metadata = payload.get("metadata", {}) if isinstance(payload.get("metadata"), dict) else {}
    provenance = metadata.get("provenance", {}) if isinstance(metadata.get("provenance"), dict) else {}
    approved_plan_summary = (
        metadata.get("approved_plan_summary", {})
        if isinstance(metadata.get("approved_plan_summary"), dict)
        else {}
    )
    compliance = session.compliance if isinstance(session.compliance, dict) else {}
    blocking_reasons = [str(item) for item in compliance.get("blocking_reasons", [])]
    warnings = [str(item) for item in compliance.get("warnings", [])]
    outcome = "accepted" if bool(payload.get("accepted", False)) else str(payload.get("status", "unknown"))
    if session.status == "needs_followup":
        outcome = "needs_followup"
    if session.status == "blocked":
        detail = _execution_block_detail(session)
        if detail == "provenance_mismatch":
            outcome = "blocked_provenance_mismatch"
        elif detail == "run_blocked":
            outcome = "blocked_execution_run"
    return {
        "session_id": session.id,
        "run_id": session.resume.linked_execution_run_id,
        "session_status": session.status,
        "outcome": outcome,
        "goal": approved_plan_summary.get("goal") or provenance.get("approved_plan_goal") or session.requirement,
        "selected_topology": approved_plan_summary.get("selected_topology") or provenance.get("selected_topology"),
        "selected_provider_runtime": approved_plan_summary.get("selected_provider_runtime") or provenance.get("selected_provider_runtime"),
        "blocking_reasons": blocking_reasons,
        "warnings": warnings,
        "primary_action": guidance.primary_action,
        "primary_reason": guidance.primary_reason,
        "resume_action": guidance.resume_action,
        "resume_reason": guidance.resume_reason,
        "recommended_commands": guidance.recommended_commands,
    }


def _build_blocker_session_summary(session: PlanSession) -> BlockerSessionSummary:
    status_summary = _build_status_summary(session)
    guidance = build_session_guidance(session)

    evidence: dict[str, object] = {
        "required_open_gaps": int(status_summary.get("open_required_gaps", 0)),
        "optional_open_followups": int(status_summary.get("open_optional_followups", 0)),
    }
    delegated_jobs = status_summary.get("delegated_jobs", [])
    failed_jobs = [
        job for job in delegated_jobs if isinstance(job, dict) and str(job.get("status")) == "failed"
    ]
    if failed_jobs:
        failed_job = failed_jobs[0]
        evidence["failed_job"] = {
            "job_id": failed_job.get("job_id"),
            "provider": failed_job.get("provider"),
            "round_type": failed_job.get("round_type"),
            "error": failed_job.get("error"),
        }
    if session.resume.linked_execution_run_id:
        evidence["linked_execution_run_id"] = session.resume.linked_execution_run_id
    if isinstance(session.compliance, dict):
        compliance_blocking_reasons = [str(item) for item in session.compliance.get("blocking_reasons", [])]
        if compliance_blocking_reasons:
            evidence["compliance_blocking_reasons"] = compliance_blocking_reasons

    return {
        "session_id": session.id,
        "session_status": session.status,
        "block_source": guidance.block_source,
        "block_detail": guidance.block_detail,
        "primary_action": guidance.primary_action,
        "primary_reason": guidance.primary_reason,
        "resume_action": guidance.resume_action,
        "resume_reason": guidance.resume_reason,
        "blocking_reasons": [str(item) for item in status_summary.get("blocking_reasons", [])],
        "warnings": [str(item) for item in status_summary.get("warnings", [])],
        "recommended_commands": guidance.recommended_commands,
        "recovery_actions": guidance.recovery_actions,
        "evidence": evidence,
    }


def _observed_failure_provider(runtime_status: Any, session: PlanSession) -> str | None:
    provider = getattr(runtime_status, "provider", None)
    if isinstance(provider, str) and provider:
        return provider
    configured = session.structured_brief.provider_recommendation.get("reviewer")
    if isinstance(configured, str) and configured:
        return configured
    return None


def _planned_recovery_provider(session: PlanSession, runtime_status: Any) -> str | None:
    round_type = getattr(runtime_status, "kind", None)
    key = "adversarial_reviewer" if round_type == "adversarial_review" else "reviewer"
    configured = session.structured_brief.provider_recommendation.get(key)
    if isinstance(configured, str) and configured:
        provider = configured
    else:
        provider = getattr(runtime_status, "provider", None)
    return provider if isinstance(provider, str) and provider else None


def _recovery_policy_for_session(
    session: PlanSession,
    *,
    preferred_round_type: str | None = None,
    provider_mode: str = "observed",
) -> dict[str, str | None]:
    recommendation = session.structured_brief.provider_recommendation
    latest_review = _latest_round_support(session.review_rounds, "review")
    latest_adversarial = _latest_round_support(session.review_rounds, "adversarial_review")
    candidate_order: list[tuple[str, PlanReviewRound | None]] = []
    if preferred_round_type == "review":
        candidate_order = [("review", latest_review), ("adversarial_review", latest_adversarial)]
    elif preferred_round_type == "adversarial_review":
        candidate_order = [("adversarial_review", latest_adversarial), ("review", latest_review)]
    else:
        candidate_order = [("adversarial_review", latest_adversarial), ("review", latest_review)]
    for round_type, round_ in candidate_order:
        if round_ is None:
            continue
        job_id = _extract_job_id_support(round_.summary)
        runtime_status = _read_delegated_job_status_support(session, job_id) if job_id else None
        if runtime_status is not None and runtime_status.status == "failed":
            if provider_mode == "planned":
                provider = _planned_recovery_provider(session, runtime_status)
            else:
                provider = _observed_failure_provider(runtime_status, session)
            fallback = _review_fallback_fields(recommendation, round_type)
            return {
                "round_type": round_type,
                "provider": provider,
                "provider_mode": provider_mode,
                "fallback_from": fallback["fallback_from"],
                "fallback_reason": fallback["fallback_reason"],
                "fallback_detail": fallback["fallback_detail"],
            }
    fallback = _review_fallback_fields(recommendation, "review")
    reviewer = recommendation.get("reviewer")
    return {
        "round_type": None,
        "provider": str(reviewer) if isinstance(reviewer, str) else None,
        "provider_mode": None,
        "fallback_from": fallback["fallback_from"],
        "fallback_reason": fallback["fallback_reason"],
        "fallback_detail": fallback["fallback_detail"],
    }


def _review_fallback_fields(recommendation: dict[str, object], round_type: str) -> dict[str, str | None]:
    if round_type == "adversarial_review":
        return {
            "fallback_from": _string_or_none(recommendation.get("adversarial_fallback_from")),
            "fallback_reason": _string_or_none(recommendation.get("adversarial_fallback_reason")),
            "fallback_detail": _string_or_none(recommendation.get("adversarial_fallback_detail")),
        }
    return {
        "fallback_from": _string_or_none(recommendation.get("fallback_from")),
        "fallback_reason": _string_or_none(recommendation.get("fallback_reason")),
        "fallback_detail": _string_or_none(recommendation.get("fallback_detail")),
    }


def _resume_apply_action(team: TeamOrchestrator, session: PlanSession) -> PlanSession:
    guidance = build_session_guidance(session)
    action = guidance.resume_action
    if not _resume_action_is_auto_applicable(session, guidance):
        next_command = _resume_guidance_command_support(session.id, action)
        reason = guidance.resume_reason
        raise ValueError(
            f"team resume --apply cannot auto-apply resume action '{action}' "
            f"(reason: {reason}); next command: {next_command}"
        )
    if action == "approve":
        resumed = team.approve(session.id)
        resumed.structured_brief.checklist_summary = _build_checklist_summary(resumed.checklist)
        return resumed
    if action == "execute":
        resumed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)
        resumed.structured_brief.checklist_summary = _build_checklist_summary(resumed.checklist)
        return resumed
    if action == "retry_review":
        return team.retry_review(session.id)
    if action == "retry_adversarial_review":
        return team.retry_adversarial_review(session.id)
    return session


def _resume_action_is_auto_applicable(session: PlanSession, guidance: Any) -> bool:
    action = guidance.resume_action
    auto_actions = {"approve", "execute", "retry_review", "retry_adversarial_review"}
    if action not in auto_actions:
        return False
    if action == "execute":
        return session.status == "approved_for_execution"
    if action == "approve":
        return session.status == "needs_revision"
    if action in {"retry_review", "retry_adversarial_review"}:
        return guidance.block_source == "delegated_job"
    return False


def _resume_guidance_command(session_id: str, action: str) -> str:
    if action == "retry_review":
        return f"python -m agent_orchestrator.cli team retry-review {session_id}"
    if action == "retry_adversarial_review":
        return f"python -m agent_orchestrator.cli team retry-adversarial-review {session_id}"
    if action in {"revise", "revise_plan"}:
        return f"python -m agent_orchestrator.cli team revise {session_id} --summary \"close required gaps\""
    if action == "approve":
        return f"python -m agent_orchestrator.cli team approve {session_id}"
    if action == "execute":
        return f"python -m agent_orchestrator.cli team execute {session_id} --mode success_first"
    if action == "inspect_execution":
        return f"python -m agent_orchestrator.cli team inspect-execution {session_id}"
    if action == "inspect_blockers":
        return f"python -m agent_orchestrator.cli team inspect-blockers {session_id}"
    if action == "inspect_compliance":
        return f"python -m agent_orchestrator.cli team check-compliance {session_id}"
    if action == "human_decision":
        return f"python -m agent_orchestrator.cli team summary {session_id}"
    if action == "wait_for_execution":
        return f"python -m agent_orchestrator.cli team status {session_id}"
    if action == "inspect_delegated_job":
        return f"python -m agent_orchestrator.cli team inspect-blockers {session_id}"
    if action == "revise":
        return f"python -m agent_orchestrator.cli team next {session_id}"
    return f"python -m agent_orchestrator.cli team summary {session_id}"

def _review_plan(requirement: str, session: PlanSession) -> ReviewResult:
    lowered = requirement.lower()
    if "architecture direction" in lowered or "stage transition" in lowered:
        return ReviewResult(
            verdict="needs_attention",
            summary="Strategic drift requires human confirmation.",
            findings=[
                Finding(
                    severity="critical",
                    title="Human escalation required",
                    body="This requirement implies roadmap, stage, or architecture direction change.",
                    file="docs/roadmap/agent-orchestrator-master-roadmap.md",
                    line_start=1,
                    line_end=1,
                    confidence=0.95,
                    recommendation="Escalate to the human before allowing plan execution.",
                )
            ],
            next_steps=["Request human decision."],
        )
    if "auth" in lowered or "migration" in lowered or "roadmap drift" in lowered:
        return ReviewResult(
            verdict="needs_attention",
            summary="High-risk review findings block execution.",
            findings=[
                Finding(
                    severity="high",
                    title="Roadmap-sensitive high-risk change",
                    body="The plan touches a high-risk area and should not execute without a stronger decision.",
                    file="docs/process/agent-orchestrator-implementation-process.md",
                    line_start=1,
                    line_end=1,
                    confidence=0.9,
                    recommendation="Revise or escalate before execution.",
                )
            ],
            next_steps=["Revise the plan or escalate."],
        )
    if "followup" in lowered:
        return ReviewResult(
            verdict="needs_attention",
            summary="Plan is usable, but follow-up items should be tracked before or after execution.",
            findings=[
                Finding(
                    severity="medium",
                    title="Follow-up checklist needed",
                    body="The plan is acceptable but leaves non-blocking follow-up items open.",
                    file="docs/process/agent-orchestrator-implementation-process.md",
                    line_start=1,
                    line_end=1,
                    confidence=0.8,
                    recommendation="Track the follow-up in the checklist and let the lead approve explicitly.",
                )
            ],
            next_steps=["Lead approval required after acknowledging follow-up."],
        )
    return ReviewResult(verdict="approve", summary="Plan review passed.", next_steps=["Proceed to execution approval."])


def _adversarial_review_plan(requirement: str, session: PlanSession) -> ReviewResult:
    lowered = requirement.lower()
    if "adversarial challenge" in lowered:
        return ReviewResult(
            verdict="needs_attention",
            summary="Adversarial review found a non-blocking planning weakness.",
            findings=[
                Finding(
                    severity="medium",
                    title="Adversarial round requests stronger exit conditions",
                    body="The plan is plausible, but its gate conditions are not yet explicit enough for autonomous execution.",
                    file="docs/process/agent-orchestrator-implementation-process.md",
                    line_start=1,
                    line_end=1,
                    confidence=0.85,
                    recommendation="Tighten round exit conditions before approval.",
                )
            ],
            next_steps=["Revise the plan and resubmit to lead approval."],
        )
    return ReviewResult(
        verdict="approve",
        summary="Adversarial review found no additional issues.",
        next_steps=["Proceed with the lead verdict."],
    )


def _finalize_execution(session: PlanSession, run: Any) -> Literal["accepted", "needs_followup", "blocked"]:
    findings = [
        finding
        for round_ in session.review_rounds
        if round_.review_result
        for finding in round_.review_result.findings
    ]
    if any(finding.severity in {"high", "critical"} for finding in findings):
        return "blocked"
    if any(finding.severity in {"low", "medium"} for finding in findings):
        return "needs_followup"
    return "accepted" if getattr(run, "accepted", False) else "blocked"


def _summarize_plan_risks(findings: list[Finding]) -> list[str]:
    return _dedupe_preserve_order(
        f"{finding.severity}: {finding.title}"
        for finding in findings
    )


def _build_plan_gaps(review_rounds: list[PlanReviewRound]) -> list[PlanGap]:
    gaps: list[PlanGap] = []
    for round_ in review_rounds:
        if not round_.review_result:
            continue
        for finding in round_.review_result.findings:
            gaps.append(
                PlanGap(
                    title=finding.title,
                    severity=finding.severity,
                    recommendation=finding.recommendation,
                    required=finding.severity in {"high", "critical"} or "adversarial" in finding.title.lower(),
                    finding_round_id=round_.id,
                )
            )
    return gaps


def _build_approved_plan(session: PlanSession) -> dict[str, object]:
    return {
        "session_id": session.id,
        "goal": session.structured_brief.goal or session.requirement,
        "subtasks": [subtask.to_dict() for subtask in session.structured_brief.subtasks],
        "acceptance_criteria": list(session.structured_brief.acceptance_criteria),
        "review_policy": dict(session.structured_brief.review_policy),
        "open_followups": [gap.to_dict() for gap in session.gaps if gap.status != "closed"],
        "decision_verdict": session.decision_verdict.to_dict() if session.decision_verdict else None,
        "execution_contract": _build_plan_execution_contract(session),
        "gating": {
            "status": session.status,
            "gate_verdict": session.gate_verdict,
            "approved_at": session.resume.approved_at,
        },
    }


def _build_checklist_summary(checklist: list[PlanChecklistItem]) -> list[str]:
    next_item = _next_executable_checklist_item(checklist)
    return [
        (
            f"{item.label} [{item.owner}]: {'done' if item.completed else 'pending'}"
            + (f" depends_on={','.join(item.depends_on)}" if item.depends_on else "")
            + (" next_executable" if next_item is not None and item.id == next_item.id else "")
        )
        for item in checklist
    ]


def _next_executable_checklist_item(checklist: list[PlanChecklistItem]) -> PlanChecklistItem | None:
    completed_labels = {item.label for item in checklist if item.completed}
    for item in checklist:
        if not item.completed and all(label in completed_labels for label in item.depends_on):
            return item
    return None


def _set_checklist_completed(session: PlanSession, label: str, completed: bool) -> None:
    for item in session.checklist:
        if item.label == label:
            item.completed = completed
            return


def _build_plan_execution_contract(session: PlanSession) -> dict[str, object]:
    decision_verdict = session.decision_verdict.to_dict() if session.decision_verdict else {}
    provider_recommendation = dict(decision_verdict.get("selected_provider_runtime", {}))
    return ExecutionContract(
        source="approved_plan_session",
        goal=session.structured_brief.goal or session.requirement,
        acceptance_criteria=list(session.structured_brief.acceptance_criteria),
        topology={
            "selected_topology": decision_verdict.get("selected_topology"),
            "selected_mode": "success_first",
            "provider_flow": [
                provider_recommendation.get("reviewer"),
                provider_recommendation.get("author"),
                provider_recommendation.get("adversarial_reviewer"),
            ]
            if decision_verdict.get("selected_topology") == "team_with_adversarial_review"
            else [provider_recommendation.get("author")],
            "work_unit_count": len(session.structured_brief.subtasks),
        },
        provider_recommendation=provider_recommendation,
        gating={
            "contract_source": "approved_plan_session",
            "review_required": True,
        },
        review_policy=dict(session.structured_brief.review_policy),
        fallback_policy=_fallback_policy_from_provider_recommendation(provider_recommendation),
        compliance_snapshot=_execution_contract_compliance_snapshot(session.compliance),
    ).to_dict()


def _fallback_policy_from_provider_recommendation(provider_recommendation: dict[str, object]) -> dict[str, object]:
    return {
        "author": {
            "preferred": provider_recommendation.get("preferred_author") or provider_recommendation.get("author"),
            "actual": provider_recommendation.get("author"),
            "fallback_from": provider_recommendation.get("author_fallback_from"),
            "fallback_reason": provider_recommendation.get("author_fallback_reason"),
            "fallback_detail": provider_recommendation.get("author_fallback_detail"),
        },
        "reviewer": {
            "preferred": provider_recommendation.get("preferred_reviewer") or provider_recommendation.get("reviewer"),
            "actual": provider_recommendation.get("reviewer"),
            "fallback_from": provider_recommendation.get("fallback_from"),
            "fallback_reason": provider_recommendation.get("fallback_reason"),
            "fallback_detail": provider_recommendation.get("fallback_detail"),
        },
        "adversarial_reviewer": {
            "preferred": provider_recommendation.get("preferred_adversarial_reviewer")
            or provider_recommendation.get("adversarial_reviewer"),
            "actual": provider_recommendation.get("adversarial_reviewer"),
            "fallback_from": provider_recommendation.get("adversarial_fallback_from"),
            "fallback_reason": provider_recommendation.get("adversarial_fallback_reason"),
            "fallback_detail": provider_recommendation.get("adversarial_fallback_detail"),
        },
        "runtime": {
            "actual": provider_recommendation.get("runtime"),
        },
    }


def _execution_contract_compliance_snapshot(compliance: dict[str, object] | None) -> dict[str, object]:
    if not isinstance(compliance, dict):
        return {"status": "unknown", "blocking": None, "source": "session"}
    return {
        "status": compliance.get("status"),
        "blocking": compliance.get("blocking"),
        "blocking_reason_count": len(list(compliance.get("blocking_reasons", []))),
        "warning_count": len(list(compliance.get("warnings", []))),
        "source": "session",
    }


def _recommend_topology(policy: Any, requirement: str, subtasks: list[PlanSubtask]) -> dict[str, object]:
    lowered = requirement.lower()
    dependency_heavy = len(subtasks) >= 3
    high_parallelism = _requirement_or_subtasks_look_parallel(lowered, subtasks)
    high_risk = any(keyword in lowered for keyword in ("auth", "payment", "security", "migration", "database"))
    high_complexity = len(subtasks) >= 3 or any(keyword in lowered for keyword in ("architecture", "workflow", "integration", "orchestration"))

    if not policy.agent_enabled:
        recommended: TopologyName = "solo"
        reason = "policy disables agent topology, so execution should stay solo."
    elif "tiny" in lowered or len(subtasks) <= 1:
        recommended = "team"
        reason = "small scope can use the standard team topology without adversarial depth."
    elif high_parallelism and not high_risk and "migration" not in lowered and "auth" not in lowered:
        recommended = "team"
        reason = "parallelizable work can proceed with the standard team topology without deeper adversarial depth."
    elif high_risk or dependency_heavy:
        recommended = "team_with_adversarial_review"
        reason = "high-risk or dependency-heavy work benefits from team execution with adversarial review."
    else:
        recommended = "team_with_adversarial_review"
        reason = "multi-step work benefits from team execution with adversarial review."
    return {
        "recommended_topology": recommended,
        "available_topologies": ["solo", "team", "team_with_adversarial_review"],
        "selection_reason": reason,
        "subtask_count": len(subtasks),
        "agent_enabled": policy.agent_enabled,
        "signals": {
            "risk_level": "high" if high_risk else "normal",
            "parallelism": "high" if high_parallelism else "normal",
            "dependency_shape": "dependency_heavy" if dependency_heavy else "light",
            "complexity": "high" if high_complexity else "normal",
        },
    }


def _requirement_or_subtasks_look_parallel(requirement: str, subtasks: list[PlanSubtask]) -> bool:
    titles = [subtask.title.lower() for subtask in subtasks]
    gate_conditions = [" ".join(subtask.gate_conditions).lower() for subtask in subtasks]
    parallel_terms = ("parallel", "independent", "compatibility", "multiple", "modules")
    haystack = " ".join([requirement, *titles, *gate_conditions])
    return any(term in haystack for term in parallel_terms)


def _recommend_review_policy(requirement: str, topology_recommendation: dict[str, object]) -> dict[str, object]:
    lowered = requirement.lower()
    recommended_topology = str(topology_recommendation.get("recommended_topology", "team"))
    signals = dict(topology_recommendation.get("signals", {}))
    risk_level = str(signals.get("risk_level", "normal"))
    if "architecture direction" in lowered or "stage transition" in lowered:
        return {
            "policy_name": "human_escalation_required",
            "author_round": "lead",
            "review_rounds": ["review", "adversarial_review"],
            "adversarial_required": True,
            "requires_human_escalation": True,
            "selection_reason": "strategic direction changes require adversarial review and an explicit human decision.",
            "execution_config": {
                "round_sequence": ["lead", "review", "adversarial_review", "human_decision"],
                "minimum_approval": "human_decision",
                "retryable_rounds": ["review", "adversarial_review"],
            },
        }
    if recommended_topology == "team_with_adversarial_review" or risk_level == "high":
        return {
            "policy_name": "adversarial_required",
            "author_round": "lead",
            "review_rounds": ["review", "adversarial_review"],
            "adversarial_required": True,
            "requires_human_escalation": False,
            "selection_reason": "topology and risk signals require both standard and adversarial review rounds.",
            "execution_config": {
                "round_sequence": ["lead", "review", "adversarial_review"],
                "minimum_approval": "all_required_gaps_closed",
                "retryable_rounds": ["review", "adversarial_review"],
            },
        }
    return {
        "policy_name": "standard",
        "author_round": "lead",
        "review_rounds": ["review"],
        "adversarial_required": False,
        "requires_human_escalation": False,
        "selection_reason": "current topology signals support the standard review loop.",
        "execution_config": {
            "round_sequence": ["lead", "review"],
            "minimum_approval": "required_gaps_closed",
            "retryable_rounds": ["review"],
        },
    }


def _apply_team_review_policy_override(policy: dict[str, object], override: str | None) -> dict[str, object]:
    if override in {None, "", "auto"}:
        return {**policy, "override_source": "auto", "override_requested": False}
    if override == "standard":
        return {
            **policy,
            "policy_name": "standard",
            "review_rounds": ["review"],
            "adversarial_required": False,
            "requires_human_escalation": False,
            "selection_reason": "CLI override selected the standard review loop.",
            "override_source": "cli",
            "override_requested": True,
            "execution_config": {
                "round_sequence": ["lead", "review"],
                "minimum_approval": "required_gaps_closed",
                "retryable_rounds": ["review"],
            },
        }
    if override == "adversarial":
        return {
            **policy,
            "policy_name": "adversarial_required",
            "review_rounds": ["review", "adversarial_review"],
            "adversarial_required": True,
            "requires_human_escalation": False,
            "selection_reason": "CLI override selected adversarial review.",
            "override_source": "cli",
            "override_requested": True,
            "execution_config": {
                "round_sequence": ["lead", "review", "adversarial_review"],
                "minimum_approval": "all_required_gaps_closed",
                "retryable_rounds": ["review", "adversarial_review"],
            },
        }
    if override == "required-human":
        return {
            **policy,
            "policy_name": "human_escalation_required",
            "review_rounds": ["review", "adversarial_review"],
            "adversarial_required": True,
            "requires_human_escalation": True,
            "selection_reason": "CLI override requires human escalation before execution is considered approved.",
            "override_source": "cli",
            "override_requested": True,
            "execution_config": {
                "round_sequence": ["lead", "review", "adversarial_review", "human_decision"],
                "minimum_approval": "human_decision",
                "retryable_rounds": ["review", "adversarial_review"],
            },
        }
    return {**policy, "override_source": "unknown", "override_requested": True, "override_value": override}


def _recommend_provider_runtime(
    runtime: JobRuntime,
    *,
    author_provider: str = "codex",
    reviewer_provider: str = "claude",
    adversarial_reviewer_provider: str = "claude",
    worker_model: str | None = None,
    reviewer_model: str | None = None,
    adversarial_reviewer_model: str | None = None,
    author_runtime_mode: str = "cli_inherit",
    reviewer_runtime_mode: str = "direct_api",
    adversarial_reviewer_runtime_mode: str = "direct_api",
    author_fallback_from: str | None = None,
    author_fallback_reason: str | None = None,
    author_fallback_detail: str | None = None,
    reviewer_fallback_from: str | None = None,
    reviewer_fallback_reason: str | None = None,
    reviewer_fallback_detail: str | None = None,
) -> dict[str, object]:
    recommendation = {
        "author": author_provider,
        "reviewer": reviewer_provider,
        "adversarial_reviewer": adversarial_reviewer_provider,
        "author_model": worker_model,
        "reviewer_model": reviewer_model,
        "adversarial_reviewer_model": adversarial_reviewer_model,
        "author_runtime_mode": author_runtime_mode,
        "reviewer_runtime_mode": reviewer_runtime_mode,
        "adversarial_reviewer_runtime_mode": adversarial_reviewer_runtime_mode,
        "direct_api_scope": "planning/review/summarization only; no local tool loop",
        "runtime": "command" if runtime.__class__.__name__ in {"CommandJobRuntime", "RuntimeModeRouter"} else "mock",
    }
    if author_fallback_from is not None and author_fallback_from != author_provider:
        recommendation["author_fallback_from"] = author_fallback_from
        recommendation["preferred_author"] = author_fallback_from
    if author_fallback_reason is not None:
        recommendation["author_fallback_reason"] = author_fallback_reason
    if author_fallback_detail is not None:
        recommendation["author_fallback_detail"] = author_fallback_detail
    if reviewer_fallback_from is not None and reviewer_fallback_from != reviewer_provider:
        recommendation["fallback_from"] = reviewer_fallback_from
        recommendation["preferred_reviewer"] = reviewer_fallback_from
    if reviewer_fallback_reason is not None:
        recommendation["fallback_reason"] = reviewer_fallback_reason
    if reviewer_fallback_detail is not None:
        recommendation["fallback_detail"] = reviewer_fallback_detail
    return recommendation


def _summarize_review_disputes(review_rounds: list[PlanReviewRound]) -> list[str]:
    disputes: list[str] = []
    for round_ in review_rounds:
        if round_.round_type != "adversarial_review" or round_.review_result is None:
            continue
        for finding in round_.review_result.findings:
            disputes.append(f"{finding.severity}: {finding.title}")
    return disputes


def _build_decision_rationale(requirement: str, session: PlanSession, policy: Any) -> list[str]:
    rationale = [
        "Decision core keeps the approved plan as the execution entrypoint.",
        f"Selected mode preference is {policy.mode.value}.",
    ]
    topology = session.structured_brief.topology_recommendation.get("recommended_topology")
    if topology:
        rationale.append(f"Recommended topology is {topology}.")
    if session.gaps:
        rationale.append(f"Plan currently tracks {len(session.gaps)} review gap(s).")
    if "followup" in requirement.lower():
        rationale.append("Follow-up findings are tracked without blocking execution.")
    reviewer_provider = session.structured_brief.provider_recommendation.get("reviewer")
    preferred_reviewer = session.structured_brief.provider_recommendation.get("preferred_reviewer")
    fallback_detail = session.structured_brief.provider_recommendation.get("fallback_detail")
    if (
        isinstance(reviewer_provider, str)
        and isinstance(preferred_reviewer, str)
        and preferred_reviewer != reviewer_provider
    ):
        detail = f" ({fallback_detail})" if isinstance(fallback_detail, str) and fallback_detail else ""
        rationale.append(f"{preferred_reviewer} unavailable; reviewer fallback switched to {reviewer_provider}{detail}.")
    author_provider = session.structured_brief.provider_recommendation.get("author")
    preferred_author = session.structured_brief.provider_recommendation.get("preferred_author")
    author_fallback_detail = session.structured_brief.provider_recommendation.get("author_fallback_detail")
    if (
        isinstance(author_provider, str)
        and isinstance(preferred_author, str)
        and preferred_author != author_provider
    ):
        detail = f" ({author_fallback_detail})" if isinstance(author_fallback_detail, str) and author_fallback_detail else ""
        rationale.append(f"{preferred_author} unavailable; author fallback switched to {author_provider}{detail}.")
    return rationale


def _normalize_approval_status(session: PlanSession) -> ApprovalStatus:
    if session.status == "approved_for_execution":
        return "approved"
    if session.status in {"accepted", "needs_followup", "blocked"}:
        return session.status
    if session.gate_verdict in {"approved", "accepted", "needs_followup", "blocked"}:
        return "approved" if session.gate_verdict == "approved" else session.gate_verdict
    return "needs_revision"


def _build_decision_verdict(
    session: PlanSession,
    *,
    runtime: JobRuntime,
    approval_status: ApprovalStatus | None = None,
) -> DecisionVerdict:
    return DecisionVerdict(
        approval_status=approval_status or _normalize_approval_status(session),
        required_gaps=[gap.to_dict() for gap in session.gaps if gap.required and gap.status != "closed"],
        followup_gaps=[gap.to_dict() for gap in session.gaps if not gap.required and gap.status != "closed"],
        selected_topology=session.structured_brief.topology_recommendation.get("recommended_topology", "team"),
        selected_provider_runtime=session.structured_brief.provider_recommendation or _recommend_provider_runtime(runtime),
        rationale=list(
            session.structured_brief.decision_rationale
            or _build_decision_rationale(session.requirement, session, get_policy(OrchestrationMode.SUCCESS_FIRST))
        ),
    )


def build_operator_runbook(session: PlanSession) -> list[str]:
    status_summary = _build_status_summary(session)
    guidance = build_session_guidance(session)
    required_open = int(status_summary.get("open_required_gaps", 0))
    optional_open = int(status_summary.get("open_optional_followups", 0))
    delegated_jobs = status_summary.get("delegated_jobs", [])
    failed_jobs = [job for job in delegated_jobs if job.get("status") == "failed"]
    compliance_blocking_reasons = _compliance_blocking_reasons_support(session)
    compliance_warnings = _compliance_warnings_support(session)

    if guidance.block_source == "compliance":
        detail = compliance_blocking_reasons[0]
        return [
            f"Inspect the compliance blocker: {detail}.",
            f"Run `{guidance.recommended_commands[0]}` after restoring the required workflow docs.",
            "Re-run `team summary` or `team runbook` to confirm the canonical guidance is unblocked.",
        ]

    if guidance.primary_action == "inspect_compliance" and compliance_warnings:
        detail = compliance_warnings[0]
        return [
            f"Review the non-blocking compliance warning: {detail}.",
            f"Use `{guidance.recommended_commands[0]}` to inspect the full warning set before the next changed-file update.",
            "You may continue the current session, but clear the warning before touching the affected files again.",
        ]

    if guidance.block_source == "delegated_job" and failed_jobs:
        failed_job = failed_jobs[0]
        is_claude = str(failed_job.get("provider")) == "claude"
        if is_claude and guidance.block_detail == "failed_adversarial_review_job":
            return [
                "Inspect the failed delegated Claude adversarial review job.",
                f"Retry the delegated adversarial review with `{guidance.recommended_commands[0]}` if the failure was transient.",
                "Switch to `team revise` if the failure uncovered a real planning gap.",
            ]
        if is_claude:
            return [
                "Inspect the failed delegated Claude review job.",
                f"Retry the delegated review with `{guidance.recommended_commands[0]}` if the failure was transient.",
                "Switch to `team revise` if the failure uncovered a real planning gap.",
            ]
        return [
            "Inspect the failed delegated job with `status <job_id>` before taking any other action.",
            "Automatic retry is not currently supported for this delegated provider; use `team revise` or escalate manually after inspection.",
            "Re-run `team summary` after recovery so the next allowed action is explicit again.",
        ]

    if session.status == "needs_revision":
        if guidance.primary_action == "approve" and required_open == 0:
            steps = [
                f"Approve the reviewed plan with `{guidance.recommended_commands[0]}`.",
                "Optional follow-up items can remain tracked without blocking approval.",
                "After approval, run `team next` to retrieve the execution command.",
            ]
            if optional_open:
                steps.insert(1, f"Review {optional_open} optional follow-up item(s) before approval if you want to promote them.")
            return steps
        steps = [
            f"Close every required gap with `{guidance.recommended_commands[0]}`.",
            "Re-run `team summary` or `team next` to confirm approval is now allowed.",
            "Use `team approve` only after required gaps are closed.",
        ]
        if optional_open:
            steps.append("Track optional follow-up items separately; they do not block approval unless you decide to promote them.")
        return steps

    if session.status == "approved_for_execution":
        return [
            f"Run `{guidance.recommended_commands[0]}` to start execution from the approved plan.",
            "Use `team status` or `team summary` if you need to confirm the session is still in the approved phase.",
            "Inspect the linked execution run after execution starts if you need deeper provenance or result details.",
        ]

    if session.status == "executing":
        return [
            "Wait for execution to finish before taking another planning action.",
            "Use `team status` to confirm the session is still executing.",
            "Inspect the linked execution run if you need more detail than the session summary provides.",
        ]

    if session.status in {"accepted", "needs_followup"}:
        steps = [
            f"Inspect the linked execution run with `{guidance.recommended_commands[0]}` to confirm provenance, outputs, and final acceptance state.",
            "Use `team summary` to review the final planning status alongside the execution result.",
            "Avoid restarting planning from the raw requirement unless a new requirement is opened.",
        ]
        if session.status == "needs_followup" or optional_open:
            steps[1] = "Use `team summary` to review the remaining follow-up items alongside the execution result."
        return steps

    if session.status == "awaiting_human":
        return [
            "Escalate to a human decision and gather the blocking strategic question before continuing.",
            "Use `team summary` to review why human confirmation is required.",
            "Resume the workflow only after the human decision is reflected in the plan direction.",
        ]

    if session.status == "blocked":
        if guidance.block_source == "execution_run":
            if guidance.block_detail == "provenance_mismatch":
                return [
                    "Inspect the linked execution provenance before trusting the blocked session state.",
                    f"Use `{guidance.recommended_commands[0]}` and `team inspect-execution` together to resolve the run/session mismatch.",
                    "Do not resume planning or execution until the provenance mismatch is corrected.",
                ]
            return [
                "Inspect the linked execution run to identify why execution ended in a blocked state.",
                f"Use `{guidance.recommended_commands[0]}` and `team summary` together before deciding whether the plan or execution path should change.",
                "Re-run execution only after the execution-side blocker is understood and reflected in the session direction.",
            ]
        step = "Close required review blockers before trying to approve or execute again."
        if required_open:
            step = f"Close the {required_open} required gap(s) before trying to approve or execute again."
        return [
            "Inspect the blocking review findings and identify whether the issue is product, policy, or execution related.",
            step,
            "Re-run `team summary` after each fix so the next valid action is explicit.",
        ]

    return [
        "Use `team status` to inspect the current session state.",
        "Use `team next` to retrieve the next recommended command.",
        "Avoid editing stored JSON directly; continue only through the standard `team` commands.",
    ]


def _build_status_summary(session: PlanSession) -> PlanStatusSummary:
    required_open = [gap for gap in session.gaps if gap.required and gap.status != "closed"]
    optional_open = [gap for gap in session.gaps if not gap.required and gap.status != "closed"]
    blocking_reasons: list[str] = []
    compliance_blocking_reasons = _compliance_blocking_reasons_support(session)
    compliance_warnings = _compliance_warnings_support(session)
    baseline_warnings = [
        str(item) for item in session.compliance.get("baseline_warnings", [])
    ] if isinstance(session.compliance, dict) else []
    delegated_jobs, delegated_job_failed, delegated_job_provider = _collect_delegated_jobs_support(session)
    guidance = build_session_guidance(session)
    next_actions = [guidance.primary_action]
    if guidance.resume_action not in next_actions:
        next_actions.append(guidance.resume_action)

    if compliance_blocking_reasons:
        blocking_reasons.extend(compliance_blocking_reasons)
    elif delegated_job_failed:
        blocking_reasons.append("at least one delegated job failed")
    elif session.status == "needs_revision" and required_open:
        blocking_reasons.append(f"{len(required_open)} required gaps remain open")
    elif compliance_warnings:
        if baseline_warnings:
            blocking_reasons.extend(baseline_warnings)
        else:
            blocking_reasons.append(f"{len(compliance_warnings)} non-blocking compliance warning(s) remain")

    preferred_recovery_round_type = None
    recovery_provider_mode = "observed"
    if guidance.resume_action == "retry_review":
        preferred_recovery_round_type = "review"
        recovery_provider_mode = "planned"
    elif guidance.resume_action == "retry_adversarial_review":
        preferred_recovery_round_type = "adversarial_review"
        recovery_provider_mode = "planned"
    recovery_policy = _recovery_policy_for_session(
        session,
        preferred_round_type=preferred_recovery_round_type,
        provider_mode=recovery_provider_mode,
    )
    next_checklist_item = _next_executable_checklist_item(session.checklist)

    return {
        "phase": session.resume.current_phase,
        "pending_role": session.resume.pending_role,
        "open_required_gaps": len(required_open),
        "open_optional_followups": len(optional_open),
        "next_actions": next_actions,
        "next_action_message": guidance.primary_reason,
        "primary_action": guidance.primary_action,
        "primary_reason": guidance.primary_reason,
        "recommended_commands": guidance.recommended_commands,
        "next_executable_checklist_item": next_checklist_item.to_dict() if next_checklist_item else None,
        "checklist_dependencies": [item.to_dict() for item in session.checklist],
        "recovery_actions": guidance.recovery_actions,
        "recovery_round_type": recovery_policy.get("round_type"),
        "recovery_provider": recovery_policy.get("provider"),
        "recovery_provider_mode": recovery_policy.get("provider_mode"),
        "recovery_provider_fallback_from": recovery_policy.get("fallback_from"),
        "recovery_provider_fallback_reason": recovery_policy.get("fallback_reason"),
        "recovery_provider_fallback_detail": recovery_policy.get("fallback_detail"),
        "review_policy": dict(session.structured_brief.review_policy),
        "recovery_semantics": _recovery_semantics_for_guidance(guidance),
        "blocking_reasons": blocking_reasons,
        "warnings": compliance_warnings,
        "baseline_warnings": baseline_warnings,
        "block_source": guidance.block_source,
        "block_detail": guidance.block_detail,
        "resume_action": guidance.resume_action,
        "resume_reason": guidance.resume_reason,
        "delegated_jobs": delegated_jobs,
        "selected_topology": session.decision_verdict.selected_topology if session.decision_verdict else None,
        "topology_reason": session.structured_brief.topology_recommendation.get("selection_reason"),
        "decision_rationale": session.decision_verdict.rationale if session.decision_verdict else [],
        "approved_plan_ready": bool(session.approved_plan),
        "approved_plan_source": session.approved_plan.get("execution_contract", {}).get("source") if session.approved_plan else None,
    }


def _recovery_semantics_for_guidance(guidance: SessionGuidance) -> dict[str, object]:
    action = guidance.resume_action
    if action in {"retry_review", "retry_adversarial_review"}:
        category = "retry"
    elif action in {"approve", "execute"}:
        category = "resume"
    elif action == "human_decision":
        category = "escalate"
    elif guidance.block_source == "execution_run":
        category = "inspect_before_rerun"
    elif action in {"inspect_compliance", "inspect_blockers", "inspect_delegated_job", "inspect_execution"}:
        category = "inspect"
    else:
        category = "manual"
    return {
        "category": category,
        "resume_action": guidance.resume_action,
        "resume_reason": guidance.resume_reason,
        "block_source": guidance.block_source,
        "block_detail": guidance.block_detail,
        "auto_apply_allowed": action in {"approve", "execute", "retry_review", "retry_adversarial_review"},
        "human_escalation_required": action == "human_decision",
    }

def _validate_compliance_ready(session: PlanSession) -> None:
    if isinstance(session.compliance, dict) and session.compliance.get("blocking"):
        reasons = [str(item) for item in session.compliance.get("blocking_reasons", [])]
        detail = "; ".join(reasons) if reasons else "compliance checks failed"
        raise ValueError(f"team action blocked by compliance: {detail}")

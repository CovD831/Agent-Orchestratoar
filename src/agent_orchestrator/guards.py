"""Hard permission guards for agent jobs, roles, and plan artifacts."""
from __future__ import annotations

# DEPS: __future__, agent_orchestrator, typing
# RESPONSIBILITY: Enforce non-prompt safety boundaries for multi-agent collaboration.
# MODULE: decision_core
# ---

from typing import Any

from agent_orchestrator.roles import get_agent_role

READ_ONLY_JOB_KINDS = {"research", "review", "adversarial_review"}
READ_ONLY_SANDBOX = "read-only"

ROLE_ARTIFACT_PERMISSIONS: dict[str, set[str]] = {
    "lead": {"draft_plan", "lead_response", "approved_plan", "execution_contract"},
    "planner": {"draft_plan", "lead_response"},
    "reviewer": {"review_findings"},
    "adversarial_reviewer": {"review_findings"},
    "builder": set(),
    "validator": {"review_findings"},
    "rescue": {"lead_response"},
    "runtime": set(),
}

ROLE_STATE_ACTIONS: dict[str, set[str]] = {
    "lead": {"draft_plan", "submit_draft", "approve_plan", "revise_plan", "respond_to_user"},
    "planner": {"draft_plan", "respond_to_user"},
    "reviewer": {"write_review_findings"},
    "adversarial_reviewer": {"write_review_findings"},
    "builder": {"execute_work_unit"},
    "validator": {"write_review_findings"},
    "rescue": {"recover_session"},
    "runtime": {"start_job", "read_job", "cancel_job"},
}


def validate_job_request_permissions(*, kind: str, sandbox: str | None, metadata: dict[str, Any] | None = None) -> None:
    """Reject job requests that violate hard role/sandbox boundaries."""
    if kind in READ_ONLY_JOB_KINDS and sandbox not in {None, READ_ONLY_SANDBOX}:
        raise ValueError(f"{kind} jobs must use sandbox=read-only")
    role = _role_from_metadata(metadata or {}, kind)
    if role:
        validate_role_job_kind(role, kind)


def validate_runtime_start(request: Any) -> None:
    """Runtime-level guard; every runtime calls this before materializing a job."""
    validate_job_request_permissions(
        kind=str(request.kind),
        sandbox=getattr(request, "sandbox", None),
        metadata=getattr(request, "metadata", {}),
    )


def validate_role_job_kind(role_id: str, kind: str) -> None:
    role = get_agent_role(_canonical_role(role_id))
    if kind not in role.allowed_job_kinds:
        raise ValueError(f"role {role.id} cannot create {kind} jobs")


def validate_role_state_action(role_id: str, action: str) -> None:
    role = _canonical_role(role_id)
    if action not in ROLE_STATE_ACTIONS.get(role, set()):
        raise ValueError(f"role {role} cannot perform state action {action}")


def validate_artifact_write(role_id: str, artifact_kind: str) -> None:
    role = _canonical_role(role_id)
    if artifact_kind not in ROLE_ARTIFACT_PERMISSIONS.get(role, set()):
        raise ValueError(f"role {role} cannot write {artifact_kind} artifacts")


def validate_execution_gate(*, status: str, gate_verdict: str | None) -> None:
    if status != "approved_for_execution" or gate_verdict != "approved":
        raise ValueError("execution requires an explicitly approved plan")


def _role_from_metadata(metadata: dict[str, Any], kind: str) -> str | None:
    role = metadata.get("role") or metadata.get("agent_role")
    if isinstance(role, str) and role:
        return role
    if kind == "review":
        return "reviewer"
    if kind == "adversarial_review":
        return "adversarial_reviewer"
    if kind == "implementation":
        return "builder"
    if kind == "rescue":
        return "rescue"
    return None


def _canonical_role(role_id: str) -> str:
    aliases = {
        "review": "reviewer",
        "plan_reviewer": "reviewer",
        "adversarial_review": "adversarial_reviewer",
        "build": "builder",
        "worker": "builder",
        "execution_reviewer": "validator",
    }
    return aliases.get(role_id, role_id)

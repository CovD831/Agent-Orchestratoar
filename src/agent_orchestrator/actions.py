"""Backend action registry for plan session operations."""
from __future__ import annotations

# DEPS: __future__, dataclasses, typing
# RESPONSIBILITY: Expose structured, state-aware actions for UI and API clients.
# MODULE: decision_core
# ---

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class TeamAction:
    id: str
    label: str
    description: str
    enabled: bool
    reason: str
    recommended_command: str | None = None
    requires_input: bool = False
    risk_level: str = "low"
    confirmation_required: bool = False
    input_schema: dict[str, object] | None = None
    state_changes: list[str] | None = None
    recovery_suggestions: list[str] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "enabled": self.enabled,
            "reason": self.reason,
            "recommended_command": self.recommended_command,
            "requires_input": self.requires_input,
            "risk_level": self.risk_level,
            "confirmation_required": self.confirmation_required,
            "input_schema": self.input_schema or {},
            "state_changes": self.state_changes or [],
            "recovery_suggestions": self.recovery_suggestions or [],
        }


ACTION_LABELS = {
    "lead_chat": ("继续沟通", "继续和计划主控澄清需求"),
    "mark_draft_ready": ("确认初稿", "确认第一版计划可以进入审查"),
    "submit_review": ("提交审查", "让主控和对抗审核开始质疑补全"),
    "revise": ("修订计划", "关闭缺口并提交修订说明"),
    "approve": ("批准执行", "批准已完成修订的计划"),
    "execute": ("开始执行", "从已批准计划启动执行 run"),
    "retry_review": ("重试审核", "重试失败的普通审核任务"),
    "retry_adversarial_review": ("重试对抗审核", "重试失败的对抗审核任务"),
    "resume": ("继续推进", "根据后端推荐动作恢复会话"),
    "inspect_execution": ("查看执行", "查看已关联的执行 run"),
    "inspect_delegated_job": ("查看委派任务", "查看阻塞或运行中的委派 job"),
}


def build_session_actions(session_payload: dict[str, object]) -> list[dict[str, object]]:
    status = str(session_payload.get("status") or "unknown")
    summary = session_payload.get("status_summary", {}) if isinstance(session_payload.get("status_summary"), dict) else {}
    primary_action = str(summary.get("primary_action") or "")
    recovery_actions = [str(item) for item in summary.get("recovery_actions", [])] if isinstance(summary.get("recovery_actions"), list) else []
    recommended_commands = [str(item) for item in summary.get("recommended_commands", [])] if isinstance(summary.get("recommended_commands"), list) else []
    command_by_action = _command_map(recommended_commands)
    allowed = _allowed_actions_for_status(status, primary_action, recovery_actions)

    actions = []
    for action_id in ACTION_LABELS:
        label, description = ACTION_LABELS[action_id]
        enabled = action_id in allowed
        actions.append(
            TeamAction(
                id=action_id,
                label=label,
                description=description,
                enabled=enabled,
                reason=_action_reason(action_id, enabled, status, summary),
                recommended_command=command_by_action.get(action_id),
                requires_input=action_id == "revise",
                risk_level=_risk_level(action_id),
                confirmation_required=action_id in {"execute", "cancel", "approve"},
                input_schema=_input_schema(action_id),
                state_changes=_state_changes(action_id),
                recovery_suggestions=_recovery_suggestions(action_id, summary),
            ).to_dict()
        )
    return actions


def primary_action_from_registry(session_payload: dict[str, object]) -> dict[str, object]:
    summary = session_payload.get("status_summary", {}) if isinstance(session_payload.get("status_summary"), dict) else {}
    primary = str(summary.get("primary_action") or "inspect_session")
    actions = build_session_actions(session_payload)
    enabled_actions = [action for action in actions if action.get("enabled")]
    selected = next((action for action in enabled_actions if action.get("id") == primary), None)
    if selected is None and enabled_actions:
        selected = enabled_actions[0]
    return {
        "primary_action": selected.get("id") if selected else primary,
        "primary_label": selected.get("label") if selected else ACTION_LABELS.get(primary, (primary, ""))[0],
        "primary_reason": summary.get("primary_reason") or summary.get("next_action_message", ""),
        "recommended_commands": [str(item) for item in summary.get("recommended_commands", [])] if isinstance(summary.get("recommended_commands"), list) else [],
        "recovery_actions": [str(item) for item in summary.get("recovery_actions", [])] if isinstance(summary.get("recovery_actions"), list) else [],
    }


def assert_session_action_allowed(session_payload: dict[str, object], action_id: str, payload: dict[str, object] | None = None) -> None:
    actions = build_session_actions(session_payload)
    action = next((item for item in actions if item.get("id") == action_id), None)
    if not action:
        raise ValueError(f"unknown session action: {action_id}")
    if not action.get("enabled"):
        raise ValueError(str(action.get("reason") or f"session action '{action_id}' is not available"))
    _validate_action_payload(action_id, payload or {})


def _allowed_actions_for_status(status: str, primary_action: str, recovery_actions: list[str]) -> set[str]:
    allowed: set[str] = set()
    if status == "needs_revision":
        allowed.add("revise")
    if status == "intake_chat":
        allowed.update({"lead_chat", "mark_draft_ready"})
    if status == "draft_ready":
        allowed.update({"lead_chat", "submit_review"})
    if status == "awaiting_human_confirmation":
        allowed.update({"lead_chat"})
        if primary_action == "approve":
            allowed.add("approve")
        else:
            allowed.add("revise")
    if status == "approved_for_execution":
        allowed.add("execute")
    if status in {"accepted", "needs_followup"}:
        allowed.add("inspect_execution")
    if status == "executing":
        allowed.update({"resume", "inspect_execution"})
    if status in {"blocked", "awaiting_human"}:
        allowed.add("resume")

    mapped_primary = _normalize_action(primary_action)
    if mapped_primary:
        allowed.add(mapped_primary)
    for recovery_action in recovery_actions:
        mapped = _normalize_action(recovery_action)
        if mapped:
            allowed.add(mapped)
    return allowed


def _normalize_action(action: str) -> str | None:
    normalized = action.replace("-", "_")
    if normalized in ACTION_LABELS:
        return normalized
    if normalized == "retry_adversarial":
        return "retry_adversarial_review"
    return None


def _command_map(commands: list[str]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for command in commands:
        if "retry-adversarial-review" in command:
            mapped["retry_adversarial_review"] = command
        elif "retry-review" in command:
            mapped["retry_review"] = command
        elif " draft-ready " in f" {command} ":
            mapped["mark_draft_ready"] = command
        elif " submit-review " in f" {command} ":
            mapped["submit_review"] = command
        elif " chat " in f" {command} ":
            mapped["lead_chat"] = command
        elif " inspect-execution " in f" {command} ":
            mapped["inspect_execution"] = command
        elif " execute " in f" {command} ":
            mapped["execute"] = command
        elif " resume " in f" {command} ":
            mapped["resume"] = command
    return mapped


def _action_reason(action_id: str, enabled: bool, status: str, summary: dict[str, Any]) -> str:
    if enabled:
        return str(summary.get("primary_reason") or summary.get("next_action_message") or "当前状态允许执行该动作")
    return f"当前状态 {status} 不允许执行 {ACTION_LABELS[action_id][0]}"


def _risk_level(action_id: str) -> str:
    if action_id in {"execute", "approve", "submit_review"}:
        return "medium"
    if action_id in {"retry_review", "retry_adversarial_review", "resume"}:
        return "low"
    return "low"


def _input_schema(action_id: str) -> dict[str, object]:
    if action_id == "revise":
        return {
            "required": ["summary", "closed_gap_ids"],
            "properties": {
                "summary": {"type": "string"},
                "closed_gap_ids": {"type": "array", "items": {"type": "string"}},
            },
        }
    if action_id == "execute":
        return {"required": [], "properties": {"mode": {"type": "string"}}}
    if action_id == "lead_chat":
        return {"required": ["message"], "properties": {"message": {"type": "string"}}}
    return {"required": [], "properties": {}}


def _state_changes(action_id: str) -> list[str]:
    changes = {
        "revise": ["append revision round", "close selected gaps", "refresh compliance"],
        "lead_chat": ["append human message", "append lead response"],
        "mark_draft_ready": ["set status draft_ready"],
        "submit_review": ["run review rounds", "set status awaiting_human_confirmation"],
        "approve": ["set status approved_for_execution", "build approved plan"],
        "execute": ["set status executing", "create linked run", "finalize execution verdict"],
        "retry_review": ["append review_retry round", "recompute gaps"],
        "retry_adversarial_review": ["append adversarial_review_retry round", "recompute gaps"],
        "resume": ["normalize session resume state"],
        "inspect_execution": ["read linked execution run"],
        "inspect_delegated_job": ["read delegated job status"],
    }
    return changes.get(action_id, [])


def _recovery_suggestions(action_id: str, summary: dict[str, Any]) -> list[str]:
    recovery = [str(item) for item in summary.get("recovery_actions", [])] if isinstance(summary.get("recovery_actions"), list) else []
    if recovery:
        return recovery
    if action_id == "execute":
        return ["inspect_execution", "resume"]
    if action_id.startswith("retry"):
        return ["inspect_delegated_job", "revise"]
    return []


def _validate_action_payload(action_id: str, payload: dict[str, object]) -> None:
    if action_id == "revise":
        if not str(payload.get("summary", "")).strip():
            raise ValueError("revise action requires summary")
        closed_gap_ids = payload.get("closed_gap_ids")
        if not isinstance(closed_gap_ids, list) or not closed_gap_ids:
            raise ValueError("revise action requires closed_gap_ids")
    if action_id == "execute" and payload.get("mode") is not None and not isinstance(payload.get("mode"), str):
        raise ValueError("execute action mode must be a string")
    if action_id == "lead_chat" and not str(payload.get("message", "")).strip():
        raise ValueError("lead_chat action requires message")

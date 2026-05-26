"""Agent role registry for team-oriented orchestration views."""
from __future__ import annotations

# DEPS: __future__, dataclasses, typing
# RESPONSIBILITY: Define stable agent role metadata used by backend work graphs and dashboard grouping.
# MODULE: decision_core
# ---

from dataclasses import dataclass, field
from typing import Literal

AgentLayer = Literal["decision", "execution", "review", "rescue", "runtime"]


@dataclass(frozen=True, slots=True)
class AgentRole:
    id: str
    label: str
    layer: AgentLayer
    responsibilities: list[str]
    default_provider: str
    allowed_job_kinds: list[str] = field(default_factory=list)

    @property
    def layer_label(self) -> str:
        return LAYER_LABELS[self.layer]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "layer": self.layer,
            "layer_label": self.layer_label,
            "responsibilities": list(self.responsibilities),
            "default_provider": self.default_provider,
            "allowed_job_kinds": list(self.allowed_job_kinds),
        }


@dataclass(frozen=True, slots=True)
class RoleContract:
    role: str
    runtime_mode: str
    allowed_actions: list[str]
    forbidden_actions: list[str]
    required_outputs: list[str]
    command_refs: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "runtime_mode": self.runtime_mode,
            "allowed_actions": list(self.allowed_actions),
            "forbidden_actions": list(self.forbidden_actions),
            "required_outputs": list(self.required_outputs),
            "command_refs": list(self.command_refs),
        }


LAYER_LABELS: dict[AgentLayer, str] = {
    "decision": "决策层",
    "execution": "执行层",
    "review": "审核层",
    "rescue": "救援层",
    "runtime": "运行时层",
}


DEFAULT_AGENT_ROLES: dict[str, AgentRole] = {
    "lead": AgentRole(
        id="lead",
        label="主控 Lead",
        layer="decision",
        responsibilities=["协调计划、门禁、下一步动作"],
        default_provider="decision_core",
        allowed_job_kinds=["research"],
    ),
    "planner": AgentRole(
        id="planner",
        label="规划 Planner",
        layer="decision",
        responsibilities=["拆解目标、维护计划结构"],
        default_provider="claude",
        allowed_job_kinds=["research"],
    ),
    "proponent": AgentRole(
        id="proponent",
        label="正方 Proponent",
        layer="decision",
        responsibilities=["提出可执行方案和价值主张"],
        default_provider="codex",
        allowed_job_kinds=["research"],
    ),
    "skeptic": AgentRole(
        id="skeptic",
        label="反方 Skeptic",
        layer="decision",
        responsibilities=["挑战假设、风险和遗漏"],
        default_provider="claude",
        allowed_job_kinds=["review"],
    ),
    "builder": AgentRole(
        id="builder",
        label="执行 Builder",
        layer="execution",
        responsibilities=["执行工作单元、产出实现结果"],
        default_provider="codex",
        allowed_job_kinds=["implementation"],
    ),
    "reviewer": AgentRole(
        id="reviewer",
        label="审核 Reviewer",
        layer="review",
        responsibilities=["检查计划和执行结果"],
        default_provider="claude",
        allowed_job_kinds=["review"],
    ),
    "adversarial_reviewer": AgentRole(
        id="adversarial_reviewer",
        label="对抗审核",
        layer="review",
        responsibilities=["从反方视角寻找风险和遗漏"],
        default_provider="claude",
        allowed_job_kinds=["adversarial_review"],
    ),
    "validator": AgentRole(
        id="validator",
        label="验证 Validator",
        layer="execution",
        responsibilities=["验证验收条件和运行结果"],
        default_provider="codex",
        allowed_job_kinds=["review"],
    ),
    "rescue": AgentRole(
        id="rescue",
        label="救援 Rescue",
        layer="rescue",
        responsibilities=["处理失败、恢复和重试路径"],
        default_provider="claude",
        allowed_job_kinds=["rescue"],
    ),
    "runtime": AgentRole(
        id="runtime",
        label="运行时",
        layer="runtime",
        responsibilities=["承载 provider、job、terminal 执行"],
        default_provider="mock",
        allowed_job_kinds=["research", "implementation", "review", "adversarial_review", "rescue"],
    ),
}


def get_agent_role(role_id: str) -> AgentRole:
    return DEFAULT_AGENT_ROLES.get(role_id, DEFAULT_AGENT_ROLES["planner"])


def role_for_job_kind(kind: str) -> AgentRole:
    normalized = kind.replace("-", "_")
    if normalized == "implementation":
        return DEFAULT_AGENT_ROLES["builder"]
    if normalized in {"review", "review_retry"}:
        return DEFAULT_AGENT_ROLES["reviewer"]
    if normalized in {"adversarial_review", "adversarial_review_retry"}:
        return DEFAULT_AGENT_ROLES["adversarial_reviewer"]
    if normalized == "rescue":
        return DEFAULT_AGENT_ROLES["rescue"]
    if normalized == "runtime":
        return DEFAULT_AGENT_ROLES["runtime"]
    return DEFAULT_AGENT_ROLES["planner"]


def role_for_work_unit_kind(kind: str) -> AgentRole:
    normalized = kind.replace("-", "_")
    if normalized == "session":
        return DEFAULT_AGENT_ROLES["lead"]
    if normalized == "subtask":
        return DEFAULT_AGENT_ROLES["builder"]
    if normalized in {"review_round", "review"}:
        return DEFAULT_AGENT_ROLES["reviewer"]
    if normalized == "adversarial_review":
        return DEFAULT_AGENT_ROLES["adversarial_reviewer"]
    if normalized == "gap":
        return DEFAULT_AGENT_ROLES["lead"]
    if normalized == "execution_run":
        return DEFAULT_AGENT_ROLES["runtime"]
    return DEFAULT_AGENT_ROLES["planner"]


ROLE_CONTRACTS: dict[str, RoleContract] = {
    "planner": RoleContract(
        role="planner",
        runtime_mode="direct_api",
        allowed_actions=["draft_plan", "respond_to_user"],
        forbidden_actions=["execute_work_unit", "write_review_findings"],
        required_outputs=["structured_brief", "checklist", "next_executable_task"],
        command_refs=["team start", "team chat", "team draft-ready", "team task next"],
    ),
    "reviewer": RoleContract(
        role="reviewer",
        runtime_mode="direct_api",
        allowed_actions=["write_review_findings"],
        forbidden_actions=["execute_work_unit", "approve_plan"],
        required_outputs=["review_findings", "required_gaps", "followup_gaps"],
        command_refs=["team submit-review", "team retry-review", "team task list"],
    ),
    "adversarial_reviewer": RoleContract(
        role="adversarial_reviewer",
        runtime_mode="direct_api",
        allowed_actions=["write_review_findings"],
        forbidden_actions=["execute_work_unit", "approve_plan"],
        required_outputs=["adversarial_findings", "risk_challenges", "gap_recommendations"],
        command_refs=["team submit-review", "team retry-adversarial-review"],
    ),
    "builder": RoleContract(
        role="builder",
        runtime_mode="cli_inherit",
        allowed_actions=["execute_work_unit"],
        forbidden_actions=["approve_plan", "write_review_findings"],
        required_outputs=["implementation_result", "targeted_validation", "changed_files"],
        command_refs=["team execute", "team inspect-execution"],
    ),
    "rescue": RoleContract(
        role="rescue",
        runtime_mode="cli_inherit",
        allowed_actions=["recover_session"],
        forbidden_actions=["approve_plan", "write_review_findings"],
        required_outputs=["rescue_summary", "retry_plan", "stop_reason"],
        command_refs=["team inspect-blockers", "team retry-review", "team retry-adversarial-review"],
    ),
}


def role_contracts() -> list[RoleContract]:
    return [ROLE_CONTRACTS[key] for key in sorted(ROLE_CONTRACTS)]

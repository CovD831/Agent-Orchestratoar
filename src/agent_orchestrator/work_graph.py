"""Persistent work graph models for plan sessions."""
from __future__ import annotations

# DEPS: __future__, agent_orchestrator, dataclasses, json, pathlib, tempfile, typing
# RESPONSIBILITY: Persist backend work-unit graphs derived from plan sessions.
# MODULE: decision_core
# ---

import json
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from agent_orchestrator.jobs import now_iso
from agent_orchestrator.roles import get_agent_role, role_for_work_unit_kind


@dataclass(slots=True)
class WorkUnitNode:
    id: str
    kind: str
    title: str
    status: str
    owner_role: str
    depends_on: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    linked_job_ids: list[str] = field(default_factory=list)
    linked_run_id: str | None = None
    summary: str = ""
    assigned_role: str | None = None
    allowed_actions: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    message_ids: list[str] = field(default_factory=list)
    job_ids: list[str] = field(default_factory=list)
    next_action: str = "inspect"
    validation: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "owner_role": self.owner_role,
            "depends_on": list(self.depends_on),
            "acceptance_criteria": list(self.acceptance_criteria),
            "linked_job_ids": list(self.linked_job_ids),
            "linked_run_id": self.linked_run_id,
            "summary": self.summary,
            "assigned_role": self.assigned_role or self.owner_role,
            "allowed_actions": list(self.allowed_actions),
            "blocked_by": list(self.blocked_by),
            "message_ids": list(self.message_ids),
            "job_ids": list(self.job_ids),
            "next_action": self.next_action,
            "validation": list(self.validation),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "WorkUnitNode":
        return cls(
            id=str(data.get("id", "")),
            kind=str(data.get("kind", "unknown")),
            title=str(data.get("title", "")),
            status=str(data.get("status", "planned")),
            owner_role=str(data.get("owner_role") or role_for_work_unit_kind(str(data.get("kind", ""))).id),
            depends_on=[str(item) for item in data.get("depends_on", [])],
            acceptance_criteria=[str(item) for item in data.get("acceptance_criteria", [])],
            linked_job_ids=[str(item) for item in data.get("linked_job_ids", [])],
            linked_run_id=data.get("linked_run_id") if isinstance(data.get("linked_run_id"), str) else None,
            summary=str(data.get("summary", "")),
            assigned_role=data.get("assigned_role") if isinstance(data.get("assigned_role"), str) else None,
            allowed_actions=[str(item) for item in data.get("allowed_actions", [])],
            blocked_by=[str(item) for item in data.get("blocked_by", [])],
            message_ids=[str(item) for item in data.get("message_ids", [])],
            job_ids=[str(item) for item in data.get("job_ids", [])],
            next_action=str(data.get("next_action") or "inspect"),
            validation=[str(item) for item in data.get("validation", [])],
        )


@dataclass(slots=True)
class WorkUnitGraph:
    session_id: str
    root_id: str
    nodes: list[WorkUnitNode]
    edges: list[dict[str, str]]
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "root_id": self.root_id,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [dict(edge) for edge in self.edges],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "WorkUnitGraph":
        nodes = [
            WorkUnitNode.from_dict(item)
            for item in data.get("nodes", [])
            if isinstance(item, dict) and item.get("id")
        ]
        return cls(
            session_id=str(data.get("session_id", "")),
            root_id=str(data.get("root_id") or (nodes[0].id if nodes else "")),
            nodes=nodes,
            edges=[
                {"from": str(edge.get("from", "")), "to": str(edge.get("to", ""))}
                for edge in data.get("edges", [])
                if isinstance(edge, dict)
            ],
            created_at=str(data.get("created_at") or now_iso()),
            updated_at=str(data.get("updated_at") or now_iso()),
        )


@dataclass(slots=True)
class WorkGraphStore:
    root: Path | str = ".agent_orchestrator/plans"

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    def path_for(self, session_id: str) -> Path:
        return self.root / session_id / "work_graph.json"

    def write(self, graph: WorkUnitGraph) -> None:
        path = self.path_for(graph.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(graph.to_dict(), ensure_ascii=False, indent=2)
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)
        tmp_path.replace(path)

    def read(self, session_id: str) -> WorkUnitGraph:
        payload = json.loads(self.path_for(session_id).read_text(encoding="utf-8"))
        return WorkUnitGraph.from_dict(payload)

    def read_optional(self, session_id: str) -> WorkUnitGraph | None:
        path = self.path_for(session_id)
        if not path.exists():
            return None
        try:
            return WorkUnitGraph.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return None


def build_initial_work_graph(session: Any, existing: WorkUnitGraph | None = None, message_store: Any | None = None) -> WorkUnitGraph:
    session_id = str(session.id)
    root_id = session_id
    created_at = existing.created_at if existing else now_iso()
    nodes: list[WorkUnitNode] = [
        WorkUnitNode(
            id=root_id,
            kind="session",
            title=_session_title(session),
            status=str(session.status),
            owner_role="lead",
            assigned_role="lead",
            acceptance_criteria=[str(item) for item in getattr(session.structured_brief, "acceptance_criteria", [])],
            allowed_actions=_node_actions_for_status(str(session.status), "session"),
            next_action=_node_next_action(str(session.status), "session"),
            summary=str(getattr(session, "requirement", "")),
        )
    ]
    edges: list[dict[str, str]] = []

    for subtask in getattr(session, "subtasks", []):
        node = WorkUnitNode(
            id=str(subtask.id),
            kind="subtask",
            title=str(subtask.title),
            status=_subtask_status(session),
            owner_role="builder",
            assigned_role="builder",
            depends_on=[root_id],
            acceptance_criteria=[str(item) for item in subtask.gate_conditions],
            allowed_actions=_node_actions_for_status(_subtask_status(session), "subtask"),
            next_action=_node_next_action(_subtask_status(session), "subtask"),
            validation=[str(item) for item in subtask.gate_conditions],
            summary=" / ".join(str(item) for item in subtask.expected_outputs),
        )
        nodes.append(node)
        edges.append({"from": root_id, "to": node.id})

    for gap in getattr(session, "gaps", []):
        owner_role = "lead"
        node = WorkUnitNode(
            id=str(gap.id),
            kind="gap",
            title=str(gap.title),
            status=str(gap.status),
            owner_role=owner_role,
            assigned_role=owner_role,
            depends_on=[root_id],
            acceptance_criteria=[str(gap.recommendation)] if gap.recommendation else [],
            allowed_actions=_node_actions_for_status(str(gap.status), "gap"),
            blocked_by=[str(gap.id)] if gap.required and gap.status != "closed" else [],
            next_action=_node_next_action(str(gap.status), "gap"),
            validation=[str(gap.recommendation)] if gap.recommendation else [],
            summary=str(gap.recommendation),
        )
        nodes.append(node)
        edges.append({"from": root_id, "to": node.id})

    for round_ in getattr(session, "review_rounds", []):
        round_type = str(round_.round_type)
        job_id = _extract_job_id(str(round_.summary))
        message_ids = _message_ids_for_round(message_store, session.id, str(round_.id))
        owner_role = "adversarial_reviewer" if "adversarial" in round_type else "reviewer" if "review" in round_type else "lead"
        node = WorkUnitNode(
            id=str(round_.id),
            kind="adversarial_review" if "adversarial" in round_type else "review_round",
            title=_round_title(round_type),
            status=round_type,
            owner_role=owner_role,
            assigned_role=owner_role,
            depends_on=[root_id],
            linked_job_ids=[job_id] if job_id else [],
            job_ids=[job_id] if job_id else [],
            message_ids=message_ids,
            allowed_actions=_node_actions_for_status(round_type, "review_round"),
            next_action=_node_next_action(round_type, "review_round"),
            summary=str(round_.summary),
        )
        nodes.append(node)
        edges.append({"from": root_id, "to": node.id})

    linked_run_id = getattr(getattr(session, "resume", None), "linked_execution_run_id", None)
    if linked_run_id:
        node = WorkUnitNode(
            id=str(linked_run_id),
            kind="execution_run",
            title="执行运行",
            status=str(session.status),
            owner_role="runtime",
            assigned_role="runtime",
            depends_on=[root_id],
            linked_run_id=str(linked_run_id),
            allowed_actions=_node_actions_for_status(str(session.status), "execution_run"),
            next_action=_node_next_action(str(session.status), "execution_run"),
            summary="Linked execution run from approved plan.",
        )
        nodes.append(node)
        edges.append({"from": root_id, "to": node.id})

    return WorkUnitGraph(
        session_id=session_id,
        root_id=root_id,
        nodes=nodes,
        edges=edges,
        created_at=created_at,
        updated_at=now_iso(),
    )


def graph_to_plan_tree(graph: WorkUnitGraph) -> dict[str, object]:
    nodes = {node.id: node for node in graph.nodes}
    children_by_parent: dict[str, list[str]] = {}
    for edge in graph.edges:
        parent = edge.get("from", "")
        child = edge.get("to", "")
        if parent and child:
            children_by_parent.setdefault(parent, []).append(child)

    def build_node(node_id: str) -> dict[str, object]:
        node = nodes[node_id]
        role = get_agent_role(node.owner_role)
        related_agent_ids = [*node.linked_job_ids]
        if node.linked_run_id:
            related_agent_ids.append(node.linked_run_id)
        return {
            "id": node.id,
            "label": node.title,
            "kind": node.kind,
            "status": node.status,
            "state": _node_state(node.status),
            "summary": node.summary,
            "owner_role": role.id,
            "owner_role_label": role.label,
            "assigned_role": node.assigned_role or node.owner_role,
            "allowed_actions": list(node.allowed_actions),
            "blocked_by": list(node.blocked_by),
            "message_ids": list(node.message_ids),
            "job_ids": list(node.job_ids),
            "next_action": node.next_action,
            "validation": list(node.validation),
            "schedulable": _node_schedulable(node, nodes),
            "related_agent_ids": related_agent_ids,
            "children": [build_node(child_id) for child_id in children_by_parent.get(node_id, []) if child_id in nodes],
        }

    if graph.root_id not in nodes:
        return {
            "id": graph.session_id,
            "label": "当前计划",
            "kind": "session",
            "status": "unknown",
            "state": "planned",
            "summary": "",
            "related_agent_ids": [],
            "children": [],
        }
    return build_node(graph.root_id)


def _session_title(session: Any) -> str:
    brief = getattr(session, "structured_brief", None)
    goal = getattr(brief, "goal", "")
    return str(goal or getattr(session, "requirement", "") or "当前计划")


def _subtask_status(session: Any) -> str:
    status = str(getattr(session, "status", "planned"))
    if status in {"accepted", "needs_followup"}:
        return "completed"
    if status == "executing":
        return "running"
    if status == "blocked":
        return "blocked"
    if status == "approved_for_execution":
        return "approved_for_execution"
    return "planned"


def _round_title(round_type: str) -> str:
    labels = {
        "authoring": "计划起草",
        "review": "计划审核",
        "review_retry": "审核重试",
        "adversarial_review": "对抗审核",
        "adversarial_review_retry": "对抗审核重试",
        "revision": "计划修订",
        "approval": "批准门禁",
    }
    return labels.get(round_type, round_type.replace("_", " "))


def _extract_job_id(summary: str) -> str | None:
    for token in reversed(summary.replace(".", " ").split()):
        if token.startswith("job-"):
            return token
    return None


def _node_state(status: str) -> str:
    if status in {"accepted", "completed", "approved_for_execution", "closed", "approval"}:
        return "done"
    if status in {"blocked", "failed", "needs_revision", "open"}:
        return "blocked"
    if status in {"needs_followup"}:
        return "followup"
    if status in {"executing", "running", "working", "in_review"}:
        return "running"
    return "planned"


def schedulable_nodes(graph: WorkUnitGraph) -> list[dict[str, object]]:
    nodes = {node.id: node for node in graph.nodes}
    return [node.to_dict() for node in graph.nodes if _node_schedulable(node, nodes)]


def next_executable_node(graph: WorkUnitGraph) -> dict[str, object] | None:
    schedulable = schedulable_nodes(graph)
    for node in schedulable:
        if node.get("kind") != "session":
            return node
    return schedulable[0] if schedulable else None


def node_actions(node: WorkUnitNode) -> list[str]:
    return list(node.allowed_actions) or _node_actions_for_status(node.status, node.kind)


def _node_schedulable(node: WorkUnitNode, nodes: dict[str, WorkUnitNode]) -> bool:
    if node.blocked_by:
        return False
    if node.status in {"completed", "accepted", "closed", "approval"}:
        return False
    for dependency_id in node.depends_on:
        dependency = nodes.get(dependency_id)
        if dependency and dependency.status in {"blocked", "failed", "needs_revision", "open"}:
            return False
    return True


def _node_actions_for_status(status: str, kind: str) -> list[str]:
    if kind == "gap" and status != "closed":
        return ["resolve"]
    if kind == "execution_run":
        return ["inspect_execution"] if status in {"accepted", "needs_followup", "blocked"} else ["wait"]
    if status in {"failed", "blocked", "needs_revision"}:
        return ["inspect", "retry"]
    if status in {"approved_for_execution"}:
        return ["execute"]
    if status in {"running", "executing", "in_review"}:
        return ["inspect", "wait"]
    return ["inspect"]


def _node_next_action(status: str, kind: str) -> str:
    actions = _node_actions_for_status(status, kind)
    return actions[0] if actions else "inspect"


def _message_ids_for_round(message_store: Any | None, session_id: str, round_id: str) -> list[str]:
    if message_store is None:
        return []
    try:
        messages = message_store.list_for_session(session_id, limit=200)
    except Exception:
        return []
    return [
        str(message.get("id"))
        for message in messages
        if isinstance(message.get("payload"), dict) and message["payload"].get("review_round_id") == round_id
    ]

"""Service helpers for the local Agent Team Console."""
from __future__ import annotations

# DEPS: __future__, agent_orchestrator, json, pathlib, typing
# RESPONSIBILITY: Build structured dashboard payloads from persisted plan, run, and job stores.
# MODULE: interface
# ---

import json
from pathlib import Path
from typing import Any

from agent_orchestrator.actions import assert_session_action_allowed, build_session_actions, primary_action_from_registry
from agent_orchestrator.agent_config import AgentConfig, AgentConfigStore
from agent_orchestrator.command import ProviderHealthCheck
from agent_orchestrator.events import EventStore
from agent_orchestrator.jobs import FileJobRuntime
from agent_orchestrator.memory import MemoryStore
from agent_orchestrator.messages import MessageStore
from agent_orchestrator.planning import PlanStore, TeamOrchestrator, build_operator_runbook
from agent_orchestrator.policies import OrchestrationMode
from agent_orchestrator.roles import DEFAULT_AGENT_ROLES, LAYER_LABELS, get_agent_role, role_for_job_kind
from agent_orchestrator.run_store import RunStore
from agent_orchestrator.tmux_runtime import TmuxJobRuntime
from agent_orchestrator.work_graph import WorkGraphStore, WorkUnitGraph, graph_to_plan_tree, schedulable_nodes


TIMELINE_STEPS = [
    ("intake_chat", "沟通"),
    ("draft_ready", "初稿"),
    ("adversarial_review", "审查"),
    ("awaiting_human_confirmation", "确认"),
    ("needs_revision", "修订"),
    ("approved_for_execution", "已批准"),
    ("executing", "执行"),
    ("accepted", "验收"),
]

ROLE_GROUPS = [
    ("decision", "决策层"),
    ("execution", "执行层"),
    ("review", "审核层"),
    ("rescue", "救援层"),
    ("runtime", "运行时层"),
]


class DashboardService:
    def __init__(
        self,
        *,
        team: TeamOrchestrator,
        plans_root: Path | str = ".agent_orchestrator/plans",
        runs_root: Path | str = ".agent_orchestrator/runs",
        jobs_root: Path | str = ".agent_orchestrator/jobs",
        health_check: ProviderHealthCheck | None = None,
        job_runtime: FileJobRuntime | None = None,
    ) -> None:
        self.team = team
        self.plans_root = Path(plans_root)
        self.runs_root = Path(runs_root)
        self.jobs_root = Path(jobs_root)
        self.run_store = RunStore(root=self.runs_root)
        self.job_runtime = job_runtime or FileJobRuntime(root=self.jobs_root)
        self.event_store = EventStore(root=self.plans_root.parent / "events")
        self.memory_store = MemoryStore(root=self.plans_root.parent / "memory")
        self.message_store = MessageStore(root=self.plans_root.parent / "messages")
        self.health_check = health_check or ProviderHealthCheck(use_cache=True)
        self.agent_config_store = AgentConfigStore(self.plans_root.parent / "agent-config.json")
        self.team.agent_config = self.agent_config_store.read()
        _apply_agent_config_to_orchestrator(self.team.orchestrator, self.team.agent_config)

    def health(self) -> dict[str, object]:
        providers = [self.health_check.check(provider).to_dict() for provider in ("codex", "claude")]
        providers.append({"provider": "mock", "available": True, "detail": "mock provider is always available"})
        return {"providers": providers, "job_runtime": self.job_runtime.__class__.__name__}

    def get_agent_config(self) -> dict[str, object]:
        return self.agent_config_store.read().to_dict()

    def update_agent_config(self, payload: dict[str, object]) -> dict[str, object]:
        config = AgentConfig.from_dict(payload)
        self.agent_config_store.write(config)
        self.team.agent_config = config
        _apply_agent_config_to_orchestrator(self.team.orchestrator, config)
        return config.to_dict()

    def list_sessions(self) -> dict[str, object]:
        sessions = []
        for path in sorted(self.plans_root.glob("*/session.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            payload = _read_json(path)
            if not isinstance(payload, dict):
                continue
            sessions.append(_session_list_item(payload, path))
        return {"sessions": sessions}

    def get_session(self, session_id: str) -> dict[str, object]:
        session = self.team.status(session_id)
        payload = session.to_dict()
        linked_run = None
        run_id = session.resume.linked_execution_run_id
        if run_id and self.run_store.exists(run_id):
            linked_run = self.run_store.read(run_id)
        graph = WorkGraphStore(self.plans_root).read_optional(session_id)
        messages = self.message_store.list_for_session(session_id, limit=50)
        return {
            "session": payload,
            "timeline": _build_timeline(payload),
            "plan_tree": graph_to_plan_tree(graph) if graph else _build_plan_tree(payload, linked_run),
            "work_graph": _work_graph_payload(graph) if graph else None,
            "evidence_summary": _build_evidence_summary(payload, linked_run, self.memory_store.query(session_id=session_id, limit=20)),
            "next_action": _build_next_action(payload),
            "actions": build_session_actions(payload),
            "events": self.event_store.list_for_session(session_id, limit=20),
            "messages": _build_message_summary(messages),
            "runbook": build_operator_runbook(session),
            "agent_cards": _build_agent_cards(payload),
            "role_groups": _build_role_groups(payload, graph, messages),
            "governance_summary": _build_governance_summary(payload),
            "operator_summary": _build_operator_summary(payload, linked_run, graph, messages),
            "linked_execution": linked_run,
        }

    def create_session(self, requirement: str) -> dict[str, object]:
        payload = self.team.start(requirement).to_dict()
        self._record_action("create_session", str(payload.get("id")), payload)
        return payload

    def chat_with_lead(self, session_id: str, *, message: str) -> dict[str, object]:
        assert_session_action_allowed(self.team.status(session_id).to_dict(), "lead_chat", {"message": message})
        payload = self.team.chat_with_lead(session_id, message=message).to_dict()
        self._record_action("lead_chat", session_id, payload)
        return payload

    def mark_draft_ready(self, session_id: str) -> dict[str, object]:
        assert_session_action_allowed(self.team.status(session_id).to_dict(), "mark_draft_ready")
        payload = self.team.mark_draft_ready(session_id).to_dict()
        self._record_action("mark_draft_ready", session_id, payload)
        return payload

    def submit_draft_for_review(self, session_id: str) -> dict[str, object]:
        assert_session_action_allowed(self.team.status(session_id).to_dict(), "submit_review")
        payload = self.team.submit_draft_for_review(session_id).to_dict()
        self._record_action("submit_review", session_id, payload)
        return payload

    def create_ideation_session(self, requirement: str) -> dict[str, object]:
        payload = self.team.ideate(requirement).to_dict()
        self._record_action("ideate", str(payload.get("id")), payload)
        return payload

    def revise_session(self, session_id: str, *, summary: str, closed_gap_ids: list[str] | None = None) -> dict[str, object]:
        assert_session_action_allowed(
            self.team.status(session_id).to_dict(),
            "revise",
            {"summary": summary, "closed_gap_ids": closed_gap_ids or []},
        )
        payload = self.team.revise(session_id, summary=summary, closed_gap_ids=closed_gap_ids or []).to_dict()
        self._record_action("revise", session_id, payload)
        return payload

    def approve_session(self, session_id: str) -> dict[str, object]:
        assert_session_action_allowed(self.team.status(session_id).to_dict(), "approve")
        payload = self.team.approve(session_id).to_dict()
        self._record_action("approve", session_id, payload)
        return payload

    def execute_session(self, session_id: str, *, mode: str | None = None) -> dict[str, object]:
        assert_session_action_allowed(self.team.status(session_id).to_dict(), "execute", {"mode": mode} if mode else {})
        selected_mode = None if mode in {None, "auto"} else OrchestrationMode(str(mode))
        payload = self.team.execute(session_id, selected_mode).to_dict()
        self._record_action("execute", session_id, payload)
        return payload

    def retry_review(self, session_id: str) -> dict[str, object]:
        assert_session_action_allowed(self.team.status(session_id).to_dict(), "retry_review")
        payload = self.team.retry_review(session_id).to_dict()
        self._record_action("retry_review", session_id, payload)
        return payload

    def retry_adversarial_review(self, session_id: str) -> dict[str, object]:
        assert_session_action_allowed(self.team.status(session_id).to_dict(), "retry_adversarial_review")
        payload = self.team.retry_adversarial_review(session_id).to_dict()
        self._record_action("retry_adversarial_review", session_id, payload)
        return payload

    def resume_session(self, session_id: str, *, apply: bool = False) -> dict[str, object]:
        assert_session_action_allowed(self.team.status(session_id).to_dict(), "resume")
        payload = self.team.resume(session_id, apply=apply).to_dict()
        self._record_action("resume", session_id, payload)
        return payload

    def list_events(self, *, limit: int = 100) -> dict[str, object]:
        return {"events": self.event_store.list_recent(limit=limit)}

    def list_session_events(self, session_id: str, *, limit: int = 100) -> dict[str, object]:
        return {"events": self.event_store.list_for_session(session_id, limit=limit)}

    def list_memory(self, *, limit: int = 100) -> dict[str, object]:
        return {"records": self.memory_store.query(limit=limit)}

    def list_session_memory(self, session_id: str, *, limit: int = 100) -> dict[str, object]:
        return {"records": self.memory_store.query(session_id=session_id, limit=limit)}

    def search_memory(self, query: str, *, session_id: str | None = None, limit: int = 5) -> dict[str, object]:
        return {"records": self.memory_store.search(query, session_id=session_id, limit=limit)}

    def list_messages(self, *, limit: int = 100) -> dict[str, object]:
        return {"messages": self.message_store.query(limit=limit)}

    def list_session_messages(self, session_id: str, *, limit: int = 100) -> dict[str, object]:
        return {"messages": self.message_store.list_for_session(session_id, limit=limit)}

    def get_run(self, run_id: str) -> dict[str, object]:
        return self.run_store.read(run_id)

    def list_jobs(self) -> dict[str, object]:
        return {"jobs": [_job_card(job.to_dict(), self.jobs_root) for job in self.job_runtime.list_recent()]}

    def get_job(self, job_id: str) -> dict[str, object]:
        return _job_card(self.job_runtime.status(job_id).to_dict(), self.jobs_root)

    def get_job_log(self, job_id: str) -> dict[str, object]:
        path = self.jobs_root / f"{job_id}.log"
        return {"job_id": job_id, "log": path.read_text(encoding="utf-8") if path.exists() else ""}

    def get_job_terminal_snapshot(self, job_id: str) -> dict[str, object]:
        job = self.job_runtime.status(job_id)
        card = _job_card(job.to_dict(), self.jobs_root)
        return {
            "job_id": card["id"],
            "status": card["status"],
            "phase": card["phase"],
            "provider": card["provider"],
            "model": card["model"],
            "kind": card["kind"],
            "terminal_ref": card["terminal_ref"],
            "attach_available": card["attach_available"],
            "stdout": card["stdout"] or "",
            "summary": card["summary"],
            "last_seen_at": card["last_seen_at"],
        }

    def send_job(self, job_id: str, message: str) -> dict[str, object]:
        try:
            return _job_card(self.job_runtime.send(job_id, message).to_dict(), self.jobs_root)
        except KeyError:
            return _missing_job_operation(job_id, "send")

    def send_job_terminal_input(self, job_id: str, message: str) -> dict[str, object]:
        return self.send_job(job_id, message)

    def cancel_job(self, job_id: str) -> dict[str, object]:
        try:
            return _job_card(self.job_runtime.cancel(job_id).to_dict(), self.jobs_root)
        except KeyError:
            return _missing_job_operation(job_id, "cancel")

    def reconnect_job_terminal(self, job_id: str) -> dict[str, object]:
        return self.get_job_terminal_snapshot(job_id)

    def _record_action(self, action: str, session_id: str, payload: dict[str, object]) -> None:
        self.event_store.append(
            type="action.completed",
            scope="session",
            scope_id=session_id,
            message=f"Dashboard action {action} completed for {session_id}.",
            payload={"session_id": session_id, "action": action, "status": payload.get("status")},
        )
        self.memory_store.append(
            namespace="operator_action",
            session_id=session_id,
            record_type="action",
            role="lead",
            provider="dashboard",
            summary=f"Action {action} completed with status {payload.get('status')}.",
            payload={"action": action, "status": payload.get("status")},
        )


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _apply_agent_config_to_orchestrator(orchestrator: Any, config: AgentConfig) -> None:
    for adapter in (getattr(orchestrator, "worker", None), getattr(orchestrator, "reviewer", None)):
        if hasattr(adapter, "agent_config"):
            adapter.agent_config = config


def _work_graph_payload(graph: WorkUnitGraph) -> dict[str, object]:
    payload = graph.to_dict()
    payload["schedulable_nodes"] = schedulable_nodes(graph)
    return payload


def _session_list_item(payload: dict[str, object], path: Path) -> dict[str, object]:
    summary = payload.get("status_summary", {}) if isinstance(payload.get("status_summary"), dict) else {}
    resume = payload.get("resume", {}) if isinstance(payload.get("resume"), dict) else {}
    brief = payload.get("structured_brief", {}) if isinstance(payload.get("structured_brief"), dict) else {}
    return {
        "id": payload.get("id"),
        "requirement": payload.get("requirement"),
        "goal": brief.get("goal") or payload.get("requirement"),
        "status": payload.get("status"),
        "phase": summary.get("phase") or resume.get("current_phase"),
        "primary_action": summary.get("primary_action"),
        "updated_at": path.stat().st_mtime,
        "linked_execution_run_id": resume.get("linked_execution_run_id"),
    }


def _build_next_action(payload: dict[str, object]) -> dict[str, object]:
    return primary_action_from_registry(payload)


def _build_timeline(payload: dict[str, object]) -> list[dict[str, object]]:
    status = str(payload.get("status", "drafting"))
    resume = payload.get("resume", {}) if isinstance(payload.get("resume"), dict) else {}
    phase = str(resume.get("current_phase") or status)
    if status in {"blocked", "awaiting_human", "needs_followup"}:
        extra_label = status.replace("_", " ").title()
        steps = [*TIMELINE_STEPS, (status, extra_label)]
    else:
        steps = TIMELINE_STEPS
    active_index = next((index for index, (key, _) in enumerate(steps) if key in {status, phase}), 0)
    return [
        {
            "key": key,
            "label": label,
            "state": "active" if index == active_index else "done" if index < active_index else "pending",
        }
        for index, (key, label) in enumerate(steps)
    ]


def _build_plan_tree(payload: dict[str, object], linked_run: dict[str, object] | None) -> dict[str, object]:
    brief = payload.get("structured_brief", {}) if isinstance(payload.get("structured_brief"), dict) else {}
    subtasks = brief.get("subtasks", []) if isinstance(brief.get("subtasks"), list) else []
    gaps = payload.get("gaps", []) if isinstance(payload.get("gaps"), list) else []
    rounds = payload.get("review_rounds", []) if isinstance(payload.get("review_rounds"), list) else []
    status = str(payload.get("status") or "unknown")
    root = {
        "id": str(payload.get("id") or "session"),
        "label": brief.get("goal") or payload.get("requirement") or "当前计划",
        "kind": "session",
        "status": status,
        "state": _node_state(status),
        "summary": payload.get("requirement") or "",
        "related_agent_ids": [card.get("id") for card in _build_agent_cards(payload) if card.get("id")],
        "children": [],
    }

    children: list[dict[str, object]] = []
    children.extend(_subtask_node(item, index) for index, item in enumerate(subtasks, start=1) if isinstance(item, dict))
    children.extend(_gap_node(item, index) for index, item in enumerate(gaps, start=1) if isinstance(item, dict))
    children.extend(_round_node(item, index) for index, item in enumerate(rounds, start=1) if isinstance(item, dict))
    if linked_run:
        children.append(_execution_node(linked_run))
    root["children"] = children
    return root


def _subtask_node(item: dict[str, object], index: int) -> dict[str, object]:
    return {
        "id": str(item.get("id") or f"subtask-{index}"),
        "label": item.get("title") or f"子任务 {index}",
        "kind": "subtask",
        "status": "planned",
        "state": "planned",
        "summary": " / ".join(str(value) for value in item.get("expected_outputs", []) if value) if isinstance(item.get("expected_outputs"), list) else "",
        "related_agent_ids": [],
        "children": [],
    }


def _gap_node(item: dict[str, object], index: int) -> dict[str, object]:
    required = bool(item.get("required", True))
    status = str(item.get("status") or "open")
    return {
        "id": str(item.get("id") or f"gap-{index}"),
        "label": item.get("title") or f"缺口 {index}",
        "kind": "gap",
        "status": status,
        "state": "blocked" if required and status != "closed" else "done" if status == "closed" else "followup",
        "summary": item.get("recommendation") or "",
        "related_agent_ids": [],
        "children": [],
    }


def _round_node(item: dict[str, object], index: int) -> dict[str, object]:
    round_type = str(item.get("round_type") or f"round-{index}")
    job_id = _extract_job_id(str(item.get("summary") or ""))
    return {
        "id": str(item.get("id") or f"round-{index}"),
        "label": _round_label(round_type),
        "kind": "review_round",
        "status": round_type,
        "state": "done",
        "summary": item.get("summary") or "",
        "related_agent_ids": [job_id] if job_id else [],
        "children": [],
    }


def _execution_node(linked_run: dict[str, object]) -> dict[str, object]:
    status = str(linked_run.get("status") or "unknown")
    return {
        "id": str(linked_run.get("run_id") or "linked-run"),
        "label": "执行运行",
        "kind": "execution_run",
        "status": status,
        "state": _node_state(status),
        "summary": str(linked_run.get("summary") or linked_run.get("final_mode") or ""),
        "related_agent_ids": [],
        "children": [],
    }


def _node_state(status: str) -> str:
    if status in {"accepted", "completed", "approved_for_execution"}:
        return "done"
    if status in {"blocked", "failed", "needs_revision"}:
        return "blocked"
    if status in {"needs_followup"}:
        return "followup"
    if status in {"executing", "running", "working", "in_review"}:
        return "running"
    return "planned"


def _round_label(round_type: str) -> str:
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


def _build_agent_cards(payload: dict[str, object]) -> list[dict[str, object]]:
    summary = payload.get("status_summary", {}) if isinstance(payload.get("status_summary"), dict) else {}
    jobs = summary.get("delegated_jobs", []) if isinstance(summary.get("delegated_jobs"), list) else []
    cards = [_session_lead_card(payload)]
    cards.extend(_delegated_job_card(job) for job in jobs if isinstance(job, dict))
    cards.append(_runtime_card(payload))
    return cards


def _delegated_job_card(job: dict[str, object]) -> dict[str, object]:
    role, role_label, layer, layer_label = _role_for_round(str(job.get("round_type") or "delegated"))
    metadata = job.get("metadata", {}) if isinstance(job.get("metadata"), dict) else {}
    return {
        "id": job.get("job_id"),
        "provider": job.get("provider") or "mock",
        "model": job.get("model"),
        "kind": job.get("round_type") or "delegated",
        "status": job.get("status") or "unknown",
        "phase": "failed" if job.get("status") == "failed" else "done",
        "summary": job.get("summary") or "",
        "error": job.get("error"),
        "role": role,
        "role_label": role_label,
        "layer": layer,
        "layer_label": layer_label,
        "current_action": job.get("summary") or job.get("error") or "等待委派任务更新",
        "terminal_ref": metadata.get("terminal_ref"),
        "attach_available": bool(metadata.get("attach_available", False)),
    }


def _session_lead_card(payload: dict[str, object]) -> dict[str, object]:
    summary = payload.get("status_summary", {}) if isinstance(payload.get("status_summary"), dict) else {}
    return {
        "id": payload.get("id"),
        "provider": "decision_core",
        "model": None,
        "kind": "session_lead",
        "status": payload.get("status") or "unknown",
        "phase": summary.get("phase") or payload.get("status") or "unknown",
        "summary": summary.get("primary_reason") or summary.get("next_action_message") or payload.get("requirement") or "",
        "error": None,
        "role": "lead",
        "role_label": "主控 Lead",
        "layer": "decision",
        "layer_label": "决策层",
        "current_action": summary.get("primary_reason") or summary.get("next_action_message") or "协调计划与下一步动作",
        "terminal_ref": None,
        "attach_available": False,
    }


def _runtime_card(payload: dict[str, object]) -> dict[str, object]:
    verdict = payload.get("decision_verdict", {}) if isinstance(payload.get("decision_verdict"), dict) else {}
    provider_runtime = (
        verdict.get("selected_provider_runtime", {})
        if isinstance(verdict.get("selected_provider_runtime"), dict)
        else {}
    )
    runtime = str(provider_runtime.get("runtime") or provider_runtime.get("worker") or "mock")
    return {
        "id": f"{payload.get('id')}-runtime",
        "provider": runtime,
        "model": provider_runtime.get("author_model"),
        "kind": "runtime",
        "status": payload.get("status") or "unknown",
        "phase": "runtime",
        "summary": _format_provider_runtime(provider_runtime) or "等待 provider/runtime 选择",
        "error": None,
        "role": "runtime",
        "role_label": "运行时",
        "layer": "runtime",
        "layer_label": "运行时层",
        "current_action": _format_provider_runtime(provider_runtime) or "承载底层 provider/job 执行",
        "terminal_ref": None,
        "attach_available": False,
    }


def _build_role_groups(
    payload: dict[str, object],
    graph: WorkUnitGraph | None = None,
    messages: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    cards = _build_graph_agent_cards(payload, graph) if graph else _build_agent_cards(payload)
    cards = _attach_message_counts(cards, messages or [])
    grouped = []
    for layer, label in ROLE_GROUPS:
        layer_cards = [card for card in cards if card.get("layer") == layer]
        grouped.append(
            {
                "layer": layer,
                "layer_label": label,
                "cards": layer_cards,
                "count": len(layer_cards),
            }
        )
    return grouped


def _build_message_summary(messages: list[dict[str, object]]) -> dict[str, object]:
    return {
        "count": len(messages),
        "items": messages[:20],
        "latest": messages[0] if messages else None,
    }


def _attach_message_counts(cards: list[dict[str, object]], messages: list[dict[str, object]]) -> list[dict[str, object]]:
    latest_by_role: dict[str, str] = {}
    inbox_counts: dict[str, int] = {}
    outbox_counts: dict[str, int] = {}
    for message in messages:
        from_role = str(message.get("from_role") or "")
        to_role = str(message.get("to_role") or "")
        content = str(message.get("content") or "")
        if to_role:
            inbox_counts[to_role] = inbox_counts.get(to_role, 0) + 1
            latest_by_role.setdefault(to_role, content)
        if from_role:
            outbox_counts[from_role] = outbox_counts.get(from_role, 0) + 1
            latest_by_role.setdefault(from_role, content)
    enriched = []
    for card in cards:
        role = str(card.get("role") or "")
        updated = dict(card)
        updated["inbox_count"] = inbox_counts.get(role, 0)
        updated["outbox_count"] = outbox_counts.get(role, 0)
        updated["latest_message_summary"] = latest_by_role.get(role, "")
        enriched.append(updated)
    return enriched


def _build_graph_agent_cards(payload: dict[str, object], graph: WorkUnitGraph | None) -> list[dict[str, object]]:
    cards_by_role: dict[str, dict[str, object]] = {}
    summary = payload.get("status_summary", {}) if isinstance(payload.get("status_summary"), dict) else {}
    if graph:
        for node in graph.nodes:
            role = get_agent_role(node.owner_role)
            current = cards_by_role.get(role.id)
            related_ids = [*node.linked_job_ids]
            if node.linked_run_id:
                related_ids.append(node.linked_run_id)
            if current is None:
                cards_by_role[role.id] = {
                    "id": f"{payload.get('id')}-{role.id}",
                    "provider": role.default_provider,
                    "kind": node.kind,
                    "status": node.status,
                    "phase": node.status,
                    "summary": node.summary or node.title,
                    "error": None,
                    "role": role.id,
                    "role_label": role.label,
                    "layer": role.layer,
                    "layer_label": role.layer_label,
                    "current_action": node.title,
                    "related_work_unit_ids": [node.id],
                    "related_agent_ids": related_ids,
                    "terminal_ref": None,
                    "attach_available": False,
                }
            else:
                current["related_work_unit_ids"] = [*list(current.get("related_work_unit_ids", [])), node.id]
                current["related_agent_ids"] = [*list(current.get("related_agent_ids", [])), *related_ids]
                current["status"] = _dominant_status(str(current.get("status") or ""), node.status)
                current["phase"] = current["status"]
                current["current_action"] = node.title

    for job_card in _build_agent_cards(payload):
        role_id = str(job_card.get("role") or role_for_job_kind(str(job_card.get("kind") or "")).id)
        role = get_agent_role(role_id)
        current = cards_by_role.get(role.id)
        if current is None:
            cards_by_role[role.id] = dict(job_card)
        else:
            current["id"] = job_card.get("id") or current.get("id")
            current["provider"] = job_card.get("provider") or current.get("provider")
            current["kind"] = job_card.get("kind") or current.get("kind")
            current["status"] = _dominant_status(str(current.get("status") or ""), str(job_card.get("status") or ""))
            current["phase"] = job_card.get("phase") or current.get("phase")
            current["summary"] = job_card.get("summary") or current.get("summary")
            current["current_action"] = job_card.get("current_action") or current.get("current_action")
            current["terminal_ref"] = job_card.get("terminal_ref")
            current["attach_available"] = bool(job_card.get("attach_available", False))

    for role in DEFAULT_AGENT_ROLES.values():
        if role.id in cards_by_role:
            continue
        cards_by_role[role.id] = {
            "id": f"{payload.get('id')}-{role.id}",
            "provider": role.default_provider,
            "kind": "role",
            "status": "idle",
            "phase": "idle",
            "summary": "暂无分配的工作单元",
            "error": None,
            "role": role.id,
            "role_label": role.label,
            "layer": role.layer,
            "layer_label": role.layer_label,
            "current_action": "等待任务",
            "related_work_unit_ids": [],
            "related_agent_ids": [],
            "terminal_ref": None,
            "attach_available": False,
        }

    order = list(DEFAULT_AGENT_ROLES)
    return sorted(cards_by_role.values(), key=lambda card: order.index(str(card.get("role"))) if str(card.get("role")) in order else len(order))


def _dominant_status(current: str, incoming: str) -> str:
    rank = {
        "failed": 6,
        "blocked": 6,
        "needs_revision": 5,
        "executing": 4,
        "running": 4,
        "working": 4,
        "in_review": 3,
        "approved_for_execution": 2,
        "accepted": 1,
        "completed": 1,
        "idle": 0,
        "planned": 0,
    }
    return incoming if rank.get(incoming, 0) >= rank.get(current, 0) else current


def _build_governance_summary(payload: dict[str, object]) -> dict[str, object]:
    summary = payload.get("status_summary", {}) if isinstance(payload.get("status_summary"), dict) else {}
    verdict = payload.get("decision_verdict", {}) if isinstance(payload.get("decision_verdict"), dict) else {}
    compliance = payload.get("compliance", {}) if isinstance(payload.get("compliance"), dict) else {}
    provider_runtime = (
        verdict.get("selected_provider_runtime", {})
        if isinstance(verdict.get("selected_provider_runtime"), dict)
        else {}
    )
    recovery_actions = summary.get("recovery_actions", []) if isinstance(summary.get("recovery_actions"), list) else []
    blocking_reasons = summary.get("blocking_reasons", []) if isinstance(summary.get("blocking_reasons"), list) else []
    recommended_commands = summary.get("recommended_commands", []) if isinstance(summary.get("recommended_commands"), list) else []
    warnings = summary.get("warnings", []) if isinstance(summary.get("warnings"), list) else []
    return {
        "selected_topology": summary.get("selected_topology") or verdict.get("selected_topology"),
        "topology_reason": summary.get("topology_reason"),
        "selected_provider_runtime": provider_runtime,
        "primary_action": summary.get("primary_action"),
        "primary_reason": summary.get("primary_reason") or summary.get("next_action_message", ""),
        "review_intensity": _review_intensity(payload),
        "gate_status": _gate_status(payload, blocking_reasons),
        "compliance_status": compliance.get("status", "unknown"),
        "blocking": bool(blocking_reasons),
        "blocking_reasons": blocking_reasons,
        "warnings": warnings,
        "recovery_actions": recovery_actions,
        "recovery_action_count": len(recovery_actions),
        "recommended_commands": recommended_commands,
        "recommended_command_count": len(recommended_commands),
        "recovery_provider": summary.get("recovery_provider"),
        "recovery_round_type": summary.get("recovery_round_type"),
        "recovery_provider_mode": summary.get("recovery_provider_mode"),
        "recovery_provider_fallback_from": summary.get("recovery_provider_fallback_from"),
        "recovery_provider_fallback_reason": summary.get("recovery_provider_fallback_reason"),
        "recovery_provider_fallback_detail": summary.get("recovery_provider_fallback_detail"),
    }


def _build_evidence_summary(
    payload: dict[str, object],
    linked_run: dict[str, object] | None,
    memory_records: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    rounds = payload.get("review_rounds", []) if isinstance(payload.get("review_rounds"), list) else []
    gaps = payload.get("gaps", []) if isinstance(payload.get("gaps"), list) else []
    summary = payload.get("status_summary", {}) if isinstance(payload.get("status_summary"), dict) else {}
    jobs = summary.get("delegated_jobs", []) if isinstance(summary.get("delegated_jobs"), list) else []
    providers = sorted({str(job.get("provider")) for job in jobs if isinstance(job, dict) and job.get("provider")})
    findings = [
        finding
        for round_ in rounds
        if isinstance(round_, dict) and isinstance(round_.get("review_result"), dict)
        for finding in round_.get("review_result", {}).get("findings", [])
        if isinstance(finding, dict)
    ]
    memory_records = memory_records or []
    postmortems = [record for record in memory_records if record.get("record_type") == "postmortem"]
    return {
        "review_round_count": len(rounds),
        "gap_count": len(gaps),
        "finding_count": len(findings),
        "delegated_job_count": len(jobs),
        "providers": providers,
        "linked_run_id": linked_run.get("run_id") if linked_run else None,
        "linked_run_status": linked_run.get("status") if linked_run else None,
        "memory_namespaces": ["plan_session", "run_artifact", "job_log"],
        "tool_surfaces": ["team_orchestrator", "run_store", "job_runtime"],
        "memory_record_count": len(memory_records),
        "postmortem_count": len(postmortems),
        "recent_memory": [
            {
                "namespace": record.get("namespace"),
                "record_type": record.get("record_type"),
                "summary": record.get("summary"),
            }
            for record in memory_records[:5]
        ],
        "retrieved_memory": memory_records[:3],
    }


def _build_operator_summary(
    payload: dict[str, object],
    linked_run: dict[str, object] | None,
    graph: WorkUnitGraph | None,
    messages: list[dict[str, object]],
) -> dict[str, object]:
    summary = payload.get("status_summary", {}) if isinstance(payload.get("status_summary"), dict) else {}
    verdict = payload.get("decision_verdict", {}) if isinstance(payload.get("decision_verdict"), dict) else {}
    compliance = payload.get("compliance", {}) if isinstance(payload.get("compliance"), dict) else {}
    approved_plan = payload.get("approved_plan", {}) if isinstance(payload.get("approved_plan"), dict) else {}
    execution_contract = (
        approved_plan.get("execution_contract", {}) if isinstance(approved_plan.get("execution_contract"), dict) else {}
    )
    linked_metadata = linked_run.get("metadata", {}) if isinstance(linked_run, dict) and isinstance(linked_run.get("metadata"), dict) else {}
    provenance = linked_metadata.get("provenance", {}) if isinstance(linked_metadata.get("provenance"), dict) else {}
    provider_runtime = verdict.get("selected_provider_runtime", {}) if isinstance(verdict.get("selected_provider_runtime"), dict) else {}
    fallback_policy = execution_contract.get("fallback_policy", {}) if isinstance(execution_contract.get("fallback_policy"), dict) else {}
    review_policy = execution_contract.get("review_policy", {}) if isinstance(execution_contract.get("review_policy"), dict) else {}
    events = payload.get("events", []) if isinstance(payload.get("events"), list) else []
    return {
        "session": {
            "id": payload.get("id"),
            "status": payload.get("status"),
            "phase": summary.get("phase") or payload.get("resume", {}).get("current_phase")
            if isinstance(payload.get("resume"), dict)
            else None,
            "primary_action": summary.get("primary_action"),
            "linked_execution_run_id": provenance.get("linked_execution_run_id")
            or (linked_run.get("run_id") if isinstance(linked_run, dict) else None),
        },
        "execution_provenance": {
            "plan_session_id": provenance.get("plan_session_id"),
            "approved_plan_goal": provenance.get("approved_plan_goal"),
            "source_requirement": provenance.get("source_requirement"),
            "selected_topology": provenance.get("selected_topology") or verdict.get("selected_topology"),
            "selected_provider_runtime": provenance.get("selected_provider_runtime") or provider_runtime,
            "linked_run_status": linked_run.get("status") if isinstance(linked_run, dict) else None,
        },
        "review_policy": review_policy or payload.get("structured_brief", {}).get("review_policy", {})
        if isinstance(payload.get("structured_brief"), dict)
        else {},
        "fallback_snapshot": {
            "provider_runtime": provider_runtime,
            "fallback_policy": fallback_policy,
            "recovery_provider": summary.get("recovery_provider"),
            "recovery_provider_fallback_from": summary.get("recovery_provider_fallback_from"),
            "recovery_provider_fallback_reason": summary.get("recovery_provider_fallback_reason"),
            "recovery_provider_fallback_detail": summary.get("recovery_provider_fallback_detail"),
        },
        "compliance_snapshot": {
            "status": compliance.get("status", "unknown"),
            "blocking": bool(compliance.get("blocking", False)),
            "blocking_reasons": list(compliance.get("blocking_reasons", []))
            if isinstance(compliance.get("blocking_reasons"), list)
            else [],
            "warnings": list(compliance.get("warnings", [])) if isinstance(compliance.get("warnings"), list) else [],
            "required_actions": list(compliance.get("required_actions", []))
            if isinstance(compliance.get("required_actions"), list)
            else [],
        },
        "event_timeline": events[:10],
        "message_timeline": [
            {
                "from_role": message.get("from_role"),
                "to_role": message.get("to_role"),
                "message_type": message.get("message_type"),
                "content": message.get("content"),
            }
            for message in messages[:10]
        ],
        "work_graph_summary": {
            "node_count": len(graph.nodes) if graph else 0,
            "edge_count": len(graph.edges) if graph else 0,
            "schedulable_nodes": schedulable_nodes(graph) if graph else [],
        },
    }


def _review_intensity(payload: dict[str, object]) -> str:
    verdict = payload.get("decision_verdict", {}) if isinstance(payload.get("decision_verdict"), dict) else {}
    selected = verdict.get("selected_provider_runtime", {}) if isinstance(verdict.get("selected_provider_runtime"), dict) else {}
    if payload.get("status") in {"blocked", "needs_revision", "awaiting_human"}:
        return "strict"
    if selected.get("reviewer") or selected.get("review_provider"):
        return "reviewed"
    return "standard"


def _gate_status(payload: dict[str, object], blocking_reasons: list[object]) -> str:
    status = str(payload.get("status") or "")
    if blocking_reasons or status in {"blocked", "awaiting_human"}:
        return "blocked"
    if status == "needs_revision":
        return "needs_revision"
    if status == "approved_for_execution":
        return "approved"
    if status in {"accepted", "needs_followup"}:
        return "completed"
    return "open"


def _role_for_round(round_type: str) -> tuple[str, str, str, str]:
    normalized = round_type.replace("-", "_")
    if normalized in {"review", "review_retry"}:
        return "reviewer", "审核 Reviewer", "review", "审核层"
    if normalized in {"adversarial_review", "adversarial_review_retry"}:
        return "adversarial_reviewer", "对抗审核", "review", "审核层"
    if normalized == "rescue":
        return "rescue", "救援 Rescue", "rescue", "救援层"
    if normalized in {"implementation", "build"}:
        return "builder", "执行 Builder", "execution", "执行层"
    if normalized == "validation":
        return "validator", "验证 Validator", "execution", "执行层"
    return "planner", "规划 Planner", "decision", "决策层"


def _format_provider_runtime(provider_runtime: dict[str, object]) -> str:
    if not provider_runtime:
        return ""
    parts = [f"{key}:{value}" for key, value in provider_runtime.items() if value not in {None, ""}]
    return " · ".join(parts)


def _job_card(job: dict[str, object], jobs_root: Path | None = None) -> dict[str, object]:
    metadata = job.get("metadata", {}) if isinstance(job.get("metadata"), dict) else {}
    job_id = str(job.get("id") or "")
    stdout = str(job.get("stdout") or "")
    stderr = str(job.get("stderr") or "")
    error = str(job.get("error") or "")
    log_text = ""
    if job_id and jobs_root and (jobs_root / f"{job_id}.log").exists():
        log_text = (jobs_root / f"{job_id}.log").read_text(encoding="utf-8")
    return {
        "id": job_id,
        "task_id": job.get("task_id"),
        "provider": job.get("provider"),
        "model": job.get("model"),
        "kind": job.get("kind"),
        "status": job.get("status"),
        "phase": job.get("phase"),
        "summary": job.get("summary"),
        "error": job.get("error"),
        "pid": job.get("pid"),
        "exit_code": job.get("exit_code"),
        "session_id": job.get("session_id"),
        "thread_id": job.get("thread_id"),
        "command": job.get("command", []),
        "stdout": job.get("stdout"),
        "stderr": job.get("stderr"),
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "updated_at": job.get("updated_at"),
        "log_available": bool(log_text),
        "output_preview": _output_preview(stdout=stdout, stderr=stderr, error=error),
        "terminal_ref": metadata.get("terminal_ref"),
        "attach_available": bool(metadata.get("attach_available", False)),
        "last_log_excerpt": _log_excerpt(log_text),
        "last_seen_at": job.get("updated_at") or job.get("completed_at") or job.get("started_at"),
        "operation": _job_operation(job),
    }


def _job_operation(job: dict[str, object]) -> dict[str, object] | None:
    parsed = job.get("parsed_payload", {}) if isinstance(job.get("parsed_payload"), dict) else {}
    operation = parsed.get("operation") if isinstance(parsed, dict) else None
    return operation if isinstance(operation, dict) else None


def _missing_job_operation(job_id: str, action: str) -> dict[str, object]:
    return {
        "id": job_id,
        "status": "missing",
        "operation": {
            "action": action,
            "status": "session_missing",
            "reason": "session_missing",
            "detail": f"Job {job_id} is not available.",
        },
    }


def _output_preview(*, stdout: str, stderr: str, error: str) -> str:
    text = error or stderr or stdout
    return text.strip().replace("\n", " ")[:180]


def _log_excerpt(text: str) -> str:
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " ".join(lines[-3:])[:240]


def build_dashboard_service(
    *,
    plans_root: str = ".agent_orchestrator/plans",
    runs_root: str = ".agent_orchestrator/runs",
    jobs_root: str = ".agent_orchestrator/jobs",
    runtime: str = "mock",
    provider: str | None = None,
) -> DashboardService:
    from agent_orchestrator.cli import _build_team_orchestrator

    team_runtime = "mock" if runtime == "tmux" else runtime
    team = _build_team_orchestrator(team_runtime, provider, plans_root, runs_root)
    job_runtime = TmuxJobRuntime(root=jobs_root) if runtime == "tmux" else FileJobRuntime(root=jobs_root)
    return DashboardService(team=team, plans_root=plans_root, runs_root=runs_root, jobs_root=jobs_root, job_runtime=job_runtime)

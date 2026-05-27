"""AI Work Control Plane artifact models and snapshot builders."""
from __future__ import annotations

# DEPS: __future__, agent_orchestrator, dataclasses, hashlib, json, pathlib, shutil, subprocess, tempfile, typing
# RESPONSIBILITY: Build AI-native workspace, context, strategy, topology, approval, evidence, and memory artifacts.
# MODULE: decision_core
# ---

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal

from agent_orchestrator.events import EventStore
from agent_orchestrator.jobs import AgentJob, FileJobRuntime, now_iso, runtime_measurement_payload
from agent_orchestrator.memory import MemoryRecord, MemoryStore
from agent_orchestrator.planning import PlanSession
from agent_orchestrator.planning_support import build_document_context_package
from agent_orchestrator.work_graph import WorkGraphStore

ApprovalStatus = Literal["pending", "approved", "rejected", "resolved"]
ApprovalReasonCode = Literal[
    "blocked_session",
    "awaiting_human_decision",
    "compliance_blocking",
    "provider_fallback",
    "rescue_reroute",
    "dirty_state_overlap",
    "external_cache_unavailable",
]

CONTROL_PLANE_FORMATS = {
    "workspace_index": "agent_orchestrator.workspace_index.v1",
    "workspace_state": "agent_orchestrator.workspace_state.v1",
    "context_packet": "agent_orchestrator.context_packet.v1",
    "strategy_decision": "agent_orchestrator.strategy_decision.v1",
    "topology_snapshot": "agent_orchestrator.execution_topology_snapshot.v1",
    "approval_item": "agent_orchestrator.approval_item.v1",
    "approval_queue": "agent_orchestrator.approval_queue.v1",
    "evidence_bundle": "agent_orchestrator.evidence_bundle.v1",
    "run_ledger": "agent_orchestrator.run_ledger.v1",
    "recovery_timeline": "agent_orchestrator.recovery_timeline.v1",
    "runtime_event_stream": "agent_orchestrator.runtime_event_stream.v1",
    "provider_session_snapshot": "agent_orchestrator.provider_session_snapshot.v1",
    "runtime_operation_receipt": "agent_orchestrator.runtime_operation_receipt.v1",
}

RECOVERY_TIMELINE_STATUSES = [
    "started",
    "checkpointed",
    "awaiting_human",
    "approval_blocked",
    "evidence_blocked",
    "compliance_blocked",
    "provider_degraded",
    "runtime_failed",
    "interrupted",
    "recovery_ready",
    "completed",
]

TOPOLOGY_NODE_TYPES = [
    "state",
    "context",
    "strategy",
    "manager_slot",
    "worker",
    "implementation",
    "review",
    "rescue",
    "condition",
    "approval",
    "evidence",
    "memory",
]


@dataclass(frozen=True, slots=True)
class WorkspaceStateSnapshot:
    project_root: str
    plans: list[dict[str, object]]
    runs: list[dict[str, object]]
    jobs: list[dict[str, object]]
    evidence: dict[str, object]
    approvals: list[dict[str, object]]
    provider_health: dict[str, object] | None
    dirty_state: dict[str, object]
    memory_digest: dict[str, object]
    external_cache: dict[str, object]
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, object]:
        return {
            "format": CONTROL_PLANE_FORMATS["workspace_state"],
            "project_root": self.project_root,
            "plans": list(self.plans),
            "runs": list(self.runs),
            "jobs": list(self.jobs),
            "evidence": dict(self.evidence),
            "approvals": list(self.approvals),
            "provider_health": self.provider_health,
            "dirty_state": dict(self.dirty_state),
            "memory_digest": dict(self.memory_digest),
            "external_cache": dict(self.external_cache),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "WorkspaceStateSnapshot":
        return cls(
            project_root=str(data.get("project_root") or ""),
            plans=[dict(item) for item in data.get("plans", []) if isinstance(item, dict)],
            runs=[dict(item) for item in data.get("runs", []) if isinstance(item, dict)],
            jobs=[dict(item) for item in data.get("jobs", []) if isinstance(item, dict)],
            evidence=dict(data.get("evidence", {})) if isinstance(data.get("evidence"), dict) else {},
            approvals=[dict(item) for item in data.get("approvals", []) if isinstance(item, dict)],
            provider_health=dict(data.get("provider_health", {})) if isinstance(data.get("provider_health"), dict) else None,
            dirty_state=dict(data.get("dirty_state", {})) if isinstance(data.get("dirty_state"), dict) else {},
            memory_digest=dict(data.get("memory_digest", {})) if isinstance(data.get("memory_digest"), dict) else {},
            external_cache=dict(data.get("external_cache", {})) if isinstance(data.get("external_cache"), dict) else {},
            created_at=str(data.get("created_at") or now_iso()),
        )


@dataclass(frozen=True, slots=True)
class ApprovalItem:
    id: str
    status: ApprovalStatus
    reason_code: ApprovalReasonCode
    reason: str
    scope: str
    scope_id: str
    recommended_action: str
    session_id: str | None = None
    run_id: str | None = None
    job_id: str | None = None
    work_unit_id: str | None = None
    plan_ref: str | None = None
    topology_ref: str | None = None
    run_ref: str | None = None
    job_ref: str | None = None
    evidence_ref: str | None = None
    memory_candidate_ref: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    resolved_at: str | None = None
    resolution_reason: str | None = None
    actor: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "format": CONTROL_PLANE_FORMATS["approval_item"],
            "id": self.id,
            "status": self.status,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "recommended_action": self.recommended_action,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "job_id": self.job_id,
            "work_unit_id": self.work_unit_id,
            "plan_ref": self.plan_ref,
            "topology_ref": self.topology_ref,
            "run_ref": self.run_ref,
            "job_ref": self.job_ref,
            "evidence_ref": self.evidence_ref,
            "memory_candidate_ref": self.memory_candidate_ref,
            "evidence_refs": list(self.evidence_refs),
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "resolution_reason": self.resolution_reason,
            "actor": self.actor,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "ApprovalItem":
        return cls(
            id=str(data.get("id") or _stable_id("approval", str(data))),
            status=str(data.get("status") or "pending"),  # type: ignore[arg-type]
            reason_code=_approval_reason_code_from_payload(data),
            reason=str(data.get("reason") or ""),
            scope=str(data.get("scope") or "unknown"),
            scope_id=str(data.get("scope_id") or ""),
            recommended_action=str(data.get("recommended_action") or "inspect"),
            session_id=data.get("session_id") if isinstance(data.get("session_id"), str) else None,
            run_id=data.get("run_id") if isinstance(data.get("run_id"), str) else None,
            job_id=data.get("job_id") if isinstance(data.get("job_id"), str) else None,
            work_unit_id=data.get("work_unit_id") if isinstance(data.get("work_unit_id"), str) else None,
            plan_ref=data.get("plan_ref") if isinstance(data.get("plan_ref"), str) else None,
            topology_ref=data.get("topology_ref") if isinstance(data.get("topology_ref"), str) else None,
            run_ref=data.get("run_ref") if isinstance(data.get("run_ref"), str) else None,
            job_ref=data.get("job_ref") if isinstance(data.get("job_ref"), str) else None,
            evidence_ref=data.get("evidence_ref") if isinstance(data.get("evidence_ref"), str) else None,
            memory_candidate_ref=data.get("memory_candidate_ref") if isinstance(data.get("memory_candidate_ref"), str) else None,
            evidence_refs=[str(item) for item in data.get("evidence_refs", [])],
            created_at=str(data.get("created_at") or now_iso()),
            resolved_at=data.get("resolved_at") if isinstance(data.get("resolved_at"), str) else None,
            resolution_reason=data.get("resolution_reason") if isinstance(data.get("resolution_reason"), str) else None,
            actor=data.get("actor") if isinstance(data.get("actor"), str) else None,
        )

    def resolved(self, *, status: ApprovalStatus, reason: str, actor: str = "human") -> "ApprovalItem":
        return ApprovalItem(
            id=self.id,
            status=status,
            reason_code=self.reason_code,
            reason=self.reason,
            scope=self.scope,
            scope_id=self.scope_id,
            recommended_action=self.recommended_action,
            session_id=self.session_id,
            run_id=self.run_id,
            job_id=self.job_id,
            work_unit_id=self.work_unit_id,
            plan_ref=self.plan_ref,
            topology_ref=self.topology_ref,
            run_ref=self.run_ref,
            job_ref=self.job_ref,
            evidence_ref=self.evidence_ref,
            memory_candidate_ref=self.memory_candidate_ref,
            evidence_refs=list(self.evidence_refs),
            created_at=self.created_at,
            resolved_at=now_iso(),
            resolution_reason=reason,
            actor=actor,
        )


@dataclass(slots=True)
class WorkspaceIndexStore:
    root: Path | str = ".agent_orchestrator/workspace"

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self.root / "index.json"

    def write(self, snapshot: WorkspaceStateSnapshot) -> dict[str, object]:
        existing = _read_json_object(self.path)
        artifacts = existing.get("artifacts", {}) if isinstance(existing.get("artifacts"), dict) else {}
        payload = _workspace_index_payload(
            snapshot,
            artifacts={
                **artifacts,
                "workspace_state": _artifact_ref(snapshot.to_dict()),
            },
        )
        return _atomic_write_json(self.path, payload)

    def write_index(self, payload: dict[str, object]) -> dict[str, object]:
        return _atomic_write_json(self.path, payload)

    def payload(self) -> dict[str, object] | None:
        payload = _read_json_object(self.path)
        return payload or None

    def record_artifact(self, name: str, payload: dict[str, object]) -> dict[str, object]:
        existing = _read_json_object(self.path)
        artifacts = existing.get("artifacts", {}) if isinstance(existing.get("artifacts"), dict) else {}
        workspace_state = existing.get("workspace_state") if isinstance(existing.get("workspace_state"), dict) else None
        if workspace_state is None and existing.get("format") == CONTROL_PLANE_FORMATS["workspace_state"]:
            workspace_state = existing
        index_payload = {
            "format": CONTROL_PLANE_FORMATS["workspace_index"],
            "workspace_state": workspace_state,
            "artifacts": {
                **artifacts,
                name: _artifact_ref(payload),
            },
            "updated_at": now_iso(),
        }
        if isinstance(workspace_state, dict):
            index_payload.update(
                _workspace_index_optional_sections(
                    WorkspaceStateSnapshot.from_dict(workspace_state),
                    artifacts=index_payload["artifacts"],
                )
            )
        return _atomic_write_json(self.path, index_payload)

    def read(self) -> WorkspaceStateSnapshot | None:
        if not self.path.exists():
            return None
        payload = _read_json_object(self.path)
        if not payload:
            return None
        if payload.get("format") == CONTROL_PLANE_FORMATS["workspace_index"]:
            workspace_state = payload.get("workspace_state")
            return WorkspaceStateSnapshot.from_dict(workspace_state) if isinstance(workspace_state, dict) else None
        return WorkspaceStateSnapshot.from_dict(payload) if isinstance(payload, dict) else None


@dataclass(slots=True)
class ApprovalStore:
    root: Path | str = ".agent_orchestrator/approvals"

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self.root / "approvals.jsonl"

    def append(self, item: ApprovalItem) -> ApprovalItem:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")
        return item

    def list_all(self) -> list[ApprovalItem]:
        if not self.path.exists():
            return []
        items: list[ApprovalItem] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                items.append(ApprovalItem.from_dict(payload))
        return items

    def latest_by_id(self) -> dict[str, ApprovalItem]:
        latest: dict[str, ApprovalItem] = {}
        for item in self.list_all():
            latest[item.id] = item
        return latest


def build_workspace_state_snapshot(
    project_root: Path | str = ".",
    *,
    plans_root: Path | str = ".agent_orchestrator/plans",
    runs_root: Path | str = ".agent_orchestrator/runs",
    jobs_root: Path | str = ".agent_orchestrator/jobs",
    approvals_root: Path | str = ".agent_orchestrator/approvals",
    provider_health: dict[str, object] | None = None,
    write_index: bool = False,
) -> dict[str, object]:
    root = Path(project_root)
    plans_path = _resolve_root(root, plans_root)
    runs_path = _resolve_root(root, runs_root)
    jobs_path = _resolve_root(root, jobs_root)
    approvals_path = _resolve_root(root, approvals_root)
    sessions = _read_plan_sessions(plans_path)
    approvals = build_approval_queue(
        root,
        plans_root=plans_path,
        approvals_root=approvals_path,
        sessions=sessions,
    )
    snapshot = WorkspaceStateSnapshot(
        project_root=str(root.resolve()),
        plans=[_session_index_entry(session) for session in sessions],
        runs=_read_run_entries(runs_path),
        jobs=_read_job_entries(jobs_path),
        evidence=_evidence_state(root),
        approvals=approvals["items"],
        provider_health=provider_health,
        dirty_state=_git_dirty_state(root),
        memory_digest=_memory_digest(root / ".agent_orchestrator" / "memory"),
        external_cache=_external_cache_status(root),
    )
    if write_index:
        WorkspaceIndexStore(root / ".agent_orchestrator" / "workspace").write(snapshot)
    return snapshot.to_dict()


def build_workspace_index(
    project_root: Path | str = ".",
    *,
    plans_root: Path | str = ".agent_orchestrator/plans",
    runs_root: Path | str = ".agent_orchestrator/runs",
    jobs_root: Path | str = ".agent_orchestrator/jobs",
    approvals_root: Path | str = ".agent_orchestrator/approvals",
    provider_health: dict[str, object] | None = None,
) -> dict[str, object]:
    root = Path(project_root)
    plans_path = _resolve_root(root, plans_root)
    runs_path = _resolve_root(root, runs_root)
    jobs_path = _resolve_root(root, jobs_root)
    approvals_path = _resolve_root(root, approvals_root)
    snapshot_payload = build_workspace_state_snapshot(
        root,
        plans_root=plans_path,
        runs_root=runs_path,
        jobs_root=jobs_path,
        approvals_root=approvals_path,
        provider_health=provider_health,
        write_index=False,
    )
    snapshot = WorkspaceStateSnapshot.from_dict(snapshot_payload)
    loaded_sessions = _read_plan_sessions(plans_path)
    runtime_events = build_runtime_event_stream(
        root,
        plans_root=plans_path,
        runs_root=runs_path,
        jobs_root=jobs_path,
        approvals_root=approvals_path,
        sessions=loaded_sessions,
    )
    recovery_timeline = build_recovery_timeline(
        root,
        plans_root=plans_path,
        runs_root=runs_path,
        jobs_root=jobs_path,
        approvals_root=approvals_path,
        sessions=loaded_sessions,
    )
    active_session = next((session for session in loaded_sessions if session.status not in {"accepted", "completed"}), None)
    recovery_recommendation = build_recovery_recommendation(
        active_session,
        recovery_timeline=recovery_timeline,
        runtime_event_stream=runtime_events,
    ) if active_session is not None else None
    index = WorkspaceIndexStore(root / ".agent_orchestrator" / "workspace")
    existing = index.payload() or {}
    artifacts = existing.get("artifacts", {}) if isinstance(existing.get("artifacts"), dict) else {}
    dashboard = _workspace_recovery_dashboard(
        recovery_timeline=recovery_timeline,
        runtime_events=runtime_events,
        recovery_recommendation=recovery_recommendation,
    )
    payload = _workspace_index_payload(
        snapshot,
        artifacts={
            **artifacts,
            "workspace_state": _artifact_ref(snapshot_payload),
            "recovery_timeline": _artifact_ref(recovery_timeline),
            "runtime_event_stream": _artifact_ref(runtime_events),
            **({"recovery_recommendation": _artifact_ref(recovery_recommendation)} if recovery_recommendation else {}),
        },
    )
    payload.update(dashboard)
    return index.write_index(payload)


def build_context_packet(
    project_root: Path | str = ".",
    *,
    query: str = "",
    changed_files: list[str] | None = None,
    jobs_root: Path | str = ".agent_orchestrator/jobs",
    memory_root: Path | str = ".agent_orchestrator/memory",
) -> dict[str, object]:
    root = Path(project_root)
    jobs_path = _resolve_root(root, jobs_root)
    memory_path = _resolve_root(root, memory_root)
    changed = list(changed_files or [])
    docs_context = build_document_context_package(
        root,
        FileJobRuntime(jobs_path),
        query=query,
        changed_files=changed,
        include_all=False,
    )
    memory = MemoryStore(memory_path)
    memory_records = memory.search(query, limit=5) if query.strip() else memory.query(limit=5)
    source_artifacts = [
        {"kind": "doc", "id": doc_id}
        for doc_id in docs_context.get("selected_doc_ids", [])
    ]
    source_artifacts.extend(
        {"kind": "memory", "id": str(record.get("id", ""))}
        for record in memory_records
        if record.get("id")
    )
    content_chars = len(str(docs_context.get("injection_markdown", ""))) + sum(
        len(str(record.get("summary", ""))) for record in memory_records
    )
    stale_warnings = _context_stale_warnings(docs_context, memory_records)
    payload = {
        "format": CONTROL_PLANE_FORMATS["context_packet"],
        "query": query,
        "changed_files": changed,
        "docs_context": docs_context,
        "memory_records": memory_records,
        "source_artifacts": source_artifacts,
        "stale_warnings": stale_warnings,
        "token_budget_summary": {
            "estimated_chars": content_chars,
            "estimated_tokens": max(1, content_chars // 4) if content_chars else 0,
            "policy": "minimum sufficient context; does not choose strategy",
        },
        "external_cache": _external_cache_status(root),
        "created_at": now_iso(),
    }
    WorkspaceIndexStore(root / ".agent_orchestrator" / "workspace").record_artifact("context_packet", payload)
    return payload


def build_strategy_decision(session: PlanSession, workspace_state: dict[str, object] | None = None) -> dict[str, object]:
    summary = session.to_dict().get("status_summary", {})
    status = summary if isinstance(summary, dict) else {}
    decision = session.decision_verdict.to_dict() if session.decision_verdict else {}
    next_task = status.get("next_executable_task") if isinstance(status.get("next_executable_task"), dict) else None
    next_goal = str(next_task.get("title")) if isinstance(next_task, dict) else str(status.get("primary_reason") or session.requirement)
    validation_plan = [
        "Run the current phase targeted pytest slice before moving phases.",
        "Run full pytest and team check-compliance only at convergence.",
    ]
    if isinstance(next_task, dict):
        validation_plan.extend(str(item) for item in next_task.get("validation", []) if item)
    return {
        "format": CONTROL_PLANE_FORMATS["strategy_decision"],
        "session_id": session.id,
        "goal": session.structured_brief.goal or session.requirement,
        "next_goal": next_goal,
        "status": session.status,
        "selected_topology": decision.get("selected_topology"),
        "selected_provider_runtime": decision.get("selected_provider_runtime", {}),
        "control_plane_focus": "state_context_strategy_topology_approval_evidence_memory_recovery",
        "orchestration_horizon": {
            "short_term": "explicit orchestration solves real local work",
            "medium_term": "control plane governs orchestration and evidence",
            "long_term": "models may internalize orchestration while external artifacts remain auditable",
        },
        "topology_policy": _strategy_topology_policy(session, status, decision),
        "recovery_policy": _strategy_recovery_policy(status),
        "runtime_health": _runtime_health_payload(decision.get("selected_provider_runtime", {})),
        "tool_inventory": _tool_inventory_payload(),
        "usage_cost": _usage_cost_placeholder(),
        "rationale": list(decision.get("decision_rationale", [])) if isinstance(decision.get("decision_rationale"), list) else [],
        "tradeoffs": [
            "Keep explicit orchestration for short-term reliability.",
            "Move durable state, evidence, approvals, and memory into the control-plane artifact chain.",
            "Allow orchestration to shrink over time while keeping state, evidence, approvals, memory, and recovery external.",
        ],
        "risks": [str(item) for item in session.structured_brief.risks],
        "validation_plan": validation_plan,
        "executes": False,
        "workspace_state_created_at": workspace_state.get("created_at") if isinstance(workspace_state, dict) else None,
        "created_at": now_iso(),
    }


def _strategy_topology_policy(
    session: PlanSession,
    status: dict[str, object],
    decision: dict[str, object],
) -> dict[str, object]:
    recommendation = (
        dict(session.structured_brief.topology_recommendation)
        if isinstance(session.structured_brief.topology_recommendation, dict)
        else {}
    )
    provider_runtime = decision.get("selected_provider_runtime", {})
    fallback_signals = {
        key: value
        for key, value in dict(provider_runtime).items()
        if isinstance(key, str) and "fallback" in key and value
    } if isinstance(provider_runtime, dict) else {}
    return {
        "task_size": recommendation.get("subtask_count"),
        "selected_topology": decision.get("selected_topology") or recommendation.get("recommended_topology"),
        "selection_reason": recommendation.get("selection_reason") or status.get("topology_reason"),
        "signals": recommendation.get("signals", {}),
        "review_policy": dict(session.structured_brief.review_policy)
        if isinstance(session.structured_brief.review_policy, dict)
        else {},
        "provider_fallback": fallback_signals,
    }


def _strategy_recovery_policy(status: dict[str, object]) -> dict[str, object]:
    recovery_semantics = status.get("recovery_semantics", {}) if isinstance(status.get("recovery_semantics"), dict) else {}
    return {
        "resume_action": status.get("resume_action"),
        "resume_reason": status.get("resume_reason"),
        "recovery_actions": list(status.get("recovery_actions", []))
        if isinstance(status.get("recovery_actions"), list)
        else [],
        "interruption_aware": bool(recovery_semantics.get("interruption_aware", True)),
        "execution_gate_authority": recovery_semantics.get("execution_gate_authority", "approved_plan_gate"),
        "records_only": bool(recovery_semantics.get("records_only", True)),
    }


def _runtime_health_payload(provider_runtime: object | None = None) -> dict[str, object]:
    runtime = provider_runtime if isinstance(provider_runtime, dict) else {}
    runtime_mode = str(runtime.get("runtime_mode") or runtime.get("mode") or "unknown")
    provider = runtime.get("provider") or runtime.get("selected_provider") or "unknown"
    degraded_reason = runtime.get("fallback_detail") or runtime.get("fallback_reason") or runtime.get("degraded_reason")
    return {
        "runtime_mode": runtime_mode,
        "provider": provider,
        "availability": runtime.get("availability", "not_checked"),
        "setup_doctor": {
            "source": "team setup",
            "status": "not_checked",
            "degraded_capability_reason": degraded_reason,
        },
        "provider_fallback": {
            key: value
            for key, value in runtime.items()
            if isinstance(key, str) and "fallback" in key and value
        },
        "degraded_capability_reason": degraded_reason,
        "records_only": True,
    }


def _tool_inventory_payload(project_root: Path | None = None) -> dict[str, object]:
    root = project_root or Path(".")
    return {
        "source": "control_plane_placeholder",
        "mcp": {
            "available": shutil.which("explore-cache-mcp") is not None,
            "required": False,
            "inventory_status": "placeholder",
        },
        "local_tools": [
            {"name": "pytest", "available": shutil.which("pytest") is not None},
            {"name": "git", "available": shutil.which("git") is not None},
            {"name": "explore-cache", "available": shutil.which("explore-cache") is not None},
        ],
        "project_root": str(root.resolve()) if root.exists() else str(root),
        "mutation_policy": "inventory only; tool execution remains below approved-plan/runtime gates",
    }


def _usage_cost_placeholder() -> dict[str, object]:
    return {
        "source": "placeholder",
        "measurement_status": "placeholder",
        "usage_available": False,
        "cost_available": False,
        "policy": "record provider usage/cost here when runtime supplies it",
    }


def build_execution_topology_snapshot(
    session: PlanSession,
    *,
    plans_root: Path | str = ".agent_orchestrator/plans",
    approvals_root: Path | str = ".agent_orchestrator/approvals",
    project_root: Path | str = ".",
) -> dict[str, object]:
    root = Path(project_root)
    plans_path = _resolve_root(root, plans_root)
    approvals_path = _resolve_root(root, approvals_root)
    approvals = build_approval_queue(root, plans_root=plans_path, approvals_root=approvals_path, sessions=[session])
    evidence_bundle = build_evidence_bundle(project_root)
    run_ledger = build_run_ledger(
        root,
        plans_root=plans_path,
        runs_root=root / ".agent_orchestrator" / "runs",
        jobs_root=root / ".agent_orchestrator" / "jobs",
        approvals_root=approvals_path,
        sessions=[session],
    )
    strategy = build_strategy_decision(session)
    strategy["approval_counts"] = approvals.get("counts", {})
    strategy["run_ledger_ref"] = {
        "format": run_ledger.get("format"),
        "entry_count": run_ledger.get("summary", {}).get("entry_count")
        if isinstance(run_ledger.get("summary"), dict)
        else 0,
    }
    graph = WorkGraphStore(root=plans_path).read_optional(session.id)
    nodes: list[dict[str, object]] = [
        _topology_node("state", "workspace-state", "Workspace state", session.status),
        _topology_node("context", "context-packet", "Context packet", "available"),
        _topology_node("strategy", "strategy-decision", str(strategy.get("next_goal")), "ready"),
        _topology_node("manager_slot", "manager-policy", "Manager policy slot", session.status),
    ]
    if graph is not None:
        for node in graph.nodes:
            if node.kind == "subtask":
                nodes.append(_topology_node("worker", node.id, node.title, node.status, owner_role=node.owner_role))
            elif node.kind in {"review_round", "review"}:
                nodes.append(_topology_node("review", node.id, node.title, node.status, owner_role=node.owner_role))
            elif node.kind == "gap":
                nodes.append(_topology_node("approval", node.id, node.title, node.status, owner_role=node.owner_role))
    for round_ in session.review_rounds:
        nodes.append(_topology_node("review", round_.id, round_.summary or round_.round_type, "completed", owner_role=round_.role))
    for item in approvals["items"]:
        if isinstance(item, dict) and item.get("session_id") == session.id:
            nodes.append(_topology_node("approval", str(item.get("id")), str(item.get("reason")), str(item.get("status"))))
    nodes.append(_topology_node("evidence", "evidence-bundle", "Evidence gates", str(evidence_bundle.get("status"))))
    nodes.append(_topology_node("memory", "memory-records", "Memory provenance", "available"))

    edges = [
        {"from": "workspace-state", "to": "context-packet"},
        {"from": "context-packet", "to": "strategy-decision"},
        {"from": "strategy-decision", "to": "manager-policy"},
    ]
    for node in nodes:
        node_id = str(node.get("id"))
        if node_id not in {"workspace-state", "context-packet", "strategy-decision", "manager-policy"}:
            edges.append({"from": "manager-policy", "to": node_id})
    execution_contract = (
        session.approved_plan.get("execution_contract", {})
        if isinstance(session.approved_plan, dict) and isinstance(session.approved_plan.get("execution_contract"), dict)
        else {}
    )
    payload = {
        "format": CONTROL_PLANE_FORMATS["topology_snapshot"],
        "session_id": session.id,
        "fixed_node_types": list(TOPOLOGY_NODE_TYPES),
        "blueprint": _topology_blueprint(session, nodes, edges, approvals, evidence_bundle),
        "nodes": nodes,
        "edges": edges,
        "lanes": _topology_lanes(nodes),
        "approval_points": _topology_points(nodes, "approval"),
        "evidence_points": _topology_points(nodes, "evidence"),
        "runtime_boundaries": _topology_runtime_boundaries(session),
        "strategy_decision": strategy,
        "execution_contract": execution_contract,
        "approval_queue": approvals,
        "run_ledger": run_ledger,
        "evidence_bundle": evidence_bundle,
        "read_only": True,
        "created_at": now_iso(),
    }
    index = WorkspaceIndexStore(root / ".agent_orchestrator" / "workspace")
    index.record_artifact("strategy_decision", strategy)
    index.record_artifact("topology_snapshot", payload)
    return payload


def build_run_ledger(
    project_root: Path | str = ".",
    *,
    plans_root: Path | str = ".agent_orchestrator/plans",
    runs_root: Path | str = ".agent_orchestrator/runs",
    jobs_root: Path | str = ".agent_orchestrator/jobs",
    approvals_root: Path | str = ".agent_orchestrator/approvals",
    sessions: list[PlanSession] | None = None,
) -> dict[str, object]:
    root = Path(project_root)
    plans_path = _resolve_root(root, plans_root)
    runs_path = _resolve_root(root, runs_root)
    jobs_path = _resolve_root(root, jobs_root)
    approvals_path = _resolve_root(root, approvals_root)
    loaded_sessions = sessions if sessions is not None else _read_plan_sessions(plans_path)
    approvals = build_approval_queue(root, plans_root=plans_path, approvals_root=approvals_path, sessions=loaded_sessions)
    approval_items = approvals.get("items", []) if isinstance(approvals.get("items"), list) else []
    entries: list[dict[str, object]] = []
    for session in loaded_sessions:
        entries.append(_run_ledger_plan_entry(session, approval_items))
    for run in _read_run_entries(runs_path):
        entries.append(_run_ledger_run_entry(run))
    for job in _read_job_entries(jobs_path):
        entries.append(_run_ledger_job_entry(job))
    status_counts: dict[str, int] = {}
    for entry in entries:
        status = str(entry.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    payload = {
        "format": CONTROL_PLANE_FORMATS["run_ledger"],
        "project_root": str(root.resolve()),
        "entries": entries,
        "summary": {
            "entry_count": len(entries),
            "status_counts": status_counts,
            "recovery_ready_count": status_counts.get("recovery_ready", 0),
            "awaiting_human_count": status_counts.get("awaiting_human", 0),
            "failed_count": status_counts.get("failed", 0),
            "provider_fallback_count": status_counts.get("provider_fallback", 0),
            "compliance_blocking_count": status_counts.get("compliance_blocking", 0),
        },
        "evidence_ref": "agent_orchestrator.evidence_bundle.v1",
        "created_at": now_iso(),
    }
    WorkspaceIndexStore(root / ".agent_orchestrator" / "workspace").record_artifact("run_ledger", payload)
    return payload


def build_recovery_timeline(
    project_root: Path | str = ".",
    *,
    plans_root: Path | str = ".agent_orchestrator/plans",
    runs_root: Path | str = ".agent_orchestrator/runs",
    jobs_root: Path | str = ".agent_orchestrator/jobs",
    approvals_root: Path | str = ".agent_orchestrator/approvals",
    sessions: list[PlanSession] | None = None,
    compliance: dict[str, object] | None = None,
) -> dict[str, object]:
    root = Path(project_root)
    plans_path = _resolve_root(root, plans_root)
    runs_path = _resolve_root(root, runs_root)
    jobs_path = _resolve_root(root, jobs_root)
    approvals_path = _resolve_root(root, approvals_root)
    loaded_sessions = sessions if sessions is not None else _read_plan_sessions(plans_path)
    approvals = build_approval_queue(root, plans_root=plans_path, approvals_root=approvals_path, sessions=loaded_sessions)
    run_ledger = build_run_ledger(
        root,
        plans_root=plans_path,
        runs_root=runs_path,
        jobs_root=jobs_path,
        approvals_root=approvals_path,
        sessions=loaded_sessions,
    )
    evidence_bundle = build_evidence_bundle(root, compliance=compliance)
    entries: list[dict[str, object]] = []
    approval_items = approvals.get("items", []) if isinstance(approvals.get("items"), list) else []
    for session in loaded_sessions:
        entries.extend(_recovery_timeline_session_entries(session, approval_items, evidence_bundle))
    for entry in run_ledger.get("entries", []) if isinstance(run_ledger.get("entries"), list) else []:
        if isinstance(entry, dict):
            timeline_entry = _recovery_timeline_ledger_entry(entry)
            if timeline_entry:
                entries.append(timeline_entry)
    status_counts: dict[str, int] = {}
    for entry in entries:
        status = str(entry.get("status") or "interrupted")
        status_counts[status] = status_counts.get(status, 0) + 1
    current = _current_recovery_status(entries)
    payload = {
        "format": CONTROL_PLANE_FORMATS["recovery_timeline"],
        "project_root": str(root.resolve()),
        "status_catalog": list(RECOVERY_TIMELINE_STATUSES),
        "entries": entries,
        "summary": {
            "entry_count": len(entries),
            "status_counts": status_counts,
            "current_status": current,
            "blocking_summary": _recovery_blocking_summary(entries),
            "resume_hint": _recovery_resume_hint(entries),
            "last_checkpoint": _recovery_last_checkpoint(entries),
        },
        "source_refs": {
            "run_ledger": _artifact_ref(run_ledger),
            "approval_queue": _artifact_ref(approvals),
            "evidence_bundle": _artifact_ref(evidence_bundle),
        },
        "read_only": True,
        "created_at": now_iso(),
    }
    WorkspaceIndexStore(root / ".agent_orchestrator" / "workspace").record_artifact("recovery_timeline", payload)
    return payload


def build_runtime_event_stream(
    project_root: Path | str = ".",
    *,
    plans_root: Path | str = ".agent_orchestrator/plans",
    runs_root: Path | str = ".agent_orchestrator/runs",
    jobs_root: Path | str = ".agent_orchestrator/jobs",
    approvals_root: Path | str = ".agent_orchestrator/approvals",
    sessions: list[PlanSession] | None = None,
) -> dict[str, object]:
    root = Path(project_root)
    plans_path = _resolve_root(root, plans_root)
    runs_path = _resolve_root(root, runs_root)
    jobs_path = _resolve_root(root, jobs_root)
    approvals_path = _resolve_root(root, approvals_root)
    loaded_sessions = sessions if sessions is not None else _read_plan_sessions(plans_path)
    events: list[dict[str, object]] = []
    for session in loaded_sessions:
        events.extend(_runtime_events_for_session(session))
    for run in _read_run_entries(runs_path):
        events.append(_runtime_event_for_run(run))
    session_snapshots = [_provider_session_snapshot_from_job(job) for job in _read_job_payloads(jobs_path)]
    for snapshot in session_snapshots:
        events.append(_runtime_event_for_session_snapshot(snapshot))
    approvals = build_approval_queue(root, plans_root=plans_path, approvals_root=approvals_path, sessions=loaded_sessions)
    pending_approvals = [
        item
        for item in approvals.get("items", [])
        if isinstance(item, dict) and item.get("status") == "pending"
    ]
    if pending_approvals:
        events.append(
            {
                "id": _stable_id("runtime-event", "approval", str(len(pending_approvals))),
                "kind": "approval_gate",
                "runtime_mode": "control_plane",
                "intent": "record human approval requirement",
                "tool_intent": "team approvals list",
                "result_status": "awaiting_human",
                "artifact_refs": ["agent_orchestrator.approval_queue.v1"],
                "usage_cost": _usage_cost_placeholder(),
                "records_only": True,
                "created_at": now_iso(),
            }
        )
    status_counts: dict[str, int] = {}
    for event in events:
        status = str(event.get("result_status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    payload = {
        "format": CONTROL_PLANE_FORMATS["runtime_event_stream"],
        "project_root": str(root.resolve()),
        "events": events,
        "summary": {
            "event_count": len(events),
            "status_counts": status_counts,
            "failed_count": status_counts.get("failed", 0),
            "degraded_count": status_counts.get("provider_degraded", 0),
            "fallback_count": status_counts.get("provider_fallback", 0),
            "live_session_count": sum(
                1
                for snapshot in session_snapshots
                if isinstance(snapshot.get("liveness"), dict)
                and snapshot["liveness"].get("state") == "running"
            ),
            "missing_session_count": sum(
                1
                for snapshot in session_snapshots
                if isinstance(snapshot.get("liveness"), dict)
                and snapshot["liveness"].get("state") == "missing"
            ),
        },
        "provider_session_snapshots": session_snapshots,
        "operation_receipts": [
            receipt
            for snapshot in session_snapshots
            for receipt in snapshot.get("operation_receipts", [])
            if isinstance(receipt, dict)
        ],
        "mutation_policy": "records runtime intent/result/fallback only; execution remains gated by approved plans",
        "usage_cost": _usage_cost_placeholder(),
        "read_only": True,
        "created_at": now_iso(),
    }
    WorkspaceIndexStore(root / ".agent_orchestrator" / "workspace").record_artifact("runtime_event_stream", payload)
    return payload


def build_provider_session_snapshot(
    job_id: str,
    project_root: Path | str = ".",
    *,
    jobs_root: Path | str = ".agent_orchestrator/jobs",
) -> dict[str, object]:
    root = Path(project_root)
    jobs_path = _resolve_root(root, jobs_root)
    job_path = jobs_path / f"{job_id}.json"
    if not job_path.exists():
        payload = {
            "format": CONTROL_PLANE_FORMATS["provider_session_snapshot"],
            "job_id": job_id,
            "status": "missing",
            "liveness": {
                "state": "missing",
                "detail": f"Job {job_id} is not available.",
                "checked_at": now_iso(),
            },
            "operation_support": {
                "send": "session_missing",
                "cancel": "session_missing",
                "attach": "unavailable",
                "continue": "unavailable",
            },
            "recommended_recovery_command": "python -m agent_orchestrator.cli team workspace-status",
            "read_only": True,
            "created_at": now_iso(),
        }
        WorkspaceIndexStore(root / ".agent_orchestrator" / "workspace").record_artifact("provider_session_snapshot", payload)
        return payload
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except Exception:
        job = {"id": job_id, "status": "failed", "error": "job payload could not be parsed"}
    payload = _provider_session_snapshot_from_job(job if isinstance(job, dict) else {"id": job_id})
    WorkspaceIndexStore(root / ".agent_orchestrator" / "workspace").record_artifact("provider_session_snapshot", payload)
    return payload


def build_recovery_recommendation(
    session: PlanSession,
    *,
    recovery_timeline: dict[str, object] | None = None,
    runtime_event_stream: dict[str, object] | None = None,
    approval_queue: dict[str, object] | None = None,
    evidence_bundle: dict[str, object] | None = None,
) -> dict[str, object]:
    payload = session.to_dict()
    summary = payload.get("status_summary", {}) if isinstance(payload.get("status_summary"), dict) else {}
    timeline_summary = (
        recovery_timeline.get("summary", {})
        if isinstance(recovery_timeline, dict) and isinstance(recovery_timeline.get("summary"), dict)
        else summary.get("recovery_timeline", {})
        if isinstance(summary.get("recovery_timeline"), dict)
        else {}
    )
    blocking_reasons = list(summary.get("blocking_reasons", [])) if isinstance(summary.get("blocking_reasons"), list) else []
    current_blocking_reason = (
        blocking_reasons[0]
        if blocking_reasons
        else str(summary.get("primary_reason") or summary.get("resume_reason") or "inspect current control-plane state")
    )
    recommended_commands = (
        [str(command) for command in summary.get("recommended_commands", [])]
        if isinstance(summary.get("recommended_commands"), list)
        else []
    )
    safest_command = recommended_commands[0] if recommended_commands else _recovery_default_command(session, summary)
    compliance = session.compliance if isinstance(session.compliance, dict) else {}
    approval_state = summary.get("approval_state", {}) if isinstance(summary.get("approval_state"), dict) else {}
    current_status = str(timeline_summary.get("current_status") or summary.get("primary_action") or session.status)
    required = _recovery_required_approval_or_evidence(
        current_status,
        compliance=compliance,
        approval_state=approval_state,
        evidence_bundle=evidence_bundle,
    )
    artifact_refs = [f"plans/{session.id}/session.json", "agent_orchestrator.recovery_timeline.v1"]
    if runtime_event_stream is not None:
        artifact_refs.append("agent_orchestrator.runtime_event_stream.v1")
    if approval_queue is not None:
        artifact_refs.append("agent_orchestrator.approval_queue.v1")
    if evidence_bundle is not None:
        artifact_refs.append("agent_orchestrator.evidence_bundle.v1")
    compliance_first = bool(compliance.get("blocking")) or current_status == "compliance_blocked"
    human_required = bool(approval_state.get("human_required")) or current_status in {"awaiting_human", "approval_blocked"}
    may_resume = (
        not compliance_first
        and not human_required
        and current_status
        not in {"runtime_failed", "provider_degraded", "evidence_blocked", "interrupted"}
    )
    return {
        "format": "agent_orchestrator.recovery_recommendation.v1",
        "session_id": session.id,
        "current_status": current_status,
        "current_blocking_reason": current_blocking_reason,
        "safest_next_operator_command": safest_command,
        "required_approval_or_evidence": required,
        "recoverable_artifact_refs": artifact_refs,
        "may_resume_execution": may_resume,
        "human_decision_required": human_required,
        "compliance_must_be_fixed_first": compliance_first,
        "read_only": True,
        "mutation_policy": "recommendation only; execution remains gated by approved-plan runtime",
        "created_at": now_iso(),
    }


def build_approval_queue(
    project_root: Path | str = ".",
    *,
    plans_root: Path | str = ".agent_orchestrator/plans",
    approvals_root: Path | str = ".agent_orchestrator/approvals",
    sessions: list[PlanSession] | None = None,
) -> dict[str, object]:
    root = Path(project_root)
    plans_path = _resolve_root(root, plans_root)
    approvals_path = _resolve_root(root, approvals_root)
    loaded_sessions = sessions if sessions is not None else _read_plan_sessions(plans_path)
    generated = _generated_approval_items(loaded_sessions)
    store = ApprovalStore(approvals_path)
    latest = store.latest_by_id()
    merged: dict[str, ApprovalItem] = {item.id: item for item in generated}
    merged.update(latest)
    items = sorted(merged.values(), key=lambda item: (item.status != "pending", item.created_at, item.id))
    counts = {
        "pending": sum(1 for item in items if item.status == "pending"),
        "approved": sum(1 for item in items if item.status == "approved"),
        "rejected": sum(1 for item in items if item.status == "rejected"),
        "resolved": sum(1 for item in items if item.status == "resolved"),
        "total": len(items),
    }
    reason_code_distribution: dict[str, int] = {}
    for item in items:
        reason_code_distribution[item.reason_code] = reason_code_distribution.get(item.reason_code, 0) + 1
    blocking_count = sum(1 for item in items if item.status == "pending" and item.reason_code in {"blocked_session", "compliance_blocking"})
    recommended_command = "team approvals list"
    first_pending = next((item for item in items if item.status == "pending"), None)
    if first_pending is not None:
        recommended_command = f"team approvals resolve {first_pending.id} --status resolved --reason \"<decision>\""
    return {
        "format": CONTROL_PLANE_FORMATS["approval_queue"],
        "project_root": str(Path(project_root).resolve()),
        "items": [item.to_dict() for item in items],
        "counts": counts,
        "inbox_summary": {
            "pending_count": counts["pending"],
            "resolved_count": counts["resolved"],
            "blocking_count": blocking_count,
            "reason_code_distribution": reason_code_distribution,
            "recommended_next_command": recommended_command,
        },
        "mutation_policy": "resolve only records the human decision; it does not execute gated work",
    }


def resolve_approval_item(
    approval_id: str,
    *,
    status: ApprovalStatus,
    reason: str,
    project_root: Path | str = ".",
    plans_root: Path | str = ".agent_orchestrator/plans",
    approvals_root: Path | str = ".agent_orchestrator/approvals",
    actor: str = "human",
) -> dict[str, object]:
    root = Path(project_root)
    plans_path = _resolve_root(root, plans_root)
    approvals_path = _resolve_root(root, approvals_root)
    queue = build_approval_queue(root, plans_root=plans_path, approvals_root=approvals_path)
    by_id = {
        str(item.get("id")): ApprovalItem.from_dict(item)
        for item in queue.get("items", [])
        if isinstance(item, dict)
    }
    item = by_id.get(
        approval_id,
        ApprovalItem(
            id=approval_id,
            status="pending",
            reason_code="awaiting_human_decision",
            reason="Manual approval item resolved without a generated queue entry.",
            scope="manual",
            scope_id=approval_id,
            recommended_action="inspect",
        ),
    )
    resolved = item.resolved(status=status, reason=reason, actor=actor)
    ApprovalStore(approvals_path).append(resolved)
    EventStore(root / ".agent_orchestrator" / "events").append(
        type="approval.resolved",
        scope=resolved.scope,
        scope_id=resolved.scope_id,
        message=f"Approval {resolved.id} resolved as {status}.",
        payload=resolved.to_dict(),
    )
    MemoryStore(root / ".agent_orchestrator" / "memory").append(
        namespace="approval",
        session_id=resolved.session_id or "",
        record_type="approval_resolution",
        role="approval_gate",
        provider="control_plane",
        summary=f"{resolved.id}: {status}",
        payload=resolved.to_dict(),
        provenance={
            "source_artifacts": [resolved.id],
            "base_commit": _git_head(root),
        },
        freshness="fresh",
        confidence=1.0,
        external_cache_status=_external_cache_status(root),
    )
    return {
        "format": CONTROL_PLANE_FORMATS["approval_queue"],
        "resolved_item": resolved.to_dict(),
        "mutation_policy": "recorded approval decision only; execution gates remain authoritative",
    }


def build_evidence_bundle(project_root: Path | str = ".", compliance: dict[str, object] | None = None) -> dict[str, object]:
    root = Path(project_root)
    evidence_state = _evidence_state(root)
    compliance_payload = compliance or {"blocking": False, "blocking_reasons": [], "warnings": []}
    gate_evidence = _gate_evidence_summary(root, compliance_payload, evidence_state)
    failed = [gate for gate in gate_evidence["gates"] if gate.get("status") in {"failed", "missing"}]
    payload = {
        "format": CONTROL_PLANE_FORMATS["evidence_bundle"],
        "status": "blocked" if any(gate.get("status") == "failed" for gate in failed) else "ready_with_gaps" if failed else "ready",
        "gate_evidence": gate_evidence,
        "evidence_state": evidence_state,
        "recovery_refs": _evidence_recovery_refs(root),
        "runtime_fidelity": _evidence_runtime_fidelity(root),
        "compliance": {
            "blocking": bool(compliance_payload.get("blocking", False)),
            "blocking_reasons": list(compliance_payload.get("blocking_reasons", []))
            if isinstance(compliance_payload.get("blocking_reasons", []), list)
            else [],
            "warnings": list(compliance_payload.get("warnings", []))
            if isinstance(compliance_payload.get("warnings", []), list)
                else [],
        },
        "runtime_health": _runtime_health_payload(),
        "tool_inventory": _tool_inventory_payload(project_root=root),
        "usage_cost": _usage_cost_placeholder(),
        "memory_recommendation": _evidence_memory_recommendation(root, gate_evidence, compliance_payload),
        "created_at": now_iso(),
    }
    WorkspaceIndexStore(root / ".agent_orchestrator" / "workspace").record_artifact("evidence_bundle", payload)
    return payload


def _workspace_index_payload(
    snapshot: WorkspaceStateSnapshot,
    *,
    artifacts: dict[str, object],
) -> dict[str, object]:
    payload = {
        "format": CONTROL_PLANE_FORMATS["workspace_index"],
        "workspace_state": snapshot.to_dict(),
        "artifacts": artifacts,
        "updated_at": now_iso(),
    }
    payload.update(_workspace_index_optional_sections(snapshot, artifacts=artifacts))
    return payload


def _workspace_index_optional_sections(
    snapshot: WorkspaceStateSnapshot,
    *,
    artifacts: dict[str, object],
) -> dict[str, object]:
    plans = list(snapshot.plans)
    active_plans = [plan for plan in plans if str(plan.get("status")) not in {"completed", "cancelled", "failed"}]
    approvals = list(snapshot.approvals)
    open_approvals = [item for item in approvals if str(item.get("status")) == "pending"]
    recent_runs = list(snapshot.runs)[:10]
    memory_recent = (
        list(snapshot.memory_digest.get("recent", []))
        if isinstance(snapshot.memory_digest.get("recent", []), list)
        else []
    )
    provider_health = snapshot.provider_health or {
        "runtime_mode": "unknown",
        "available": None,
        "status": "not_checked",
        "degraded_reason": None,
    }
    return {
        "program": {
            "kind": "workspace_program",
            "name": Path(snapshot.project_root).name or "workspace",
            "project_root": snapshot.project_root,
            "active_plan_count": len(active_plans),
            "open_approval_count": len(open_approvals),
            "recent_run_count": len(recent_runs),
        },
        "active_artifacts": {
            "workspace_state": artifacts.get("workspace_state"),
            "context_packet": artifacts.get("context_packet"),
            "strategy_decision": artifacts.get("strategy_decision"),
            "topology_snapshot": artifacts.get("topology_snapshot"),
            "approval_queue": artifacts.get("approval_queue"),
            "run_ledger": artifacts.get("run_ledger"),
            "recovery_timeline": artifacts.get("recovery_timeline"),
            "runtime_event_stream": artifacts.get("runtime_event_stream"),
            "provider_session_snapshot": artifacts.get("provider_session_snapshot"),
            "recovery_recommendation": artifacts.get("recovery_recommendation"),
            "evidence_bundle": artifacts.get("evidence_bundle"),
        },
        "recent_artifacts": [
            {"name": name, "ref": ref}
            for name, ref in sorted(artifacts.items())
            if isinstance(name, str)
        ][-10:],
        "open_approvals": open_approvals,
        "recent_runs": recent_runs,
        "memory_candidates": [
            {
                "record_type": record.get("record_type"),
                "namespace": record.get("namespace"),
                "summary": record.get("summary"),
                "provenance": record.get("provenance", {}),
                "source": "memory_digest",
            }
            for record in memory_recent
            if isinstance(record, dict)
        ],
        "provider_runtime_health": provider_health,
    }


def _workspace_recovery_dashboard(
    *,
    recovery_timeline: dict[str, object],
    runtime_events: dict[str, object],
    recovery_recommendation: dict[str, object] | None,
) -> dict[str, object]:
    timeline_summary = recovery_timeline.get("summary", {}) if isinstance(recovery_timeline.get("summary"), dict) else {}
    runtime_summary = runtime_events.get("summary", {}) if isinstance(runtime_events.get("summary"), dict) else {}
    blocking_summary = (
        timeline_summary.get("blocking_summary", {})
        if isinstance(timeline_summary.get("blocking_summary"), dict)
        else {}
    )
    return {
        "recovery_timeline": {
            "format": recovery_timeline.get("format"),
            "summary": timeline_summary,
            "read_only": True,
        },
        "runtime_events": {
            "format": runtime_events.get("format"),
            "summary": runtime_summary,
            "read_only": True,
        },
        "runtime_fidelity": {
            "provider_session_snapshot_count": len(runtime_events.get("provider_session_snapshots", []))
            if isinstance(runtime_events.get("provider_session_snapshots"), list)
            else 0,
            "operation_receipt_count": len(runtime_events.get("operation_receipts", []))
            if isinstance(runtime_events.get("operation_receipts"), list)
            else 0,
            "live_session_count": runtime_summary.get("live_session_count", 0),
            "missing_session_count": runtime_summary.get("missing_session_count", 0),
            "read_only": True,
        },
        "recovery_recommendation": recovery_recommendation,
        "blocking_summary": blocking_summary,
        "resume_hint": timeline_summary.get("resume_hint"),
        "last_checkpoint": timeline_summary.get("last_checkpoint"),
    }


def _read_plan_sessions(plans_root: Path) -> list[PlanSession]:
    sessions: list[PlanSession] = []
    for path in sorted(plans_root.glob("*/session.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                sessions.append(PlanSession.from_dict(payload))
        except Exception:
            continue
    return sessions


def _session_index_entry(session: PlanSession) -> dict[str, object]:
    summary = session.to_dict().get("status_summary", {})
    status = summary if isinstance(summary, dict) else {}
    return {
        "id": session.id,
        "status": session.status,
        "phase": session.resume.current_phase,
        "pending_role": session.resume.pending_role,
        "goal": session.structured_brief.goal or session.requirement,
        "selected_topology": session.decision_verdict.selected_topology if session.decision_verdict else None,
        "linked_execution_run_id": session.resume.linked_execution_run_id,
        "primary_action": status.get("primary_action"),
        "blocking_reasons": status.get("blocking_reasons", []),
    }


def _run_ledger_plan_entry(session: PlanSession, approval_items: list[object]) -> dict[str, object]:
    payload = session.to_dict()
    summary = payload.get("status_summary", {}) if isinstance(payload.get("status_summary"), dict) else {}
    approval_count = sum(
        1
        for item in approval_items
        if isinstance(item, dict) and item.get("session_id") == session.id and item.get("status") == "pending"
    )
    status = _ledger_status_for_session(session, summary, approval_count)
    return {
        "id": f"plan:{session.id}",
        "kind": "plan_session",
        "status": status,
        "session_id": session.id,
        "phase": session.resume.current_phase,
        "primary_action": summary.get("primary_action"),
        "resume_action": summary.get("resume_action"),
        "resume_reason": summary.get("resume_reason"),
        "recovery_actions": list(summary.get("recovery_actions", [])) if isinstance(summary.get("recovery_actions"), list) else [],
        "approval_count": approval_count,
        "provider_fallback": _session_has_provider_fallback(session),
        "evidence_refs": [f"plans/{session.id}/session.json"],
    }


def _run_ledger_run_entry(run: dict[str, object]) -> dict[str, object]:
    accepted = run.get("accepted")
    status = "completed" if accepted is True else "failed" if accepted is False else "interrupted"
    return {
        "id": f"run:{run.get('id')}",
        "kind": "execution_run",
        "status": status,
        "run_id": run.get("id"),
        "final_mode": run.get("final_mode"),
        "accepted": accepted,
        "evidence_refs": [str(run.get("path"))] if run.get("path") else [],
    }


def _run_ledger_job_entry(job: dict[str, object]) -> dict[str, object]:
    raw_status = str(job.get("status") or "unknown")
    status = "failed" if raw_status == "failed" else "completed" if raw_status == "completed" else "interrupted"
    return {
        "id": f"job:{job.get('id')}",
        "kind": "delegated_job",
        "status": status,
        "job_id": job.get("id"),
        "provider": job.get("provider"),
        "runtime_mode": job.get("runtime_mode"),
        "phase": job.get("phase"),
        "summary": job.get("summary"),
        "evidence_refs": [f"jobs/{job.get('id')}.json"],
    }


def _recovery_timeline_session_entries(
    session: PlanSession,
    approval_items: list[object],
    evidence_bundle: dict[str, object],
) -> list[dict[str, object]]:
    payload = session.to_dict()
    summary = payload.get("status_summary", {}) if isinstance(payload.get("status_summary"), dict) else {}
    entries = [
        {
            "id": f"timeline:{session.id}:started",
            "status": "started",
            "kind": "plan_session",
            "session_id": session.id,
            "message": "Plan session exists in the control plane.",
            "artifact_refs": [f"plans/{session.id}/session.json"],
            "created_at": now_iso(),
        },
        {
            "id": f"timeline:{session.id}:checkpointed",
            "status": "checkpointed",
            "kind": "plan_session",
            "session_id": session.id,
            "message": f"Checkpoint at phase {session.resume.current_phase}.",
            "artifact_refs": [f"plans/{session.id}/session.json"],
            "created_at": now_iso(),
            "checkpoint": {
                "phase": session.resume.current_phase,
                "pending_role": session.resume.pending_role,
                "linked_execution_run_id": session.resume.linked_execution_run_id,
            },
        },
    ]
    current_status = _recovery_status_for_session(session, summary, approval_items, evidence_bundle)
    entries.append(
        {
            "id": f"timeline:{session.id}:current",
            "status": current_status,
            "kind": "recovery_state",
            "session_id": session.id,
            "message": _recovery_message_for_status(current_status, summary),
            "resume_action": summary.get("resume_action") or summary.get("primary_action"),
            "resume_reason": summary.get("resume_reason") or summary.get("primary_reason"),
            "blocking_reasons": list(summary.get("blocking_reasons", []))
            if isinstance(summary.get("blocking_reasons"), list)
            else [],
            "artifact_refs": [f"plans/{session.id}/session.json", "agent_orchestrator.run_ledger.v1"],
            "created_at": now_iso(),
        }
    )
    return entries


def _recovery_status_for_session(
    session: PlanSession,
    summary: dict[str, object],
    approval_items: list[object],
    evidence_bundle: dict[str, object],
) -> str:
    compliance = session.compliance if isinstance(session.compliance, dict) else {}
    if compliance.get("blocking"):
        return "compliance_blocked"
    if session.status in {"awaiting_human", "awaiting_human_confirmation"}:
        return "awaiting_human"
    if any(
        isinstance(item, dict) and item.get("session_id") == session.id and item.get("status") == "pending"
        for item in approval_items
    ):
        return "approval_blocked"
    if evidence_bundle.get("status") == "blocked":
        return "evidence_blocked"
    if _session_has_provider_fallback(session):
        return "provider_degraded"
    delegated_jobs = summary.get("delegated_jobs", []) if isinstance(summary.get("delegated_jobs"), list) else []
    if any(isinstance(job, dict) and job.get("status") == "failed" for job in delegated_jobs):
        return "runtime_failed"
    if session.status in {"accepted", "completed"}:
        return "completed"
    if summary.get("resume_action") or summary.get("recovery_actions"):
        return "recovery_ready"
    return "interrupted"


def _recovery_timeline_ledger_entry(entry: dict[str, object]) -> dict[str, object] | None:
    status_map = {
        "completed": "completed",
        "failed": "runtime_failed",
        "interrupted": "interrupted",
        "awaiting_human": "awaiting_human",
        "compliance_blocking": "compliance_blocked",
        "provider_fallback": "provider_degraded",
        "recovery_ready": "recovery_ready",
    }
    status = status_map.get(str(entry.get("status") or ""))
    if not status:
        return None
    entry_id = str(entry.get("id") or _stable_id("ledger", str(entry)))
    return {
        "id": f"timeline:{entry_id}",
        "status": status,
        "kind": str(entry.get("kind") or "run_ledger_entry"),
        "session_id": entry.get("session_id"),
        "run_id": entry.get("run_id"),
        "job_id": entry.get("job_id"),
        "message": f"Run ledger entry {entry_id} reports {status}.",
        "resume_action": entry.get("resume_action") or entry.get("primary_action"),
        "resume_reason": entry.get("resume_reason"),
        "artifact_refs": list(entry.get("evidence_refs", [])) if isinstance(entry.get("evidence_refs"), list) else [],
        "created_at": now_iso(),
    }


def _current_recovery_status(entries: list[dict[str, object]]) -> str:
    priority = [
        "compliance_blocked",
        "approval_blocked",
        "awaiting_human",
        "runtime_failed",
        "provider_degraded",
        "evidence_blocked",
        "recovery_ready",
        "interrupted",
        "checkpointed",
        "completed",
        "started",
    ]
    statuses = {str(entry.get("status")) for entry in entries}
    for status in priority:
        if status in statuses:
            return status
    return "interrupted"


def _recovery_blocking_summary(entries: list[dict[str, object]]) -> dict[str, object]:
    blocking_statuses = {
        "awaiting_human",
        "approval_blocked",
        "evidence_blocked",
        "compliance_blocked",
        "provider_degraded",
        "runtime_failed",
        "interrupted",
    }
    blockers = [entry for entry in entries if str(entry.get("status")) in blocking_statuses]
    return {
        "blocking": bool(blockers),
        "count": len(blockers),
        "statuses": sorted({str(entry.get("status")) for entry in blockers}),
        "reasons": [
            str(reason)
            for entry in blockers
            for reason in (
                entry.get("blocking_reasons", []) if isinstance(entry.get("blocking_reasons"), list) else []
            )
        ],
    }


def _recovery_resume_hint(entries: list[dict[str, object]]) -> str:
    for entry in reversed(entries):
        action = entry.get("resume_action")
        if action:
            return str(action)
    current = _current_recovery_status(entries)
    defaults = {
        "compliance_blocked": "inspect_compliance",
        "approval_blocked": "resolve_approval",
        "awaiting_human": "human_decision",
        "runtime_failed": "inspect_blockers",
        "provider_degraded": "inspect_runtime_health",
        "evidence_blocked": "inspect_evidence",
        "recovery_ready": "team next",
        "completed": "inspect_execution",
    }
    return defaults.get(current, "team summary")


def _recovery_last_checkpoint(entries: list[dict[str, object]]) -> dict[str, object] | None:
    checkpoints = [entry for entry in entries if entry.get("status") == "checkpointed"]
    return checkpoints[-1] if checkpoints else None


def _recovery_message_for_status(status: str, summary: dict[str, object]) -> str:
    reason = summary.get("primary_reason") or summary.get("resume_reason")
    if reason:
        return str(reason)
    messages = {
        "awaiting_human": "Human decision is required before work can continue.",
        "approval_blocked": "Pending approval blocks the next execution step.",
        "evidence_blocked": "Evidence gates are blocking recovery.",
        "compliance_blocked": "Compliance must be fixed before resuming.",
        "provider_degraded": "Provider/runtime fallback or degraded capability is active.",
        "runtime_failed": "Runtime or delegated job failure requires inspection.",
        "interrupted": "Work is interrupted and needs operator inspection.",
        "recovery_ready": "Recovery path is available.",
        "completed": "Work is completed.",
    }
    return messages.get(status, "Recovery timeline checkpoint recorded.")


def _runtime_events_for_session(session: PlanSession) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    provider_runtime = (
        session.decision_verdict.selected_provider_runtime
        if session.decision_verdict and isinstance(session.decision_verdict.selected_provider_runtime, dict)
        else {}
    )
    runtime_mode = str(provider_runtime.get("runtime_mode") or provider_runtime.get("mode") or "control_plane")
    fallback_reason = provider_runtime.get("fallback_reason") or provider_runtime.get("recovery_provider_fallback_reason")
    degraded_reason = provider_runtime.get("degraded_reason") or provider_runtime.get("fallback_detail") or fallback_reason
    events.append(
        {
            "id": _stable_id("runtime-event", "session", session.id),
            "kind": "plan_session",
            "session_id": session.id,
            "runtime_mode": runtime_mode,
            "provider": provider_runtime.get("provider") or provider_runtime.get("selected_provider"),
            "intent": "govern plan session through approved-plan gate",
            "tool_intent": "team summary",
            "result_status": session.status,
            "fallback_reason": fallback_reason,
            "degraded_capability_reason": degraded_reason,
            "artifact_refs": [f"plans/{session.id}/session.json"],
            "usage_cost": _usage_cost_placeholder(),
            "records_only": True,
            "created_at": now_iso(),
        }
    )
    if degraded_reason or _session_has_provider_fallback(session):
        events.append(
            {
                "id": _stable_id("runtime-event", "provider-degraded", session.id, str(degraded_reason)),
                "kind": "provider_runtime_health",
                "session_id": session.id,
                "runtime_mode": runtime_mode,
                "provider": provider_runtime.get("provider") or provider_runtime.get("selected_provider"),
                "intent": "record provider/runtime fallback or degraded capability",
                "tool_intent": "team setup",
                "result_status": "provider_degraded",
                "fallback_reason": fallback_reason,
                "degraded_capability_reason": degraded_reason,
                "artifact_refs": [f"plans/{session.id}/session.json", "agent_orchestrator.strategy_decision.v1"],
                "usage_cost": _usage_cost_placeholder(),
                "records_only": True,
                "created_at": now_iso(),
            }
        )
    return events


def _runtime_event_for_run(run: dict[str, object]) -> dict[str, object]:
    accepted = run.get("accepted")
    status = "completed" if accepted is True else "failed" if accepted is False else "interrupted"
    return {
        "id": _stable_id("runtime-event", "run", str(run.get("id"))),
        "kind": "execution_run",
        "run_id": run.get("id"),
        "runtime_mode": run.get("final_mode") or run.get("initial_mode") or "unknown",
        "intent": "execute approved plan",
        "tool_intent": "team execute",
        "result_status": status,
        "failure_reason": "execution rejected or failed" if accepted is False else None,
        "artifact_refs": [str(run.get("path"))] if run.get("path") else [],
        "usage_cost": _usage_cost_placeholder(),
        "records_only": True,
        "created_at": now_iso(),
    }


def _provider_session_snapshot_from_job(job: dict[str, object]) -> dict[str, object]:
    status = str(job.get("status") or "unknown")
    metadata = job.get("metadata", {}) if isinstance(job.get("metadata"), dict) else {}
    parsed = job.get("parsed_payload", {}) if isinstance(job.get("parsed_payload"), dict) else {}
    operation = parsed.get("operation") if isinstance(parsed.get("operation"), dict) else None
    provider_session_ref = parsed.get("provider_session_ref") if isinstance(parsed.get("provider_session_ref"), dict) else None
    receipts = parsed.get("runtime_operation_receipts", []) if isinstance(parsed.get("runtime_operation_receipts"), list) else []
    if operation and not any(isinstance(item, dict) and item.get("id") == operation.get("id") for item in receipts):
        receipts = [*receipts, operation]
    terminal = status in {"completed", "failed", "cancelled"}
    pid = job.get("pid")
    if terminal:
        liveness_state = "terminal"
    elif pid:
        liveness_state = "running"
    elif job.get("session_id"):
        liveness_state = "unknown"
    else:
        liveness_state = "missing"
    runtime_mode = metadata.get("runtime_mode", {}) if isinstance(metadata.get("runtime_mode"), dict) else {}
    degraded_reason = (
        job.get("error")
        if status == "failed"
        else _metadata_value(job, "degraded_capability_reason")
        or _metadata_value(job, "fallback_reason")
    )
    support = _runtime_operation_support(job, liveness_state=liveness_state)
    measurement = _runtime_measurement_from_job(job)
    return {
        "format": CONTROL_PLANE_FORMATS["provider_session_snapshot"],
        "job_id": job.get("id"),
        "task_id": job.get("task_id"),
        "provider": job.get("provider"),
        "kind": job.get("kind"),
        "status": status,
        "phase": job.get("phase"),
        "runtime_mode": job.get("runtime_mode") or runtime_mode.get("mode") or "unknown",
        "model": job.get("model"),
        "session_id": job.get("session_id"),
        "thread_id": job.get("thread_id"),
        "provider_session_ref": provider_session_ref,
        "pid": pid,
        "command": list(job.get("command", [])) if isinstance(job.get("command"), list) else [],
        "home_isolation": {
            "runtime_home": runtime_mode.get("runtime_home"),
            "config_source": runtime_mode.get("config_source"),
            "inherits_user_config": runtime_mode.get("inherits_user_config"),
            "sandbox": runtime_mode.get("sandbox") or job.get("sandbox"),
        },
        "liveness": {
            "state": liveness_state,
            "terminal": terminal,
            "last_seen_at": job.get("updated_at") or job.get("completed_at") or job.get("started_at"),
            "degraded_reason": degraded_reason,
            "checked_at": now_iso(),
        },
        "runtime_measurement": measurement,
        "operation_support": support,
        "operation_receipts": [_runtime_operation_receipt(item, job) for item in receipts if isinstance(item, dict)][-10:],
        "last_operation_receipt": _runtime_operation_receipt(operation, job) if isinstance(operation, dict) else None,
        "recommended_recovery_command": _runtime_recovery_command(job, liveness_state=liveness_state),
        "artifact_refs": [f"jobs/{job.get('id')}.json"],
        "read_only": True,
        "created_at": now_iso(),
    }


def _runtime_event_for_session_snapshot(snapshot: dict[str, object]) -> dict[str, object]:
    status = str(snapshot.get("status") or "unknown")
    liveness = snapshot.get("liveness", {}) if isinstance(snapshot.get("liveness"), dict) else {}
    support = snapshot.get("operation_support", {}) if isinstance(snapshot.get("operation_support"), dict) else {}
    measurement = snapshot.get("runtime_measurement", {}) if isinstance(snapshot.get("runtime_measurement"), dict) else {}
    degraded = liveness.get("degraded_reason")
    return {
        "id": _stable_id("runtime-event", "job", str(snapshot.get("job_id"))),
        "kind": "delegated_job",
        "job_id": snapshot.get("job_id"),
        "session_id": snapshot.get("session_id"),
        "thread_id": snapshot.get("thread_id"),
        "runtime_mode": snapshot.get("runtime_mode") or "unknown",
        "provider": snapshot.get("provider"),
        "intent": snapshot.get("kind") or "delegated runtime job",
        "tool_intent": "job runtime",
        "result_status": "failed" if status == "failed" else "completed" if status == "completed" else status,
        "failure_reason": degraded if status == "failed" else None,
        "fallback_reason": degraded if "fallback" in str(degraded or "") else None,
        "degraded_capability_reason": degraded,
        "session_liveness": liveness,
        "operation_support": support,
        "runtime_measurement": measurement,
        "operation_receipts": snapshot.get("operation_receipts", []),
        "attachable": support.get("attach") == "available",
        "continuation_supported": support.get("continue") == "available",
        "recovery_safe_next_command": snapshot.get("recommended_recovery_command"),
        "artifact_refs": snapshot.get("artifact_refs", []),
        "usage_cost": measurement.get("usage_cost") if isinstance(measurement.get("usage_cost"), dict) else _usage_cost_placeholder(),
        "records_only": True,
        "created_at": liveness.get("last_seen_at") or now_iso(),
    }


def _runtime_measurement_from_job(job: dict[str, object]) -> dict[str, object]:
    existing = job.get("runtime_measurement")
    if isinstance(existing, dict):
        return existing
    metadata = job.get("metadata", {}) if isinstance(job.get("metadata"), dict) else {}
    parsed = job.get("parsed_payload", {}) if isinstance(job.get("parsed_payload"), dict) else {}
    return runtime_measurement_payload(
        provider=str(job.get("provider") or "unknown"),
        runtime_mode=str(job.get("runtime_mode") or "unknown"),
        status=str(job.get("status") or "unknown"),
        started_at=job.get("started_at") if isinstance(job.get("started_at"), str) else None,
        completed_at=job.get("completed_at") if isinstance(job.get("completed_at"), str) else None,
        exit_code=job.get("exit_code") if isinstance(job.get("exit_code"), int) else None,
        error=job.get("error") if isinstance(job.get("error"), str) else None,
        metadata=metadata,
        parsed_payload=parsed,
    )


def _runtime_operation_receipt(operation: dict[str, object] | None, job: dict[str, object]) -> dict[str, object] | None:
    if not operation:
        return None
    return {
        "format": operation.get("format") or CONTROL_PLANE_FORMATS["runtime_operation_receipt"],
        "id": operation.get("id") or _stable_id("receipt", str(job.get("id")), str(operation.get("action")), str(operation.get("updated_at"))),
        "job_id": operation.get("job_id") or job.get("id"),
        "provider": operation.get("provider") or job.get("provider"),
        "runtime_mode": operation.get("runtime_mode") or job.get("runtime_mode"),
        "session_id": operation.get("session_id") or job.get("session_id"),
        "thread_id": operation.get("thread_id") or job.get("thread_id"),
        "action": operation.get("action"),
        "status": operation.get("status"),
        "reason": operation.get("reason") or operation.get("status"),
        "detail": operation.get("detail"),
        "terminal_state": bool(operation.get("terminal_state")) or str(job.get("status")) in {"completed", "failed", "cancelled"},
        "records_only": True,
        "updated_at": operation.get("updated_at") or job.get("updated_at"),
    }


def _runtime_operation_support(job: dict[str, object], *, liveness_state: str) -> dict[str, object]:
    status = str(job.get("status") or "unknown")
    attach_available = bool(_metadata_value(job, "attach_available"))
    if status in {"completed", "failed", "cancelled"}:
        send = cancel = "already_terminal"
        continuation = "unavailable"
    elif liveness_state == "missing":
        send = cancel = "session_missing"
        continuation = "unavailable"
    else:
        send = cancel = "available"
        continuation = "available"
    return {
        "send": send,
        "cancel": cancel,
        "attach": "available" if attach_available else "unavailable",
        "continue": continuation,
    }


def _runtime_recovery_command(job: dict[str, object], *, liveness_state: str) -> str:
    job_id = str(job.get("id") or "")
    status = str(job.get("status") or "")
    if status == "running" and liveness_state in {"running", "unknown"}:
        return f"python -m agent_orchestrator.cli status {job_id}"
    if status == "failed":
        return f"python -m agent_orchestrator.cli result {job_id}"
    if status == "cancelled":
        return f"python -m agent_orchestrator.cli result {job_id}"
    if liveness_state == "missing":
        return "python -m agent_orchestrator.cli team workspace-status"
    return f"python -m agent_orchestrator.cli result {job_id}" if job_id else "python -m agent_orchestrator.cli team workspace-status"


def _metadata_value(payload: dict[str, object], key: str) -> object | None:
    metadata = payload.get("metadata", {}) if isinstance(payload.get("metadata"), dict) else {}
    return metadata.get(key)


def _recovery_default_command(session: PlanSession, summary: dict[str, object]) -> str:
    action = str(summary.get("resume_action") or summary.get("primary_action") or "summary")
    commands = {
        "revise": f"python -m agent_orchestrator.cli team revise {session.id} --summary \"close required gaps\"",
        "approve": f"python -m agent_orchestrator.cli team approve {session.id}",
        "execute": f"python -m agent_orchestrator.cli team execute {session.id}",
        "human_decision": f"python -m agent_orchestrator.cli team summary {session.id}",
        "inspect_compliance": "python -m agent_orchestrator.cli team check-compliance",
        "inspect_blockers": f"python -m agent_orchestrator.cli team inspect-blockers {session.id}",
        "inspect_execution": f"python -m agent_orchestrator.cli team inspect-execution {session.id}",
        "retry_review": f"python -m agent_orchestrator.cli team retry-review {session.id}",
        "retry_adversarial_review": f"python -m agent_orchestrator.cli team retry-adversarial-review {session.id}",
    }
    return commands.get(action, f"python -m agent_orchestrator.cli team summary {session.id}")


def _recovery_required_approval_or_evidence(
    current_status: str,
    *,
    compliance: dict[str, object],
    approval_state: dict[str, object],
    evidence_bundle: dict[str, object] | None,
) -> dict[str, object]:
    evidence_status = evidence_bundle.get("status") if isinstance(evidence_bundle, dict) else None
    return {
        "approval_required": bool(approval_state.get("human_required"))
        or current_status in {"awaiting_human", "approval_blocked"},
        "evidence_required": current_status == "evidence_blocked" or evidence_status == "blocked",
        "compliance_required": bool(compliance.get("blocking")) or current_status == "compliance_blocked",
        "reason": current_status,
    }


def _ledger_status_for_session(session: PlanSession, summary: dict[str, object], approval_count: int) -> str:
    if session.status in {"awaiting_human", "awaiting_human_confirmation"}:
        return "awaiting_human"
    compliance = session.compliance if isinstance(session.compliance, dict) else {}
    if compliance.get("blocking"):
        return "compliance_blocking"
    if _session_has_provider_fallback(session):
        return "provider_fallback"
    if session.status in {"accepted", "completed"}:
        return "completed"
    if session.status in {"blocked", "failed"}:
        return "failed"
    recovery_actions = summary.get("recovery_actions", []) if isinstance(summary.get("recovery_actions"), list) else []
    if approval_count or summary.get("resume_action") or recovery_actions:
        return "recovery_ready"
    return "interrupted"


def _session_has_provider_fallback(session: PlanSession) -> bool:
    if not session.decision_verdict:
        return False
    provider_runtime = session.decision_verdict.selected_provider_runtime
    return any("fallback" in str(key) and bool(value) for key, value in provider_runtime.items())


def _read_run_entries(runs_root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in sorted(runs_root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        entries.append(
            {
                "id": payload.get("id") or path.stem,
                "status": payload.get("status"),
                "initial_mode": payload.get("initial_mode"),
                "final_mode": payload.get("final_mode"),
                "accepted": payload.get("accepted"),
                "path": str(path),
            }
        )
    return entries[-25:][::-1]


def _read_job_entries(jobs_root: Path) -> list[dict[str, object]]:
    jobs: list[AgentJob] = []
    try:
        jobs = FileJobRuntime(jobs_root).list_recent()
    except Exception:
        jobs = []
    if not jobs:
        for path in sorted(jobs_root.glob("job-*.json"))[-25:]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    jobs.append(AgentJob.from_dict(payload))
            except Exception:
                continue
    return [
        {
            "id": job.id,
            "provider": job.provider,
            "kind": job.kind,
            "status": job.status,
            "phase": job.phase,
            "runtime_mode": job.runtime_mode,
            "updated_at": job.updated_at,
            "summary": job.summary,
        }
        for job in jobs[-25:][::-1]
    ]


def _read_job_payloads(jobs_root: Path) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for path in sorted(jobs_root.glob("job-*.json"))[-25:]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    for job in _read_job_entries(jobs_root):
        if not any(existing.get("id") == job.get("id") for existing in payloads):
            payloads.append(dict(job))
    return payloads[-25:][::-1]


def _evidence_state(project_root: Path) -> dict[str, object]:
    evidence_root = project_root / ".agent_orchestrator" / "evidence"
    return {
        "benchmark_report_present": (project_root / "docs" / "process" / "v1x-evidence-report.md").exists(),
        "trend_report_present": (project_root / "docs" / "process" / "v1x-evidence-trend.md").exists(),
        "evidence_cases_present": (project_root / "docs" / "process" / "evidence-cases.json").exists(),
        "real_tasks_json_present": (evidence_root / "real-tasks.json").exists(),
        "evidence_root": str(evidence_root),
    }


def _git_dirty_state(project_root: Path) -> dict[str, object]:
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=project_root,
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except Exception as exc:
        return {"available": False, "reason": str(exc), "changed_files": [], "dirty": False}
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return {
        "available": result.returncode == 0,
        "dirty": bool(lines),
        "changed_files": lines,
        "count": len(lines),
        "detail": result.stderr.strip() if result.returncode else "",
    }


def _git_head(project_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except Exception:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _memory_digest(memory_root: Path) -> dict[str, object]:
    path = memory_root / "memory.jsonl"
    if not path.exists():
        return {"count": 0, "recent": [], "namespaces": []}
    records: list[MemoryRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(MemoryRecord.from_dict(payload))
    namespaces = sorted({record.namespace for record in records})
    return {
        "count": len(records),
        "namespaces": namespaces,
        "recent": [record.to_dict() for record in records[-5:]][::-1],
    }


def _external_cache_status(project_root: Path) -> dict[str, object]:
    cli_path = shutil.which("explore-cache")
    mcp_path = shutil.which("explore-cache-mcp")
    return {
        "name": "explore_cache",
        "required": False,
        "cli_available": bool(cli_path),
        "mcp_available": bool(mcp_path),
        "cli_path": cli_path,
        "mcp_path": mcp_path,
        "project_cache_present": (project_root / "explore-cache").exists(),
        "status": "available" if cli_path or mcp_path else "optional_unavailable",
    }


def _evidence_memory_recommendation(
    project_root: Path,
    gate_evidence: dict[str, object],
    compliance: dict[str, object],
) -> dict[str, object]:
    gates = gate_evidence.get("gates", []) if isinstance(gate_evidence.get("gates"), list) else []
    eligible: list[dict[str, object]] = []
    compliance_has_signal = bool(compliance.get("blocking")) or bool(compliance.get("blocking_reasons")) or bool(compliance.get("warnings"))
    if compliance_has_signal:
        eligible.append(
            {
                "record_type": "compliance_result",
                "namespace": "evidence",
                "source_artifacts": ["team check-compliance"],
                "freshness": "fresh",
                "confidence": 1.0,
            }
        )
    if any(isinstance(gate, dict) and gate.get("name") == "full_tests" and gate.get("status") == "passed" for gate in gates):
        eligible.append(
            {
                "record_type": "full_gate_result",
                "namespace": "evidence",
                "source_artifacts": ["pytest"],
                "freshness": "fresh",
                "confidence": 1.0,
            }
        )
    if any(isinstance(gate, dict) and gate.get("name") == "evidence_report" and gate.get("status") == "passed" for gate in gates):
        eligible.append(
            {
                "record_type": "evidence_report",
                "namespace": "evidence",
                "source_artifacts": ["docs/process/v1x-evidence-report.md"],
                "freshness": "fresh",
                "confidence": 0.9,
            }
        )
    external = _external_cache_status(project_root)
    candidates = _memory_promotion_candidates(gate_evidence, compliance)
    return {
        "policy": "write only durable gate outcomes, compliance results, approval resolutions, and dogfood outcomes",
        "auto_write": False,
        "eligible_records": eligible,
        "promotion_policy": "candidates require provenance and explicit promotion before durable MemoryRecord write",
        "candidates": candidates,
        "candidate_count": len(candidates),
        "excluded": ["transient status", "planned gates without result"],
        "required_provenance_fields": ["source_artifacts"],
        "recovery_refs": _evidence_recovery_refs(project_root),
        "external_cache_status": external if external.get("status") == "available" else {**external, "status": "optional_unavailable"},
    }


def _evidence_recovery_refs(project_root: Path) -> dict[str, object]:
    index = WorkspaceIndexStore(project_root / ".agent_orchestrator" / "workspace").payload() or {}
    artifacts = index.get("artifacts", {}) if isinstance(index.get("artifacts"), dict) else {}
    return {
        "recovery_timeline": artifacts.get("recovery_timeline"),
        "runtime_event_stream": artifacts.get("runtime_event_stream"),
        "recovery_recommendation": artifacts.get("recovery_recommendation"),
        "run_ledger": artifacts.get("run_ledger"),
    }


def _evidence_runtime_fidelity(project_root: Path) -> dict[str, object]:
    index = WorkspaceIndexStore(project_root / ".agent_orchestrator" / "workspace").payload() or {}
    artifacts = index.get("artifacts", {}) if isinstance(index.get("artifacts"), dict) else {}
    return {
        "provider_session_snapshot": artifacts.get("provider_session_snapshot"),
        "runtime_event_stream": artifacts.get("runtime_event_stream"),
        "operation_receipt_format": CONTROL_PLANE_FORMATS["runtime_operation_receipt"],
        "policy": "read-only runtime fidelity evidence; does not imply persistent provider session ownership",
    }


def _memory_promotion_candidates(
    gate_evidence: dict[str, object],
    compliance: dict[str, object],
) -> list[dict[str, object]]:
    gates = gate_evidence.get("gates", []) if isinstance(gate_evidence.get("gates"), list) else []
    candidates = [
        _memory_candidate(
            "durable_outcome",
            "evidence",
            "Durable gate outcome can be promoted after final convergence evidence exists.",
            ["agent_orchestrator.evidence_bundle.v1"],
            ready=any(isinstance(gate, dict) and gate.get("status") == "passed" for gate in gates),
        ),
        _memory_candidate(
            "decision",
            "decision",
            "Control-plane operations track decisions can be promoted when linked to docs and evidence.",
            ["docs/process/ai-work-control-plane-operations-track-plan.md"],
            ready=True,
        ),
        _memory_candidate(
            "lesson",
            "knowledge",
            "Lessons from targeted-test failures can be promoted when backed by command output and patch context.",
            ["phase targeted tests"],
            ready=False,
        ),
        _memory_candidate(
            "recovery_note",
            "recovery",
            "Recovery notes can be promoted when they explain an interrupted, failed, or blocked run.",
            ["agent_orchestrator.run_ledger.v1"],
            ready=bool(compliance.get("blocking")) or bool(compliance.get("blocking_reasons")),
        ),
        _memory_candidate(
            "provider_runtime_health_note",
            "runtime_health",
            "Provider/runtime health notes can be promoted after setup or evidence gates record degraded capability.",
            ["agent_orchestrator.evidence_bundle.v1"],
            ready=False,
        ),
        _memory_candidate(
            "recovery_pattern",
            "recovery",
            "Live recovery patterns can be promoted after the timeline explains a repeated blocked or interrupted path.",
            ["agent_orchestrator.recovery_timeline.v1"],
            ready=bool(compliance.get("blocking")) or bool(compliance.get("blocking_reasons")),
        ),
        _memory_candidate(
            "runtime_degradation_note",
            "runtime_health",
            "Runtime degradation notes can be promoted when provider fallback or failed runtime events have provenance.",
            ["agent_orchestrator.runtime_event_stream.v1"],
            ready=False,
        ),
        _memory_candidate(
            "approval_delay_note",
            "approval",
            "Approval delay notes can be promoted when recovery timeline and approval inbox show a durable waiting pattern.",
            ["agent_orchestrator.approval_queue.v1", "agent_orchestrator.recovery_timeline.v1"],
            ready=False,
        ),
        _memory_candidate(
            "compliance_blocking_note",
            "compliance",
            "Compliance blocking notes can be promoted when blocking reasons are linked to recovery evidence.",
            ["team check-compliance", "agent_orchestrator.recovery_timeline.v1"],
            ready=bool(compliance.get("blocking")),
        ),
    ]
    return candidates


def _memory_candidate(
    record_type: str,
    namespace: str,
    summary: str,
    source_artifacts: list[str],
    *,
    ready: bool,
) -> dict[str, object]:
    return {
        "id": _stable_id("memory-candidate", record_type, namespace, *source_artifacts),
        "record_type": record_type,
        "namespace": namespace,
        "summary": summary,
        "ready_for_promotion": ready,
        "provenance": {
            "source_artifacts": source_artifacts,
        },
        "promotion_gate": "explicit approval or evidence-backed curator action required",
    }


def _context_stale_warnings(docs_context: dict[str, object], memory_records: list[dict[str, object]]) -> list[str]:
    warnings: list[str] = []
    doc_sync = docs_context.get("doc_sync", {}) if isinstance(docs_context.get("doc_sync"), dict) else {}
    if doc_sync.get("missing_docs"):
        warnings.append("docs context has missing canonical docs")
    if doc_sync.get("stale_docs"):
        warnings.append("docs context has stale canonical docs")
    for record in memory_records:
        freshness = record.get("freshness")
        if freshness and freshness != "fresh":
            warnings.append(f"memory {record.get('id')} freshness={freshness}")
    return warnings


def _generated_approval_items(sessions: list[PlanSession]) -> list[ApprovalItem]:
    items: list[ApprovalItem] = []
    for session in sessions:
        payload = session.to_dict()
        summary = payload.get("status_summary", {}) if isinstance(payload.get("status_summary"), dict) else {}
        if session.status in {"blocked", "awaiting_human", "awaiting_human_confirmation"}:
            reason = str(summary.get("primary_reason") or summary.get("block_detail") or f"Session {session.status} needs human decision.")
            items.append(
                _approval_item(
                    session=session,
                    reason=reason,
                    reason_code="awaiting_human_decision"
                    if session.status in {"awaiting_human", "awaiting_human_confirmation"}
                    else "blocked_session",
                    scope="session",
                    scope_id=session.id,
                    recommended_action=str(summary.get("primary_action") or "human_decision"),
                    evidence_refs=[f"plans/{session.id}/session.json"],
                )
            )
        compliance = session.compliance if isinstance(session.compliance, dict) else {}
        if compliance.get("blocking"):
            for reason in compliance.get("blocking_reasons", []) or ["compliance blocking"]:
                items.append(
                    _approval_item(
                        session=session,
                        reason=f"Compliance blocking: {reason}",
                        reason_code="compliance_blocking",
                        scope="compliance",
                        scope_id=session.id,
                        recommended_action="inspect_compliance",
                        evidence_refs=["team check-compliance", f"plans/{session.id}/session.json"],
                    )
                )
        if isinstance(session.decision_verdict, object) and session.decision_verdict:
            provider_runtime = session.decision_verdict.selected_provider_runtime
            for key, value in provider_runtime.items():
                if "fallback_from" in key and value:
                    items.append(
                        _approval_item(
                            session=session,
                            reason=f"Provider fallback observed: {key}={value}",
                            reason_code="provider_fallback",
                            scope="provider_fallback",
                            scope_id=session.id,
                            recommended_action="inspect_provider_fallback",
                            evidence_refs=[f"plans/{session.id}/verdict.json"],
                        )
                    )
    return items


def _approval_item(
    *,
    session: PlanSession,
    reason: str,
    reason_code: ApprovalReasonCode,
    scope: str,
    scope_id: str,
    recommended_action: str,
    evidence_refs: list[str],
) -> ApprovalItem:
    return ApprovalItem(
        id=_stable_id("approval", session.id, scope, reason),
        status="pending",
        reason_code=reason_code,
        reason=reason,
        scope=scope,
        scope_id=scope_id,
        recommended_action=recommended_action,
        session_id=session.id,
        run_id=session.resume.linked_execution_run_id,
        plan_ref=f"plans/{session.id}/session.json",
        topology_ref=f"topology:{session.id}",
        run_ref=f"runs/{session.resume.linked_execution_run_id}.json" if session.resume.linked_execution_run_id else None,
        evidence_ref=evidence_refs[0] if evidence_refs else None,
        memory_candidate_ref=f"memory-candidate:{session.id}:{scope}",
        evidence_refs=evidence_refs,
    )


def _approval_reason_code_from_payload(data: dict[str, object]) -> ApprovalReasonCode:
    raw = data.get("reason_code")
    allowed = {
        "blocked_session",
        "awaiting_human_decision",
        "compliance_blocking",
        "provider_fallback",
        "rescue_reroute",
        "dirty_state_overlap",
        "external_cache_unavailable",
    }
    if isinstance(raw, str) and raw in allowed:
        return raw  # type: ignore[return-value]
    scope = str(data.get("scope") or "")
    reason = str(data.get("reason") or "").lower()
    if scope == "compliance" or "compliance" in reason:
        return "compliance_blocking"
    if scope == "provider_fallback" or "fallback" in reason:
        return "provider_fallback"
    if scope == "rescue" or "reroute" in reason:
        return "rescue_reroute"
    if "dirty" in reason:
        return "dirty_state_overlap"
    if "external cache" in reason or "explore_cache" in reason:
        return "external_cache_unavailable"
    if "human" in reason or "awaiting" in reason:
        return "awaiting_human_decision"
    return "blocked_session"


def _topology_node(
    node_type: str,
    node_id: str,
    label: str,
    status: str,
    *,
    owner_role: str | None = None,
) -> dict[str, object]:
    return {
        "id": node_id,
        "type": node_type,
        "label": label,
        "status": status,
        "owner_role": owner_role,
    }


def _topology_blueprint(
    session: PlanSession,
    nodes: list[dict[str, object]],
    edges: list[dict[str, object]],
    approvals: dict[str, object],
    evidence_bundle: dict[str, object],
) -> dict[str, object]:
    return {
        "id": f"blueprint:{session.id}",
        "name": session.structured_brief.goal or session.requirement,
        "read_only": True,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "approval_count": approvals.get("counts", {}).get("pending", 0)
        if isinstance(approvals.get("counts"), dict)
        else 0,
        "evidence_status": evidence_bundle.get("status"),
        "export_policy": "snapshot only; topology editing is out of scope",
    }


def _topology_lanes(nodes: list[dict[str, object]]) -> list[dict[str, object]]:
    lane_order = ["control_plane", "execution", "review", "approval", "evidence_memory"]
    lane_map = {
        "state": "control_plane",
        "context": "control_plane",
        "strategy": "control_plane",
        "manager_slot": "control_plane",
        "worker": "execution",
        "implementation": "execution",
        "rescue": "execution",
        "condition": "execution",
        "review": "review",
        "approval": "approval",
        "evidence": "evidence_memory",
        "memory": "evidence_memory",
    }
    grouped: dict[str, list[str]] = {lane: [] for lane in lane_order}
    for node in nodes:
        node_type = str(node.get("type") or "")
        lane = lane_map.get(node_type, "execution")
        grouped.setdefault(lane, []).append(str(node.get("id")))
    return [{"id": lane, "node_ids": grouped.get(lane, [])} for lane in lane_order if grouped.get(lane)]


def _topology_points(nodes: list[dict[str, object]], node_type: str) -> list[dict[str, object]]:
    return [
        {"node_id": node.get("id"), "status": node.get("status"), "label": node.get("label")}
        for node in nodes
        if node.get("type") == node_type
    ]


def _topology_runtime_boundaries(session: PlanSession) -> list[dict[str, object]]:
    provider_runtime = session.decision_verdict.selected_provider_runtime if session.decision_verdict else {}
    return [
        {
            "boundary": "strategy_to_execution",
            "executes": False,
            "authority": "approved_plan_gate",
        },
        {
            "boundary": "provider_runtime",
            "selected_provider_runtime": provider_runtime,
            "policy": "runtime executes below the control plane",
        },
    ]


def _gate_evidence_summary(
    project_root: Path,
    compliance: dict[str, object],
    evidence_state: dict[str, object],
) -> dict[str, object]:
    gates = [
        {
            "name": "targeted_tests",
            "command": "phase-specific pytest slice",
            "cwd": str(project_root),
            "exit_code": None,
            "duration_seconds": None,
            "summary": "recorded per implementation phase",
            "artifact_path": None,
            "status": "planned",
        },
        {
            "name": "full_tests",
            "command": "pytest",
            "cwd": str(project_root),
            "exit_code": None,
            "duration_seconds": None,
            "summary": "reserved for final convergence",
            "artifact_path": None,
            "status": "planned",
        },
        {
            "name": "compliance",
            "command": "env PYTHONPATH=src python -m agent_orchestrator.cli team check-compliance",
            "cwd": str(project_root),
            "exit_code": 1 if bool(compliance.get("blocking", False)) else 0,
            "duration_seconds": None,
            "summary": "blocked" if bool(compliance.get("blocking", False)) else "passed or warning-only",
            "artifact_path": None,
            "status": "failed" if bool(compliance.get("blocking", False)) else "passed",
        },
        {
            "name": "evidence_report",
            "command": "python -m agent_orchestrator.cli evidence report --output docs/process/v1x-evidence-report.md",
            "cwd": str(project_root),
            "exit_code": 0 if bool(evidence_state.get("benchmark_report_present", False)) else None,
            "duration_seconds": None,
            "summary": "local markdown evidence report present"
            if bool(evidence_state.get("benchmark_report_present", False))
            else "local markdown evidence report missing",
            "artifact_path": "docs/process/v1x-evidence-report.md",
            "status": "passed" if bool(evidence_state.get("benchmark_report_present", False)) else "missing",
        },
    ]
    return {
        "format": "agent_orchestrator.gate_evidence.v1",
        "log_policy": "large logs stay in artifact_path; setup and release readiness show summaries only",
        "gates": gates,
        "latest": gates[-1],
    }


def _stable_id(prefix: str, *parts: str) -> str:
    seed = "|".join(str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:12]}"


def _atomic_write_json(path: Path, payload: dict[str, object]) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)
    return payload


def _read_json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _artifact_ref(payload: dict[str, object]) -> dict[str, object]:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return {
        "format": payload.get("format"),
        "digest": hashlib.sha256(data.encode("utf-8")).hexdigest(),
        "created_at": payload.get("created_at"),
        "recorded_at": now_iso(),
        "status": payload.get("status"),
        "summary": _artifact_summary(payload),
    }


def _artifact_summary(payload: dict[str, object]) -> dict[str, object]:
    artifact_format = payload.get("format")
    if artifact_format == CONTROL_PLANE_FORMATS["workspace_state"]:
        return {
            "plans": len(payload.get("plans", [])) if isinstance(payload.get("plans"), list) else 0,
            "runs": len(payload.get("runs", [])) if isinstance(payload.get("runs"), list) else 0,
            "dirty": (payload.get("dirty_state") or {}).get("dirty")
            if isinstance(payload.get("dirty_state"), dict)
            else None,
        }
    if artifact_format == CONTROL_PLANE_FORMATS["context_packet"]:
        return {
            "query": payload.get("query"),
            "changed_files": len(payload.get("changed_files", [])) if isinstance(payload.get("changed_files"), list) else 0,
            "stale_warnings": len(payload.get("stale_warnings", [])) if isinstance(payload.get("stale_warnings"), list) else 0,
        }
    if artifact_format == CONTROL_PLANE_FORMATS["strategy_decision"]:
        return {"session_id": payload.get("session_id"), "next_goal": payload.get("next_goal"), "executes": payload.get("executes")}
    if artifact_format == CONTROL_PLANE_FORMATS["topology_snapshot"]:
        return {
            "session_id": payload.get("session_id"),
            "nodes": len(payload.get("nodes", [])) if isinstance(payload.get("nodes"), list) else 0,
            "read_only": payload.get("read_only"),
        }
    if artifact_format == CONTROL_PLANE_FORMATS["evidence_bundle"]:
        return {"status": payload.get("status")}
    return {}


def _resolve_root(project_root: Path, path: Path | str) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else project_root / candidate

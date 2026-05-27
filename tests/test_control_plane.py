import json
from pathlib import Path

from agent_orchestrator import OrchestrationMode
from agent_orchestrator.control_plane import (
    WorkspaceIndexStore,
    build_approval_queue,
    build_context_packet,
    build_evidence_bundle,
    build_execution_topology_snapshot,
    build_provider_session_snapshot,
    build_recovery_recommendation,
    build_recovery_timeline,
    build_run_ledger,
    build_runtime_event_stream,
    build_workspace_index,
    build_workspace_state_snapshot,
    resolve_approval_item,
)
from agent_orchestrator.jobs import FileJobRuntime, JobRequest
from agent_orchestrator.memory import MemoryRecord, MemoryStore
from agent_orchestrator.orchestrator import Orchestrator
from agent_orchestrator.planning import PlanStore, TeamOrchestrator
from test_support import start_approved_session, start_reviewed_session, write_minimal_process_docs


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "control_plane"


def test_control_plane_golden_fixtures_pin_minimum_contracts() -> None:
    expected_formats = {
        "empty_workspace_state.json": "agent_orchestrator.workspace_state.v1",
        "active_topology_snapshot.json": "agent_orchestrator.execution_topology_snapshot.v1",
        "blocked_approval_item.json": "agent_orchestrator.approval_item.v1",
        "resolved_approval_item.json": "agent_orchestrator.approval_item.v1",
        "evidence_bundle.json": "agent_orchestrator.evidence_bundle.v1",
    }

    for fixture_name, expected_format in expected_formats.items():
        payload = json.loads((FIXTURE_ROOT / fixture_name).read_text(encoding="utf-8"))
        assert payload["format"] == expected_format

    topology = json.loads((FIXTURE_ROOT / "active_topology_snapshot.json").read_text(encoding="utf-8"))
    assert topology["read_only"] is True
    assert topology["strategy_decision"]["executes"] is False

    approval = json.loads((FIXTURE_ROOT / "blocked_approval_item.json").read_text(encoding="utf-8"))
    assert {"id", "status", "reason_code", "reason", "scope", "scope_id", "recommended_action", "evidence_refs"} <= set(approval)


def test_workspace_state_snapshot_empty_workspace_is_valid(tmp_path) -> None:
    payload = build_workspace_state_snapshot(
        tmp_path,
        plans_root=tmp_path / "plans",
        runs_root=tmp_path / "runs",
        jobs_root=tmp_path / "jobs",
        approvals_root=tmp_path / "approvals",
        write_index=True,
    )

    assert payload["format"] == "agent_orchestrator.workspace_state.v1"
    assert payload["plans"] == []
    assert payload["runs"] == []
    assert payload["jobs"] == []
    assert payload["external_cache"]["required"] is False
    assert WorkspaceIndexStore(tmp_path / ".agent_orchestrator" / "workspace").read() is not None


def test_workspace_index_records_control_plane_artifact_lifecycle_refs(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    build_workspace_state_snapshot(
        tmp_path,
        plans_root=tmp_path / "plans",
        runs_root=tmp_path / "runs",
        jobs_root=tmp_path / "jobs",
        approvals_root=tmp_path / "approvals",
        write_index=True,
    )
    build_context_packet(
        tmp_path,
        query="workspace lifecycle",
        jobs_root=tmp_path / ".agent_orchestrator" / "jobs",
        memory_root=tmp_path / ".agent_orchestrator" / "memory",
    )
    build_evidence_bundle(tmp_path)

    payload = json.loads((tmp_path / ".agent_orchestrator" / "workspace" / "index.json").read_text(encoding="utf-8"))
    assert payload["format"] == "agent_orchestrator.workspace_index.v1"
    assert payload["workspace_state"]["format"] == "agent_orchestrator.workspace_state.v1"
    assert payload["artifacts"]["workspace_state"]["format"] == "agent_orchestrator.workspace_state.v1"
    assert payload["artifacts"]["context_packet"]["format"] == "agent_orchestrator.context_packet.v1"
    assert payload["artifacts"]["evidence_bundle"]["format"] == "agent_orchestrator.evidence_bundle.v1"
    assert payload["artifacts"]["context_packet"]["digest"]
    assert WorkspaceIndexStore(tmp_path / ".agent_orchestrator" / "workspace").read() is not None


def test_workspace_index_v2_surfaces_operator_current_state(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    session = start_reviewed_session(team, "Need a workspace program index")
    session.status = "awaiting_human"
    team.store.write_session(session)

    payload = build_workspace_index(
        tmp_path,
        plans_root=tmp_path / "plans",
        runs_root=tmp_path / "runs",
        jobs_root=tmp_path / "jobs",
        approvals_root=tmp_path / "approvals",
        provider_health={"runtime_mode": "mock", "status": "available"},
    )

    assert payload["format"] == "agent_orchestrator.workspace_index.v1"
    assert payload["workspace_state"]["format"] == "agent_orchestrator.workspace_state.v1"
    assert payload["program"]["kind"] == "workspace_program"
    assert payload["program"]["active_plan_count"] >= 1
    assert payload["open_approvals"]
    assert payload["active_artifacts"]["workspace_state"]["format"] == "agent_orchestrator.workspace_state.v1"
    assert isinstance(payload["recent_artifacts"], list)
    assert isinstance(payload["recent_runs"], list)
    assert isinstance(payload["memory_candidates"], list)
    assert payload["provider_runtime_health"]["runtime_mode"] == "mock"
    assert payload["recovery_timeline"]["format"] == "agent_orchestrator.recovery_timeline.v1"
    assert payload["runtime_events"]["format"] == "agent_orchestrator.runtime_event_stream.v1"
    assert payload["recovery_recommendation"]["format"] == "agent_orchestrator.recovery_recommendation.v1"
    assert "blocking" in payload["blocking_summary"]
    assert payload["resume_hint"]
    assert payload["last_checkpoint"]["status"] == "checkpointed"


def test_context_packet_combines_docs_and_memory_without_strategy(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    MemoryStore(tmp_path / ".agent_orchestrator" / "memory").append(
        namespace="research",
        session_id="plan-1",
        record_type="note",
        summary="workspace state and context packet should stay separate from strategy",
        provenance={"source_artifacts": ["docs/process/context-map.md"], "base_commit": "abc"},
        freshness="fresh",
        confidence=0.9,
    )

    payload = build_context_packet(
        tmp_path,
        query="workspace state context packet",
        changed_files=["src/agent_orchestrator/control_plane.py"],
        jobs_root=tmp_path / ".agent_orchestrator" / "jobs",
        memory_root=tmp_path / ".agent_orchestrator" / "memory",
    )

    assert payload["format"] == "agent_orchestrator.context_packet.v1"
    assert payload["docs_context"]["format"] == "agent_orchestrator.docs_context.v1"
    assert payload["memory_records"]
    assert payload["token_budget_summary"]["policy"].startswith("minimum sufficient context")


def test_topology_snapshot_is_read_only_and_links_approval_evidence(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = start_approved_session(team, "Implement workspace telemetry summaries")
    executed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)

    payload = build_execution_topology_snapshot(
        executed,
        plans_root=tmp_path / "plans",
        approvals_root=tmp_path / "approvals",
        project_root=tmp_path,
    )

    assert payload["format"] == "agent_orchestrator.execution_topology_snapshot.v1"
    assert payload["read_only"] is True
    assert {"state", "context", "strategy", "manager_slot", "evidence", "memory"} <= set(payload["fixed_node_types"])
    assert {"implementation", "review", "rescue", "condition", "approval"} <= set(payload["fixed_node_types"])
    assert payload["blueprint"]["read_only"] is True
    assert payload["blueprint"]["export_policy"] == "snapshot only; topology editing is out of scope"
    assert payload["lanes"]
    assert isinstance(payload["approval_points"], list)
    assert payload["evidence_points"]
    assert payload["runtime_boundaries"][0]["authority"] == "approved_plan_gate"
    assert payload["strategy_decision"]["executes"] is False
    assert payload["strategy_decision"]["control_plane_focus"] == "state_context_strategy_topology_approval_evidence_memory_recovery"
    assert payload["strategy_decision"]["topology_policy"]["signals"]
    assert payload["strategy_decision"]["runtime_health"]["records_only"] is True
    assert payload["strategy_decision"]["tool_inventory"]["mutation_policy"].startswith("inventory only")
    assert payload["strategy_decision"]["usage_cost"]["source"] == "placeholder"
    assert payload["evidence_bundle"]["format"] == "agent_orchestrator.evidence_bundle.v1"


def test_approval_queue_resolve_records_decision_without_execution(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    blocked = start_reviewed_session(team, "Need architecture direction before implementation")
    blocked.status = "awaiting_human"
    team.store.write_session(blocked)

    queue = build_approval_queue(tmp_path, plans_root=tmp_path / "plans", approvals_root=tmp_path / "approvals")
    item = queue["items"][0]
    assert queue["inbox_summary"]["pending_count"] >= 1
    assert queue["inbox_summary"]["blocking_count"] >= 0
    assert queue["inbox_summary"]["reason_code_distribution"]["awaiting_human_decision"] >= 1
    assert "team approvals resolve" in queue["inbox_summary"]["recommended_next_command"]
    assert item["plan_ref"] == f"plans/{blocked.id}/session.json"
    assert item["topology_ref"] == f"topology:{blocked.id}"
    assert item["evidence_ref"]
    assert item["memory_candidate_ref"]
    result = resolve_approval_item(
        item["id"],
        status="resolved",
        reason="Documented human decision",
        project_root=tmp_path,
        plans_root=tmp_path / "plans",
        approvals_root=tmp_path / "approvals",
    )

    assert result["resolved_item"]["status"] == "resolved"
    assert result["resolved_item"]["reason_code"] == "awaiting_human_decision"
    assert "execution gates remain authoritative" in result["mutation_policy"]
    memory = MemoryStore(tmp_path / ".agent_orchestrator" / "memory").query(namespace="approval")
    assert memory[0]["provenance"]["source_artifacts"] == [item["id"]]


def test_approval_queue_hydrates_legacy_reason_code() -> None:
    from agent_orchestrator.control_plane import ApprovalItem

    legacy = ApprovalItem.from_dict(
        {
            "id": "approval-legacy",
            "status": "pending",
            "reason": "Compliance blocking: docs drift",
            "scope": "compliance",
            "scope_id": "plan-1",
            "recommended_action": "inspect_compliance",
        }
    )

    assert legacy.reason_code == "compliance_blocking"
    assert legacy.to_dict()["reason_code"] == "compliance_blocking"
    assert legacy.to_dict()["plan_ref"] is None


def test_evidence_bundle_reports_gate_evidence_shape(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    payload = build_evidence_bundle(tmp_path, compliance={"blocking": True, "blocking_reasons": ["docs drift"], "warnings": []})

    assert payload["format"] == "agent_orchestrator.evidence_bundle.v1"
    assert payload["gate_evidence"]["format"] == "agent_orchestrator.gate_evidence.v1"
    assert payload["runtime_health"]["records_only"] is True
    assert payload["tool_inventory"]["source"] == "control_plane_placeholder"
    assert payload["usage_cost"]["source"] == "placeholder"
    assert "recovery_refs" in payload
    assert payload["compliance"]["blocking"] is True
    recommendation = payload["memory_recommendation"]
    assert recommendation["auto_write"] is False
    assert recommendation["eligible_records"][0]["record_type"] == "compliance_result"
    assert recommendation["candidate_count"] == 9
    assert {candidate["record_type"] for candidate in recommendation["candidates"]} == {
        "durable_outcome",
        "decision",
        "lesson",
        "recovery_note",
        "provider_runtime_health_note",
        "recovery_pattern",
        "runtime_degradation_note",
        "approval_delay_note",
        "compliance_blocking_note",
    }
    assert all(candidate["provenance"]["source_artifacts"] for candidate in recommendation["candidates"])
    assert recommendation["external_cache_status"]["status"] in {"available", "optional_unavailable"}


def test_memory_record_hydrates_legacy_and_provenance_payloads() -> None:
    legacy = MemoryRecord.from_dict({"summary": "legacy", "session_id": "plan-1"})
    current = MemoryRecord.from_dict(
        {
            "summary": "current",
            "session_id": "plan-1",
            "provenance": {"base_commit": "abc"},
            "freshness": "fresh",
            "confidence": 0.8,
            "external_cache_status": {"status": "optional_unavailable"},
        }
    )

    assert legacy.provenance == {}
    assert current.to_dict()["provenance"]["base_commit"] == "abc"
    assert json.loads(json.dumps(current.to_dict()))["freshness"] == "fresh"


def test_dogfood_control_plane_pipeline_links_state_context_strategy_approval_evidence_memory(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    active = start_reviewed_session(team, "Build a persisted plan artifact")
    blocked = start_reviewed_session(team, "Need architecture direction before implementation")
    blocked.status = "awaiting_human"
    team.store.write_session(blocked)

    workspace = build_workspace_state_snapshot(
        tmp_path,
        plans_root=tmp_path / "plans",
        runs_root=tmp_path / "runs",
        jobs_root=tmp_path / "jobs",
        approvals_root=tmp_path / "approvals",
        write_index=True,
    )
    workspace_index = build_workspace_index(
        tmp_path,
        plans_root=tmp_path / "plans",
        runs_root=tmp_path / "runs",
        jobs_root=tmp_path / "jobs",
        approvals_root=tmp_path / "approvals",
    )
    context = build_context_packet(
        tmp_path,
        query="dogfood control plane pipeline",
        jobs_root=tmp_path / ".agent_orchestrator" / "jobs",
        memory_root=tmp_path / ".agent_orchestrator" / "memory",
    )
    topology = build_execution_topology_snapshot(
        active,
        plans_root=tmp_path / "plans",
        approvals_root=tmp_path / "approvals",
        project_root=tmp_path,
    )
    evidence = build_evidence_bundle(tmp_path)
    queue = build_approval_queue(tmp_path, plans_root=tmp_path / "plans", approvals_root=tmp_path / "approvals")
    approval = next(item for item in queue["items"] if item["session_id"] == blocked.id)
    resolved = resolve_approval_item(
        approval["id"],
        status="resolved",
        reason="Dogfood human decision recorded",
        project_root=tmp_path,
        plans_root=tmp_path / "plans",
        approvals_root=tmp_path / "approvals",
    )
    MemoryStore(tmp_path / ".agent_orchestrator" / "memory").append(
        namespace="dogfood",
        session_id=active.id,
        record_type="dogfood_outcome",
        summary="Control-plane dogfood pipeline completed.",
        provenance={"source_artifacts": [active.id, approval["id"], "agent_orchestrator.evidence_bundle.v1"]},
        freshness="fresh",
        confidence=1.0,
    )
    resolved_queue = build_approval_queue(tmp_path, plans_root=tmp_path / "plans", approvals_root=tmp_path / "approvals")
    memory = MemoryStore(tmp_path / ".agent_orchestrator" / "memory").query(namespace="dogfood")

    assert workspace["format"] == "agent_orchestrator.workspace_state.v1"
    assert workspace_index["format"] == "agent_orchestrator.workspace_index.v1"
    assert workspace_index["program"]["kind"] == "workspace_program"
    assert context["format"] == "agent_orchestrator.context_packet.v1"
    assert topology["strategy_decision"]["format"] == "agent_orchestrator.strategy_decision.v1"
    assert topology["blueprint"]["read_only"] is True
    assert topology["run_ledger"]["format"] == "agent_orchestrator.run_ledger.v1"
    assert queue["inbox_summary"]["pending_count"] >= 1
    assert topology["strategy_decision"]["executes"] is False
    assert topology["strategy_decision"]["recovery_policy"]["execution_gate_authority"] == "approved_plan_gate"
    assert evidence["memory_recommendation"]["auto_write"] is False
    assert evidence["memory_recommendation"]["candidates"]
    assert resolved["resolved_item"]["status"] == "resolved"
    assert any(item["id"] == approval["id"] and item["status"] == "resolved" for item in resolved_queue["items"])
    assert memory[0]["provenance"]["source_artifacts"] == [active.id, approval["id"], "agent_orchestrator.evidence_bundle.v1"]


def test_run_ledger_records_recovery_statuses_and_links_workspace_index(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    awaiting = start_reviewed_session(team, "Need human decision")
    awaiting.status = "awaiting_human"
    team.store.write_session(awaiting)
    awaiting_payload = build_run_ledger(
        tmp_path,
        plans_root=tmp_path / "plans",
        runs_root=tmp_path / "runs",
        jobs_root=tmp_path / "jobs",
        approvals_root=tmp_path / "approvals",
    )

    assert awaiting_payload["format"] == "agent_orchestrator.run_ledger.v1"
    statuses = {entry["status"] for entry in awaiting_payload["entries"]}
    assert "awaiting_human" in statuses
    awaiting.status = "needs_revision"
    awaiting.compliance = {"blocking": True, "blocking_reasons": ["docs drift"], "warnings": []}
    team.store.write_session(awaiting)

    payload = build_run_ledger(
        tmp_path,
        plans_root=tmp_path / "plans",
        runs_root=tmp_path / "runs",
        jobs_root=tmp_path / "jobs",
        approvals_root=tmp_path / "approvals",
    )
    statuses = {entry["status"] for entry in payload["entries"]}
    assert "compliance_blocking" in statuses
    assert payload["summary"]["entry_count"] >= 1
    index = json.loads((tmp_path / ".agent_orchestrator" / "workspace" / "index.json").read_text(encoding="utf-8"))
    assert index["artifacts"]["run_ledger"]["format"] == "agent_orchestrator.run_ledger.v1"


def test_recovery_timeline_records_operator_recovery_statuses(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    session = start_reviewed_session(team, "Need live recovery telemetry")
    session.status = "awaiting_human"
    team.store.write_session(session)

    payload = build_recovery_timeline(
        tmp_path,
        plans_root=tmp_path / "plans",
        runs_root=tmp_path / "runs",
        jobs_root=tmp_path / "jobs",
        approvals_root=tmp_path / "approvals",
    )

    assert payload["format"] == "agent_orchestrator.recovery_timeline.v1"
    assert payload["read_only"] is True
    assert {
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
    } <= set(payload["status_catalog"])
    assert payload["summary"]["current_status"] == "awaiting_human"
    assert payload["summary"]["resume_hint"]
    assert payload["summary"]["last_checkpoint"]["status"] == "checkpointed"
    index = json.loads((tmp_path / ".agent_orchestrator" / "workspace" / "index.json").read_text(encoding="utf-8"))
    assert index["artifacts"]["recovery_timeline"]["format"] == "agent_orchestrator.recovery_timeline.v1"


def test_runtime_event_stream_records_runtime_intent_result_and_fallback(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    runtime = FileJobRuntime(tmp_path / "jobs")
    job = runtime.start(
        JobRequest(
            task_id="task-runtime",
            provider="claude",
            kind="review",
            prompt="review",
            cwd=str(tmp_path),
            runtime_mode="direct_api",
            metadata={"fallback_reason": "provider unavailable", "degraded_capability_reason": "auth missing"},
        )
    )
    runtime.fail(job.id, summary="review failed", error="auth missing")

    payload = build_runtime_event_stream(
        tmp_path,
        plans_root=tmp_path / "plans",
        runs_root=tmp_path / "runs",
        jobs_root=tmp_path / "jobs",
        approvals_root=tmp_path / "approvals",
    )

    assert payload["format"] == "agent_orchestrator.runtime_event_stream.v1"
    assert payload["read_only"] is True
    assert payload["mutation_policy"].startswith("records runtime intent")
    job_event = next(event for event in payload["events"] if event.get("job_id") == job.id)
    assert job_event["runtime_mode"] == "direct_api"
    assert job_event["result_status"] == "failed"
    assert job_event["failure_reason"] == "auth missing"
    assert job_event["degraded_capability_reason"] == "auth missing"
    assert job_event["session_liveness"]["state"] == "terminal"
    assert job_event["operation_support"]["send"] == "already_terminal"
    assert job_event["runtime_measurement"]["measurement_status"] == "measured"
    assert job_event["runtime_measurement"]["exit_code"] is None
    assert job_event["usage_cost"]["source"] == "placeholder"
    assert job_event["usage_cost"]["measurement_status"] == "placeholder"
    assert payload["provider_session_snapshots"][0]["format"] == "agent_orchestrator.provider_session_snapshot.v1"
    index = json.loads((tmp_path / ".agent_orchestrator" / "workspace" / "index.json").read_text(encoding="utf-8"))
    assert index["artifacts"]["runtime_event_stream"]["format"] == "agent_orchestrator.runtime_event_stream.v1"


def test_provider_session_snapshot_records_liveness_and_receipts(tmp_path) -> None:
    runtime = FileJobRuntime(tmp_path / "jobs")
    job = runtime.start(
        JobRequest(
            task_id="task-session",
            provider="codex",
            kind="implementation",
            prompt="build",
            cwd=str(tmp_path),
            runtime_mode="cli_isolated",
        )
    )
    runtime.send(job.id, "continue")

    payload = build_provider_session_snapshot(job.id, tmp_path, jobs_root=tmp_path / "jobs")

    assert payload["format"] == "agent_orchestrator.provider_session_snapshot.v1"
    assert payload["job_id"] == job.id
    assert payload["runtime_mode"] == "cli_isolated"
    assert payload["liveness"]["state"] == "unknown"
    assert payload["runtime_measurement"]["format"] == "agent_orchestrator.runtime_measurement.v1"
    assert payload["runtime_measurement"]["measurement_status"] == "placeholder"
    assert payload["operation_support"]["send"] == "available"
    assert payload["last_operation_receipt"]["format"] == "agent_orchestrator.runtime_operation_receipt.v1"
    assert payload["last_operation_receipt"]["status"] == "accepted"


def test_provider_session_snapshot_exposes_provider_owned_ref(tmp_path) -> None:
    runtime = FileJobRuntime(tmp_path / "jobs")
    job = runtime.start(
        JobRequest(
            task_id="task-session-ref",
            provider="codex",
            kind="implementation",
            prompt="build",
            cwd=str(tmp_path),
        )
    )
    runtime.complete(
        job.id,
        summary="done",
        parsed_payload={
            "provider_session_ref": {
                "format": "agent_orchestrator.provider_session_ref.v1",
                "provider": "codex",
                "runtime_id": "codex_exec_json",
                "session_id": "codex-session-1",
                "thread_id": "codex-thread-1",
                "provider_owned": True,
                "continuation_guarantee": "provider_owned",
            }
        },
        exit_code=0,
    )

    payload = build_provider_session_snapshot(job.id, tmp_path, jobs_root=tmp_path / "jobs")

    assert payload["provider_session_ref"]["format"] == "agent_orchestrator.provider_session_ref.v1"
    assert payload["provider_session_ref"]["runtime_id"] == "codex_exec_json"
    assert payload["provider_session_ref"]["provider_owned"] is True


def test_recovery_recommendation_is_read_only_and_explains_next_step(tmp_path) -> None:
    write_minimal_process_docs(tmp_path)
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    session = start_reviewed_session(team, "Need a recovery recommendation")
    session.status = "awaiting_human"
    team.store.write_session(session)

    payload = build_recovery_recommendation(session)

    assert payload["format"] == "agent_orchestrator.recovery_recommendation.v1"
    assert payload["read_only"] is True
    assert payload["human_decision_required"] is True
    assert payload["may_resume_execution"] is False
    assert payload["required_approval_or_evidence"]["approval_required"] is True
    assert payload["safest_next_operator_command"]
    assert "agent_orchestrator.recovery_timeline.v1" in payload["recoverable_artifact_refs"]

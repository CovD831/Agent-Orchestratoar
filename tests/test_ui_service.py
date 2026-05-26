import json
from pathlib import Path

from agent_orchestrator import OrchestrationMode, Orchestrator
from agent_orchestrator.jobs import FileJobRuntime, JobRequest
from agent_orchestrator.planning import PlanStore, TeamOrchestrator
from agent_orchestrator.run_store import RunStore
from agent_orchestrator.ui_service import DashboardService, build_dashboard_service


def _service(tmp_path):
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=FileJobRuntime(root=tmp_path / "jobs"),
        project_root=tmp_path,
    )
    team.orchestrator.run_store = RunStore(root=tmp_path / "runs")
    return DashboardService(
        team=team,
        plans_root=tmp_path / "plans",
        runs_root=tmp_path / "runs",
        jobs_root=tmp_path / "jobs",
    )


def test_dashboard_lists_sessions_and_builds_detail(tmp_path) -> None:
    service = _service(tmp_path)
    session = service.create_session("Build a persisted plan artifact")

    sessions = service.list_sessions()["sessions"]
    detail = service.get_session(str(session["id"]))

    assert sessions[0]["id"] == session["id"]
    assert detail["session"]["id"] == session["id"]
    assert detail["next_action"]["primary_action"] == "mark_draft_ready"
    assert detail["next_action"]["primary_label"] == "确认初稿"
    assert detail["actions"]
    draft_action = next(action for action in detail["actions"] if action["id"] == "mark_draft_ready")
    assert draft_action["enabled"] is True
    assert draft_action["state_changes"]
    assert detail["events"]
    assert detail["messages"]["count"] >= 2
    assert detail["messages"]["threads"].get("main", 0) >= 1
    assert detail["evidence_summary"]["memory_record_count"] >= 1
    assert detail["evidence_summary"]["recent_memory"]
    assert "retrieved_memory" in detail["evidence_summary"]
    assert detail["timeline"]
    assert detail["runbook"]
    assert detail["agent_cards"]
    assert detail["agent_cards"][0]["attach_available"] is False
    assert detail["agent_cards"][0]["terminal_ref"] is None
    assert detail["role_groups"]
    assert detail["governance_summary"]["primary_action"] == "mark_draft_ready"
    assert detail["operator_summary"]["session"]["id"] == session["id"]
    assert detail["operator_summary"]["review_policy"]["policy_name"]
    assert "fallback_snapshot" in detail["operator_summary"]
    assert detail["operator_summary"]["approval_observability"]["approval_state"]["state"] == "drafting"
    assert detail["operator_summary"]["approval_observability"]["usage_cost"]["source"] == "placeholder"
    assert detail["operator_summary"]["compliance_snapshot"]["status"] in {"passed", "warning", "blocked", "unknown"}
    assert detail["operator_summary"]["message_timeline"]
    assert "thread" in detail["operator_summary"]["message_timeline"][0]
    assert detail["plan_tree"]["kind"] == "session"
    assert detail["plan_tree"]["children"]
    assert detail["evidence_summary"]["review_round_count"] >= 1
    assert "job_log" in detail["evidence_summary"]["memory_namespaces"]


def test_dashboard_creates_ideation_session_with_messages(tmp_path) -> None:
    service = _service(tmp_path)

    session = service.create_ideation_session("Explore a multi-agent debate mode")
    detail = service.get_session(str(session["id"]))

    assert detail["session"]["resume"]["current_phase"] == "ideation"
    assert detail["messages"]["count"] >= 5
    assert any(message["from_role"] == "proponent" for message in detail["messages"]["items"])
    groups = {group["layer"]: group for group in detail["role_groups"]}
    assert any(card["role"] == "proponent" for card in groups["decision"]["cards"])


def test_dashboard_job_list_detail_and_missing_log(tmp_path) -> None:
    service = _service(tmp_path)
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    job = runtime.start(
        JobRequest(
            task_id="ui-job",
            provider="codex",
            kind="implementation",
            prompt="Build UI",
            cwd=str(tmp_path),
        )
    )
    runtime.complete(job.id, summary="done", stdout="ok")

    jobs = service.list_jobs()["jobs"]
    detail = service.get_job(job.id)
    missing_log = service.get_job_log("missing-job")

    assert jobs[0]["id"] == job.id
    assert detail["summary"] == "done"
    assert detail["attach_available"] is False
    assert detail["log_available"] is True
    assert detail["output_preview"] == "ok"
    assert detail["last_log_excerpt"]
    assert detail["last_seen_at"]
    assert "ok" in service.get_job_log(job.id)["log"]
    assert missing_log["log"] == ""


def test_dashboard_job_send_cancel_surface_operation_status(tmp_path) -> None:
    service = _service(tmp_path)
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    job = runtime.start(
        JobRequest(
            task_id="ui-job-operation",
            provider="codex",
            kind="implementation",
            prompt="Build UI",
            cwd=str(tmp_path),
        )
    )

    sent = service.send_job(job.id, "continue")
    cancelled = service.cancel_job(job.id)
    missing = service.send_job("missing-job", "continue")

    assert sent["operation"]["status"] == "accepted"
    assert cancelled["operation"]["status"] == "accepted"
    assert missing["operation"]["status"] == "session_missing"


def test_dashboard_job_terminal_input_and_reconnect_surface_status(tmp_path) -> None:
    service = _service(tmp_path)
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    job = runtime.start(
        JobRequest(
            task_id="ui-job-terminal-operation",
            provider="codex",
            kind="implementation",
            prompt="Build UI",
            cwd=str(tmp_path),
            metadata={"terminal_ref": "tmux:agent-job", "attach_available": True},
        )
    )

    sent = service.send_job_terminal_input(job.id, "continue")
    snapshot = service.reconnect_job_terminal(job.id)

    assert sent["operation"]["status"] == "accepted"
    assert snapshot["job_id"] == job.id
    assert snapshot["terminal_ref"] == "tmux:agent-job"


def test_dashboard_job_cards_surface_terminal_metadata(tmp_path) -> None:
    service = _service(tmp_path)
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    job = runtime.start(
        JobRequest(
            task_id="ui-job",
            provider="codex",
            kind="implementation",
            prompt="Build UI",
            cwd=str(tmp_path),
            metadata={"terminal_ref": "tmux:agent-job", "attach_available": True},
        )
    )

    detail = service.get_job(job.id)

    assert detail["terminal_ref"] == "tmux:agent-job"
    assert detail["attach_available"] is True


def test_dashboard_job_terminal_snapshot_surfaces_stdout_and_terminal_ref(tmp_path) -> None:
    service = _service(tmp_path)
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    job = runtime.start(
        JobRequest(
            task_id="ui-job-terminal",
            provider="codex",
            kind="implementation",
            prompt="Build terminal UI",
            cwd=str(tmp_path),
            metadata={"terminal_ref": "tmux:agent-terminal", "attach_available": True},
        )
    )
    runtime.complete(job.id, summary="captured", stdout="pane output")

    snapshot = service.get_job_terminal_snapshot(job.id)

    assert snapshot["job_id"] == job.id
    assert snapshot["terminal_ref"] == "tmux:agent-terminal"
    assert snapshot["attach_available"] is True
    assert snapshot["stdout"] == "pane output"


def test_dashboard_service_can_use_tmux_job_runtime(tmp_path) -> None:
    service = build_dashboard_service(
        plans_root=str(tmp_path / "plans"),
        runs_root=str(tmp_path / "runs"),
        jobs_root=str(tmp_path / "jobs"),
        runtime="tmux",
    )

    assert service.health()["job_runtime"] == "TmuxJobRuntime"


def test_dashboard_actions_execute_and_read_run(tmp_path) -> None:
    service = _service(tmp_path)
    session = service.create_session("Build a persisted plan artifact")
    session = service.mark_draft_ready(str(session["id"]))
    session = service.submit_draft_for_review(str(session["id"]))
    session = service.approve_session(str(session["id"]))

    executed = service.execute_session(str(session["id"]), mode=OrchestrationMode.SUCCESS_FIRST.value)
    run_id = executed["resume"]["linked_execution_run_id"]
    run = service.get_run(run_id)

    assert executed["status"] in {"accepted", "needs_followup"}
    assert run["run_id"] == run_id
    assert run["metadata"]["approved_plan"]["session_id"] == session["id"]
    assert run["metadata"]["execution_contract"]["source"] == "approved_plan_style_direct_run"
    assert any(event["payload"].get("action") == "execute" for event in service.list_session_events(str(session["id"]))["events"])
    assert service.list_events()["events"]
    assert any(record["record_type"] == "action" for record in service.list_session_memory(str(session["id"]))["records"])
    assert service.list_memory()["records"]
    assert service.search_memory("Build persisted")["records"]
    assert service.list_session_messages(str(session["id"]))["messages"]

    detail = service.get_session(str(session["id"]))
    operator = detail["operator_summary"]
    assert operator["execution_provenance"]["plan_session_id"] == session["id"]
    assert operator["execution_provenance"]["linked_run_status"] in {"completed", "blocked"}
    assert operator["work_graph_summary"]["node_count"] >= 1
    assert service.list_messages()["messages"]


def test_dashboard_rejects_unavailable_session_action(tmp_path) -> None:
    service = _service(tmp_path)
    session = service.create_session("Build a persisted plan artifact")

    try:
        service.approve_session(str(session["id"]))
    except ValueError as exc:
        assert "不允许执行" in str(exc)
    else:
        raise AssertionError("approve_session should reject an approved session")


def test_dashboard_sessions_empty_when_index_missing(tmp_path) -> None:
    service = _service(tmp_path)

    assert service.list_sessions() == {"sessions": []}
    assert service.list_jobs() == {"jobs": []}


def test_dashboard_role_groups_map_session_jobs_to_layers(tmp_path) -> None:
    service = _service(tmp_path)
    session = service.create_session("Build a persisted plan artifact")

    detail = service.get_session(str(session["id"]))
    groups = {group["layer"]: group for group in detail["role_groups"]}
    review_cards = groups["review"]["cards"]
    decision_cards = groups["decision"]["cards"]
    runtime_cards = groups["runtime"]["cards"]

    assert decision_cards[0]["role"] == "lead"
    assert decision_cards[0]["layer_label"] == "决策层"
    assert any(card["role"] == "reviewer" for card in review_cards)
    assert any(card["role"] == "adversarial_reviewer" for card in review_cards)
    assert runtime_cards[0]["role"] == "runtime"
    assert review_cards[0]["attach_available"] is False
    assert review_cards[0]["terminal_ref"] is None


def test_dashboard_governance_summary_surfaces_topology_and_recovery(tmp_path) -> None:
    service = _service(tmp_path)
    session = service.create_session("Build a persisted plan artifact")

    summary = service.get_session(str(session["id"]))["governance_summary"]

    assert summary["selected_topology"]
    assert isinstance(summary["selected_provider_runtime"], dict)
    assert summary["primary_action"] == "mark_draft_ready"
    assert isinstance(summary["blocking"], bool)
    assert isinstance(summary["recovery_actions"], list)
    assert summary["recovery_action_count"] == len(summary["recovery_actions"])
    assert summary["gate_status"] in {"open", "approved", "blocked", "needs_revision", "completed"}
    assert summary["review_intensity"] in {"standard", "reviewed", "strict"}
    assert isinstance(summary["recommended_commands"], list)
    assert summary["recommended_command_count"] == len(summary["recommended_commands"])
    assert "compliance_status" in summary


def test_dashboard_plan_tree_includes_subtasks_rounds_and_execution(tmp_path) -> None:
    service = _service(tmp_path)
    session = service.create_session("Build a persisted plan artifact")
    detail = service.get_session(str(session["id"]))
    children = detail["plan_tree"]["children"]

    assert any(node["kind"] == "subtask" for node in children)
    review_nodes = [node for node in children if node["kind"] == "review_round"]
    assert review_nodes
    assert any(node["related_agent_ids"] for node in review_nodes)

    session = service.mark_draft_ready(str(session["id"]))
    session = service.submit_draft_for_review(str(session["id"]))
    session = service.approve_session(str(session["id"]))
    executed = service.execute_session(str(session["id"]), mode=OrchestrationMode.SUCCESS_FIRST.value)
    executed_detail = service.get_session(str(executed["id"]))
    executed_children = executed_detail["plan_tree"]["children"]

    assert any(node["kind"] == "execution_run" for node in executed_children)
    assert any(action["id"] == "inspect_execution" and action["enabled"] for action in executed_detail["actions"])


def test_dashboard_falls_back_when_work_graph_is_missing(tmp_path) -> None:
    service = _service(tmp_path)
    session = service.create_session("Build a persisted plan artifact")
    graph_path = Path(tmp_path / "plans" / str(session["id"]) / "work_graph.json")
    graph_path.unlink()

    detail = service.get_session(str(session["id"]))

    assert detail["work_graph"] is None
    assert detail["plan_tree"]["kind"] == "session"
    assert detail["plan_tree"]["children"]


def test_dashboard_role_groups_prefer_persisted_work_graph(tmp_path) -> None:
    service = _service(tmp_path)
    session = service.create_session("Build a persisted plan artifact")

    detail = service.get_session(str(session["id"]))
    groups = {group["layer"]: group for group in detail["role_groups"]}

    assert detail["work_graph"]["session_id"] == session["id"]
    assert "schedulable_nodes" in detail["work_graph"]
    assert any(card["role"] == "builder" for card in groups["execution"]["cards"])
    lead_cards = [card for card in groups["decision"]["cards"] if card["role"] == "lead"]
    assert lead_cards
    assert lead_cards[0]["outbox_count"] >= 1
    assert lead_cards[0]["latest_message_summary"]
    assert any(card["role"] == "runtime" for card in groups["runtime"]["cards"])

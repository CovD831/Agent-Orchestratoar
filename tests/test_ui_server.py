from fastapi.testclient import TestClient

from agent_orchestrator import Orchestrator
from agent_orchestrator.jobs import FileJobRuntime, JobRequest
from agent_orchestrator.planning import PlanStore, TeamOrchestrator
from agent_orchestrator.run_store import RunStore
from agent_orchestrator.ui_server import create_app
from agent_orchestrator.ui_service import DashboardService


def _client(tmp_path, runtime: FileJobRuntime | None = None):
    runtime = runtime or FileJobRuntime(root=tmp_path / "jobs")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
        project_root=tmp_path,
    )
    team.orchestrator.run_store = RunStore(root=tmp_path / "runs")
    service = DashboardService(
        team=team,
        plans_root=tmp_path / "plans",
        runs_root=tmp_path / "runs",
        jobs_root=tmp_path / "jobs",
        job_runtime=runtime,
    )
    return TestClient(create_app(service)), service


def test_global_stream_returns_event_stream_frames(tmp_path) -> None:
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    client, service = _client(tmp_path, runtime=runtime)
    session = service.create_session("Build dashboard")
    job = runtime.start(
        JobRequest(
            task_id="ui-stream-job",
            provider="mock",
            kind="review",
            prompt="stream",
            cwd=str(tmp_path),
        )
    )
    runtime.complete(job.id, summary="done", stdout="ok")

    response = client.get("/api/stream?once=true")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: orchestration_event" in response.text
    assert "event: team_message" in response.text
    assert "event: job_update" in response.text


def test_session_stream_returns_session_scoped_frames(tmp_path) -> None:
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    client, service = _client(tmp_path, runtime=runtime)
    session = service.create_session("Build dashboard")
    job = runtime.start(
        JobRequest(
            task_id=f"{session['id']}:review",
            provider="mock",
            kind="review",
            prompt="stream",
            cwd=str(tmp_path),
        )
    )
    runtime.complete(job.id, summary="done", stdout="ok")

    response = client.get(f"/api/sessions/{session['id']}/stream?once=true")

    assert response.status_code == 200
    assert "team_message" in response.text
    assert "job_update" in response.text
    assert str(session["id"]) in response.text


def test_memory_search_endpoint_returns_records(tmp_path) -> None:
    client, service = _client(tmp_path)
    service.create_session("Build dashboard")

    response = client.get("/api/memory/search?q=dashboard")

    assert response.status_code == 200
    assert response.json()["records"]


def test_agent_config_endpoint_round_trips_profiles(tmp_path) -> None:
    client, _service = _client(tmp_path)

    payload = client.get("/api/agent-config").json()
    payload["profiles"]["worker"]["provider"] = "claude"
    payload["profiles"]["worker"]["model"] = "opus"
    payload["profiles"]["worker"]["prompt_template"] = "Worker UI: {default_prompt}"

    response = client.post("/api/agent-config", json=payload)

    assert response.status_code == 200
    saved = client.get("/api/agent-config").json()
    assert saved["profiles"]["worker"]["provider"] == "claude"
    assert saved["profiles"]["worker"]["model"] == "opus"
    assert saved["profiles"]["worker"]["prompt_template"] == "Worker UI: {default_prompt}"


def test_index_contains_operator_and_job_control_mounts(tmp_path) -> None:
    client, _service = _client(tmp_path)

    response = client.get("/")

    assert response.status_code == 200
    assert (
        'id="operator-summary"' in response.text
        or 'id="root"' in response.text
    )
    assert (
        'id="job-actions"' in response.text
        or 'src="/static/' in response.text
        or 'src="/src/main.jsx"' in response.text
    )


def test_job_terminal_snapshot_endpoint_returns_snapshot(tmp_path) -> None:
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    client, _service = _client(tmp_path, runtime=runtime)
    job = runtime.start(
        JobRequest(
            task_id="ui-terminal-job",
            provider="codex",
            kind="implementation",
            prompt="Build terminal UI",
            cwd=str(tmp_path),
            metadata={"terminal_ref": "tmux:agent-ui", "attach_available": True},
        )
    )
    runtime.complete(job.id, summary="done", stdout="pane output")

    response = client.get(f"/api/jobs/{job.id}/terminal/snapshot")

    assert response.status_code == 200
    payload = response.json()
    assert payload["job_id"] == job.id
    assert payload["terminal_ref"] == "tmux:agent-ui"
    assert payload["stdout"] == "pane output"


def test_job_terminal_input_and_reconnect_endpoints(tmp_path) -> None:
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    client, _service = _client(tmp_path, runtime=runtime)
    job = runtime.start(
        JobRequest(
            task_id="ui-terminal-job-ops",
            provider="codex",
            kind="implementation",
            prompt="Build terminal UI",
            cwd=str(tmp_path),
            metadata={"terminal_ref": "tmux:agent-ui", "attach_available": True},
        )
    )

    send_response = client.post(f"/api/jobs/{job.id}/terminal/input", json={"message": "continue"})
    reconnect_response = client.post(f"/api/jobs/{job.id}/terminal/reconnect")

    assert send_response.status_code == 200
    assert send_response.json()["operation"]["status"] == "accepted"
    assert reconnect_response.status_code == 200
    assert reconnect_response.json()["job_id"] == job.id

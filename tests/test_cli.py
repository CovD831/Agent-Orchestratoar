# DEPS: agent_orchestrator, datetime, json, os, pathlib, pytest
# RESPONSIBILITY: 待补充
# MODULE: 待确定
# ---

import json
import os
from pathlib import Path
from datetime import UTC, datetime
import pytest

from agent_orchestrator import OrchestrationMode
from agent_orchestrator.command import CommandResult
from agent_orchestrator.cli import _print_run_summary
from agent_orchestrator.jobs import FileJobRuntime, JobRequest
from agent_orchestrator.orchestrator import Orchestrator
from agent_orchestrator.routing import PolicyRouter
from test_support import start_reviewed_session, write_minimal_process_docs


def _cli_session(team, requirement: str):
    session = start_reviewed_session(team, requirement)
    lowered = requirement.lower()
    if "architecture direction" in lowered:
        session.status = "awaiting_human"
        session.resume.current_phase = "awaiting_human"
        session.resume.pending_role = "human"
        team.store.write_session(session)
    elif "auth migration" in lowered and "roadmap drift" in lowered:
        session.status = "blocked"
        session.resume.current_phase = "blocked"
        session.resume.pending_role = "human"
        team.store.write_session(session)
    elif session.gaps:
        session.status = "needs_revision"
        session.resume.current_phase = "in_review"
        session.resume.pending_role = "lead"
        team.store.write_session(session)
    elif session.status != "approved_for_execution":
        session = team.approve(session.id)
    return session


class _FakeClaudeRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def spawn(self, command: list[str], *, cwd: str, env: dict[str, str] | None = None):
        self.commands.append(command)
        return None

    def run(self, command: list[str], *, cwd: str, env: dict[str, str] | None = None) -> CommandResult:
        self.commands.append(command)
        if "--version" in command:
            return CommandResult(command=command, exit_code=0, stdout="claude 1.0.0\n", stderr="")
        return CommandResult(
            command=command,
            exit_code=0,
            stdout=json.dumps({"result": "Claude review complete", "is_error": False}),
            stderr="",
        )


def test_auto_mode_high_risk_routes_to_success_first() -> None:
    run = Orchestrator(router=PolicyRouter()).run("Urgent auth refactor today", None)

    assert run.routing_decision is not None
    assert run.routing_decision.mode.value == "success_first"


def test_provider_health_snapshot_includes_mock_binary_and_fallback_fields() -> None:
    from agent_orchestrator import cli

    payload = cli._provider_health_snapshot()
    providers = {item["provider"]: item for item in payload["providers"]}

    assert payload["cache"]["tiers"] == ["memory", "disk", "live"]
    assert {item["mode"] for item in payload["runtime_modes"]} == {"cli_inherit", "cli_isolated", "direct_api"}
    assert {"codex", "claude", "mock"} <= set(providers)
    assert "binary" in providers["codex"]
    assert "recommended_fallback" in providers["claude"]
    assert "cache_tier" in providers["codex"]
    assert providers["mock"]["available"] is True


def test_print_run_summary_reports_reroute(capsys) -> None:
    run = Orchestrator().run("Fail the auth migration", OrchestrationMode.SPEED_FIRST)

    _print_run_summary(run)
    out = capsys.readouterr().out

    assert "rerouted:" in out
    assert "attempts=2" in out
    assert "reasons=" in out
    assert "upgrade=mode_upgrade" in out


def test_print_run_summary_reports_partial_rescue_without_reroute(capsys) -> None:
    run = Orchestrator().run("Fail task", OrchestrationMode.SUCCESS_FIRST)

    _print_run_summary(run)
    out = capsys.readouterr().out

    assert "dependency_rescue:" in out
    assert "accepted=true" in out
    assert "rerouted:" not in out


def test_run_to_dict_preserves_reroute_history() -> None:
    run = Orchestrator().run("Fail the auth migration", OrchestrationMode.SPEED_FIRST)
    payload = run.to_dict()

    assert payload["attempts"]
    assert payload["reroute_history"]
    assert payload["attempts"][0]["failure_decision"] is not None
    assert payload["reroute_history"][0]["upgrade_kind"] == "mode_upgrade"
    assert json.loads(json.dumps(payload))["final_mode"] == "success_first"


def test_run_to_dict_preserves_partial_rescue_history() -> None:
    run = Orchestrator().run("Fail task", OrchestrationMode.SUCCESS_FIRST)
    payload = run.to_dict()

    assert payload["attempts"][0]["dependency_rescue_results"]
    assert payload["attempts"][0]["replayed_work_unit_ids"]


def test_job_status_and_result_commands_round_trip(tmp_path, capsys) -> None:
    runtime = FileJobRuntime(tmp_path)
    job = runtime.start(
        JobRequest(
            task_id="work-cli",
            provider="codex",
            kind="implementation",
            prompt="CLI",
            cwd=str(tmp_path),
        )
    )
    runtime.complete(job.id, summary="cli done", stdout="ok")
    runtime.send(job.id, "ignored")

    from agent_orchestrator import cli

    original = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="status", job_id=job.id, root=str(Path(tmp_path))
        )
        cli.main()
        status_out = capsys.readouterr().out
        assert status_out.startswith("job_status:")
        assert "operation=already_terminal" in status_out
        assert "last_seen=" in status_out
        assert "job_log_excerpt: ok" in status_out
        status_payload = json.loads("\n".join(status_out.splitlines()[2:]))
        assert status_payload["id"] == job.id
        assert "session_id" in status_payload
        assert "thread_id" in status_payload

        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="result", job_id=job.id, root=str(Path(tmp_path))
        )
        cli.main()
        result_out = capsys.readouterr().out
        assert result_out.startswith("job_result:")
        assert "job_log_excerpt: ok" in result_out
        result_payload = json.loads("\n".join(result_out.splitlines()[2:]))
        assert result_payload["job_id"] == job.id
        assert result_payload["summary"] == "cli done"
    finally:
        cli.argparse.ArgumentParser.parse_args = original


def test_job_status_json_format_outputs_pure_json(tmp_path, capsys) -> None:
    runtime = FileJobRuntime(tmp_path)
    job = runtime.start(
        JobRequest(
            task_id="work-cli-json",
            provider="codex",
            kind="implementation",
            prompt="CLI",
            cwd=str(tmp_path),
        )
    )

    from agent_orchestrator import cli

    original = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="status",
            job_id=job.id,
            root=str(Path(tmp_path)),
            format="json",
        )
        cli.main()
        out = capsys.readouterr().out
        assert not out.startswith("job_status:")
        assert json.loads(out)["id"] == job.id
    finally:
        cli.argparse.ArgumentParser.parse_args = original


def test_health_json_format_outputs_pure_json(capsys) -> None:
    from agent_orchestrator import cli

    original = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="health",
            refresh=False,
            cache_ttl=60,
            format="json",
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["providers"]
        assert payload["cache"]["enabled"] is True
    finally:
        cli.argparse.ArgumentParser.parse_args = original



def test_team_setup_reports_readiness_and_recommendations(capsys, tmp_path, monkeypatch) -> None:
    from agent_orchestrator import cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('version = "1.0.0rc1"\n', encoding="utf-8")
    (tmp_path / "README.md").write_text("# Agent Orchestrator\n", encoding="utf-8")
    (tmp_path / "docs/process").mkdir(parents=True)
    (tmp_path / "docs/architecture").mkdir(parents=True)
    for name in [
        "长周期主执行计划.md",
        "agent-orchestrator-implementation-process.md",
        "agent-team-operator-runbook.md",
        "root-map.md",
        "module-manifest.md",
        "file-header-contract.md",
    ]:
        (tmp_path / "docs/process" / name).write_text("# Stub\n- stub\n", encoding="utf-8")
    (tmp_path / "docs/architecture" / "决策核心-执行拓扑-运行时分层说明.md").write_text("# Stub\n- stub\n", encoding="utf-8")
    (tmp_path / "docs/process" / "v1x-evidence-report.md").write_text("# Stub\n", encoding="utf-8")
    (tmp_path / "docs/process" / "v1x-evidence-trend.md").write_text("# Stub\n", encoding="utf-8")
    (tmp_path / "docs/process" / "evidence-cases.json").write_text("[]", encoding="utf-8")

    team_payload = {}

    class FakeTeam:
        def refresh_documentation_sync(self):
            return {"refresh_results": [{"path": "docs/process/root-map.md", "status": "passed"}], "doc_sync": True}

        def check_compliance(self):
            return {
                "blocking": False,
                "warnings": [],
                "blocking_reasons": [],
                "required_actions": [],
                "recommended_commands": ["python -m agent_orchestrator.cli team check-compliance"],
            }

    original = cli.argparse.ArgumentParser.parse_args
    original_builder = cli._build_team_orchestrator
    try:
        cli._build_team_orchestrator = lambda *args, **kwargs: FakeTeam()
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="setup",
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
            jobs_root=str(tmp_path / "jobs"),
            runtime="mock",
            provider=None,
            format="pretty",
        )
        cli.main()
        output = capsys.readouterr().out
        assert "setup: ready=yes release_ready=yes compliance=ok" in output
        assert "providers: available=" in output
        assert "runtime_modes: " in output
        assert "release_checklist: version_sync=ok" in output
        out = json.loads(output[output.index("{") :])
        assert out["provider_health"]["providers"]
        assert out["runtime_modes"]
        assert out["role_profiles"]
        assert {profile["runtime_mode"] for profile in out["role_profiles"]} >= {"cli_inherit", "direct_api"}
        assert out["readiness"]["ready"] is True
        assert out["readiness"]["provider_states"]
        assert out["release_readiness"]["version_sync"]["package_version"] == "1.0.0rc1"
        assert out["release_readiness"]["checklist"]["version_sync"] is True
        assert out["release_readiness"]["evidence_state"]["benchmark_report_present"] is True
        assert out["recommended_commands"][0].endswith("team check-compliance")
    finally:
        cli._build_team_orchestrator = original_builder
        cli.argparse.ArgumentParser.parse_args = original


def test_team_setup_json_mode_remains_machine_readable(capsys, tmp_path, monkeypatch) -> None:
    from agent_orchestrator import cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('version = "1.0.0rc1"\n', encoding="utf-8")
    (tmp_path / "docs/process").mkdir(parents=True)
    (tmp_path / "docs/process" / "v1x-evidence-report.md").write_text("# Stub\n", encoding="utf-8")
    (tmp_path / "docs/process" / "v1x-evidence-trend.md").write_text("# Stub\n", encoding="utf-8")
    (tmp_path / "docs/process" / "evidence-cases.json").write_text("[]", encoding="utf-8")

    class FakeTeam:
        def refresh_documentation_sync(self):
            return {"missing_docs": [], "stale_docs": [], "header_contract_violations": []}

        def check_compliance(self):
            return {"blocking": False, "warnings": [], "blocking_reasons": [], "required_actions": [], "recommended_commands": []}

    original = cli.argparse.ArgumentParser.parse_args
    original_builder = cli._build_team_orchestrator
    try:
        cli._build_team_orchestrator = lambda *args, **kwargs: FakeTeam()
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="setup",
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
            jobs_root=str(tmp_path / "jobs"),
            runtime="mock",
            provider=None,
            format="json",
        )
        cli.main()
        output = capsys.readouterr().out
        assert output.lstrip().startswith("{")
        payload = json.loads(output)
        assert payload["release_readiness"]["version_sync"]["package_version"] == "1.0.0rc1"
    finally:
        cli._build_team_orchestrator = original_builder
        cli.argparse.ArgumentParser.parse_args = original

def test_async_run_returns_handle(tmp_path, capsys) -> None:
    from agent_orchestrator import cli

    original = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="run",
            requirement="Build dashboard",
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            async_run=True,
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["run_id"]
        assert payload["status"] in {"queued", "running"}
        assert payload["job_ids"] == []
    finally:
        cli.argparse.ArgumentParser.parse_args = original


def test_lock_status_command_reports_metadata(tmp_path, capsys) -> None:
    from agent_orchestrator import cli

    orchestrator = Orchestrator()
    orchestrator.run_store.root = tmp_path
    run = orchestrator.run("Build dashboard", OrchestrationMode.SUCCESS_FIRST)
    lock_path = tmp_path / f"{run.run_id}.lock"
    lock_path.write_text(
        json.dumps(
            {
                "run_id": run.run_id,
                "pid": os.getpid(),
                "owner": "orchestrator",
                "reason": "cli-test",
                "started_at": datetime.now(UTC).isoformat(),
                "heartbeat_at": datetime.now(UTC).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    original = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="lock-status",
            run_id=run.run_id,
            root=str(Path(tmp_path)),
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["run_id"] == run.run_id
        assert payload["owner"] == "orchestrator"
        assert payload["reason"] == "cli-test"
        assert payload["state"] == "active"
    finally:
        cli.argparse.ArgumentParser.parse_args = original


def test_print_run_summary_reports_agent_depth(capsys) -> None:
    run = Orchestrator().run("Build dashboard", OrchestrationMode.SUCCESS_FIRST, depth=2)

    _print_run_summary(run)
    out = capsys.readouterr().out

    assert "agent=on" in out
    assert "depth=2" in out


def test_print_run_summary_reports_depth_upgrade(capsys) -> None:
    run = Orchestrator().run("Fail the auth migration", OrchestrationMode.SUCCESS_FIRST, depth=1)

    _print_run_summary(run)
    out = capsys.readouterr().out

    assert "rerouted:" in out
    assert "upgrade=depth_upgrade" in out


def test_print_run_summary_reports_decision_contract(capsys) -> None:
    run = Orchestrator().run("Build dashboard", OrchestrationMode.SUCCESS_FIRST)

    _print_run_summary(run)
    out = capsys.readouterr().out

    assert "decision:" in out
    assert "route=success_first" in out
    assert "review=required" in out
    assert "route_source=explicit_mode" in out
    assert "execution_contract:" in out
    assert "source=approved_plan_style_direct_run" in out
    assert "goal=Build dashboard" in out


def test_print_run_summary_reports_router_source_and_execution_contract(capsys) -> None:
    run = Orchestrator().run("Implement multiple independent modules in parallel", None)

    _print_run_summary(run)
    out = capsys.readouterr().out

    assert "route_source=router" in out
    assert "execution_contract:" in out


def test_poll_run_command_returns_execution_contract_metadata(tmp_path, capsys) -> None:
    from agent_orchestrator import cli

    orchestrator = Orchestrator()
    orchestrator.run_store.root = tmp_path
    run = orchestrator.run("Build dashboard", OrchestrationMode.SUCCESS_FIRST)

    original_build = cli._build_orchestrator
    original_parse = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_orchestrator = lambda runtime, provider: orchestrator
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="poll-run",
            run_id=run.run_id,
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["metadata"]["entrypoint"] == "direct_run"
        assert payload["metadata"]["execution_contract"]["source"] == "approved_plan_style_direct_run"
    finally:
        cli._build_orchestrator = original_build
        cli.argparse.ArgumentParser.parse_args = original_parse


def test_ui_command_starts_dashboard_server(monkeypatch, capsys) -> None:
    from agent_orchestrator import cli

    calls = {}

    class _FakeApp:
        def __init__(self, *args, **kwargs):
            pass

        def mount(self, *args, **kwargs):
            return None

        def get(self, *args, **kwargs):
            return lambda fn: fn

        def post(self, *args, **kwargs):
            return lambda fn: fn

    class _FakeHTTPException(Exception):
        def __init__(self, *, status_code, detail):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class _FakeBaseModel:
        pass

    class _FakeUvicorn:
        @staticmethod
        def run(app, *, host, port):
            calls["app"] = app
            calls["host"] = host
            calls["port"] = port

    monkeypatch.setitem(
        __import__("sys").modules,
        "fastapi",
        type("FastApiModule", (), {"Body": lambda default=None, **kwargs: default, "FastAPI": _FakeApp, "HTTPException": _FakeHTTPException}),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "fastapi.responses",
        type("ResponsesModule", (), {"FileResponse": lambda path: path, "StreamingResponse": lambda body, media_type=None: body}),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "fastapi.staticfiles",
        type("StaticFilesModule", (), {"StaticFiles": lambda directory: directory}),
    )
    monkeypatch.setitem(__import__("sys").modules, "pydantic", type("PydanticModule", (), {"BaseModel": _FakeBaseModel}))
    monkeypatch.setitem(__import__("sys").modules, "uvicorn", _FakeUvicorn)
    monkeypatch.setattr(
        cli,
        "_build_team_orchestrator",
        lambda runtime, provider, plans_root, runs_root: __import__(
            "agent_orchestrator.planning",
            fromlist=["TeamOrchestrator", "PlanStore"],
        ).TeamOrchestrator(
            orchestrator=__import__("agent_orchestrator").Orchestrator(),
            store=__import__("agent_orchestrator.planning", fromlist=["PlanStore"]).PlanStore(root=plans_root),
        ),
    )

    original_parse = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="ui",
            host="127.0.0.1",
            port=8765,
            plans_root=".agent_orchestrator/plans",
            runs_root=".agent_orchestrator/runs",
            jobs_root=".agent_orchestrator/jobs",
            runtime="mock",
            job_runtime="mock",
            provider=None,
        )
        cli.main()
    finally:
        cli.argparse.ArgumentParser.parse_args = original_parse

    assert calls["host"] == "127.0.0.1"
    assert calls["port"] == 8765
    assert "Agent Team Console: http://127.0.0.1:8765" in capsys.readouterr().out


def test_ui_command_accepts_tmux_job_runtime(monkeypatch, capsys) -> None:
    from agent_orchestrator import cli

    calls = {}

    class _FakeApp:
        def __init__(self, *args, **kwargs):
            pass

        def mount(self, *args, **kwargs):
            return None

        def get(self, *args, **kwargs):
            return lambda fn: fn

        def post(self, *args, **kwargs):
            return lambda fn: fn

    class _FakeHTTPException(Exception):
        def __init__(self, *, status_code, detail):
            super().__init__(detail)

    class _FakeUvicorn:
        @staticmethod
        def run(app, *, host, port):
            calls["host"] = host
            calls["port"] = port

    monkeypatch.setitem(
        __import__("sys").modules,
        "fastapi",
        type("FastApiModule", (), {"Body": lambda default=None, **kwargs: default, "FastAPI": _FakeApp, "HTTPException": _FakeHTTPException}),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "fastapi.responses",
        type("ResponsesModule", (), {"FileResponse": lambda path: path, "StreamingResponse": lambda body, media_type=None: body}),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "fastapi.staticfiles",
        type("StaticFilesModule", (), {"StaticFiles": lambda directory: directory}),
    )
    monkeypatch.setitem(__import__("sys").modules, "uvicorn", _FakeUvicorn)
    monkeypatch.setattr(
        cli,
        "_build_team_orchestrator",
        lambda runtime, provider, plans_root, runs_root: __import__(
            "agent_orchestrator.planning",
            fromlist=["TeamOrchestrator", "PlanStore"],
        ).TeamOrchestrator(
            orchestrator=__import__("agent_orchestrator").Orchestrator(),
            store=__import__("agent_orchestrator.planning", fromlist=["PlanStore"]).PlanStore(root=plans_root),
        ),
    )

    original_parse = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="ui",
            host="127.0.0.1",
            port=8765,
            plans_root=".agent_orchestrator/plans",
            runs_root=".agent_orchestrator/runs",
            jobs_root=".agent_orchestrator/jobs",
            runtime="mock",
            job_runtime="tmux",
            provider=None,
        )
        cli.main()
    finally:
        cli.argparse.ArgumentParser.parse_args = original_parse

    assert calls == {"host": "127.0.0.1", "port": 8765}


def test_team_status_command_round_trips_session(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = _cli_session(team, "Build a persisted plan artifact")

    original = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="status",
            session_id=session.id,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
            context_policy="resume_if_same_task",
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["id"] == session.id
        assert payload["status"] == "approved_for_execution"
        assert payload["structured_brief"]["goal"]
        assert payload["structured_brief"]["subtasks"]
        assert payload["status_summary"]["next_actions"] == ["execute"]
        assert payload["status_summary"]["approval_state"]["state"] == "approved"
        assert payload["status_summary"]["runtime_health"]["job_count"] >= 1
        assert payload["status_summary"]["usage_cost"]["source"] == "placeholder"
    finally:
        cli.argparse.ArgumentParser.parse_args = original


def test_team_resume_command_normalizes_session_state(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = _cli_session(team, "Build plan with adversarial challenge")
    session.resume.current_phase = "drafting"
    session.resume.pending_role = "build"
    team.store.write_session(session)

    original = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="resume",
            session_id=session.id,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["resume"]["current_phase"] == "in_review"
        assert payload["resume"]["pending_role"] == "lead"
        assert "revise" in payload["status_summary"]["next_actions"]
        assert payload["status_summary"]["resume_action"] == "revise"
        assert payload["status_summary"]["resume_reason"] == "required_gaps_open"
    finally:
        cli.argparse.ArgumentParser.parse_args = original


def test_team_revise_command_closes_required_gap(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = _cli_session(team, "Build plan with adversarial challenge")
    gap_id = session.gaps[0].id

    original = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="revise",
            session_id=session.id,
            summary="closed required gap",
            close_gap=[gap_id],
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["review_rounds"][-1]["round_type"] == "revision"
        assert payload["gaps"][0]["status"] == "closed"
        status_summary = payload["status_summary"]
        assert status_summary["open_required_gaps"] == 0
        assert status_summary["primary_action"] in {"approve", "inspect_compliance"}
        if status_summary["primary_action"] == "inspect_compliance":
            assert status_summary["resume_reason"] in {
                "compliance_blocking",
                "compliance_warning_only",
            }
        else:
            assert "approve" in status_summary["next_actions"]
    finally:
        cli.argparse.ArgumentParser.parse_args = original


def test_team_status_command_reports_claude_job_summary(tmp_path, capsys) -> None:
    from agent_orchestrator import cli

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args

    class _FakeTeam:
        def status(self, session_id: str):
            return type(
                "FakeSession",
                (),
                {
                    "to_dict": lambda self: {
                        "id": session_id,
                        "status": "needs_revision",
                        "structured_brief": {"goal": "g", "subtasks": []},
                        "status_summary": {
                            "next_actions": ["revise"],
                            "delegated_jobs": [
                                {
                                    "provider": "claude",
                                    "kind": "review",
                                    "status": "completed",
                                    "summary": "Claude review complete",
                                }
                            ],
                        },
                    }
                },
            )()

    try:
        cli._build_team_orchestrator = lambda runtime, provider, plans_root, runs_root: _FakeTeam()
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="status",
            session_id="plan-1",
            requirement=None,
            mode="success_first",
            runtime="command",
            reroute="on",
            provider="claude",
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["status_summary"]["delegated_jobs"][0]["provider"] == "claude"
        assert payload["status_summary"]["delegated_jobs"][0]["status"] == "completed"
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_summary_command_reports_primary_next_step(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = _cli_session(team, "Build plan with adversarial challenge")

    original = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="summary",
            session_id=session.id,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert f"session: {session.id}" in out
        assert "status: needs_revision" in out
        assert "next: revise" in out
        assert "topology_reason:" in out
        assert "blocking:" in out
    finally:
        cli.argparse.ArgumentParser.parse_args = original


def test_team_summary_command_prioritizes_failed_claude_job(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.jobs import FileJobRuntime
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    runtime = FileJobRuntime(root=tmp_path / "jobs")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
    )
    session = _cli_session(team, "Build a persisted plan artifact")
    review_round = session.review_rounds[1]
    review_job_id = review_round.summary.split("job ")[-1].rstrip(".")
    runtime.fail(review_job_id, summary="review failed", error="claude auth failed")

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="summary",
            session_id=session.id,
            requirement=None,
            mode="success_first",
            runtime="command",
            reroute="on",
            provider="claude",
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert "next: retry_review" in out
        assert f"failed_job: claude {review_job_id}" in out
        assert "inspect the failed Claude job" in out
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_summary_command_reports_execute_for_approved_session(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = _cli_session(team, "Build a persisted plan artifact")

    original = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="summary",
            session_id=session.id,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert "status: approved_for_execution" in out
        assert "next: execute" in out
    finally:
        cli.argparse.ArgumentParser.parse_args = original


def test_team_summary_command_reports_human_decision_for_awaiting_human_session(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = _cli_session(team, "Architecture direction change for stage transition")

    original = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="summary",
            session_id=session.id,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert "status: awaiting_human" in out
        assert "next: human_decision" in out
    finally:
        cli.argparse.ArgumentParser.parse_args = original


def test_team_next_command_reports_revise_command(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = _cli_session(team, "Build plan with adversarial challenge")

    original = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="next",
            session_id=session.id,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert "next_command: python -m agent_orchestrator.cli team revise" in out
        assert session.id in out
        assert "--summary" in out
    finally:
        cli.argparse.ArgumentParser.parse_args = original


def test_team_next_command_reports_execute_command(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = _cli_session(team, "Build a persisted plan artifact")

    original = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="next",
            session_id=session.id,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert "next_command: python -m agent_orchestrator.cli team execute" in out
        assert "--mode success_first" in out
        assert "alternatives: none" in out
    finally:
        cli.argparse.ArgumentParser.parse_args = original


def test_team_next_command_reports_next_task_for_intake_session(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = team.start("Build a persisted plan artifact")

    original = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="next",
            session_id=session.id,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert "next_task:" in out
        assert "action=mark_draft_ready" in out
        assert "Draft confirmed by human" in out
    finally:
        cli.argparse.ArgumentParser.parse_args = original


def test_team_task_commands_list_next_and_done(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = team.start("Build a persisted plan artifact")
    draft_task = next(item for item in session.checklist if item.label == "Draft confirmed by human")

    original = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="task",
            task_command="list",
            session_id=session.id,
            task_id=None,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
            format="json",
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["next_executable_task"]["title"] == "Draft confirmed by human"
        assert len(payload["tasks"]) == 4

        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="task",
            task_command="next",
            session_id=session.id,
            task_id=None,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
            format="json",
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["next_executable_task"]["next_action"] == "mark_draft_ready"

        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="task",
            task_command="done",
            session_id=session.id,
            task_id=draft_task.id,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
            format="json",
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["task"]["status"] == "done"
        assert payload["next_executable_task"]["title"] == "Review round completed"
    finally:
        cli.argparse.ArgumentParser.parse_args = original


def test_team_roles_command_reports_role_contracts(tmp_path, capsys) -> None:
    from agent_orchestrator import cli

    original = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="roles",
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
            format="json",
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        roles = {role["role"]: role for role in payload["roles"]}
        assert roles["reviewer"]["runtime_mode"] == "direct_api"
        assert "execute_work_unit" in roles["reviewer"]["forbidden_actions"]
        assert "implementation_result" in roles["builder"]["required_outputs"]
    finally:
        cli.argparse.ArgumentParser.parse_args = original


def test_team_next_command_reports_failed_job_inspection_first(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.jobs import FileJobRuntime
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    runtime = FileJobRuntime(root=tmp_path / "jobs")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
    )
    session = _cli_session(team, "Build a persisted plan artifact")
    review_round = session.review_rounds[1]
    review_job_id = review_round.summary.split("job ")[-1].rstrip(".")
    runtime.fail(review_job_id, summary="review failed", error="claude auth failed")

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="next",
            session_id=session.id,
            requirement=None,
            mode="success_first",
            runtime="command",
            reroute="on",
            provider="claude",
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert "next_command: python -m agent_orchestrator.cli team retry-review" in out
        assert session.id in out
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_summary_command_reports_recovery_actions_for_failed_claude_job(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.jobs import FileJobRuntime
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    runtime = FileJobRuntime(root=tmp_path / "jobs")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
    )
    session = _cli_session(team, "Build a persisted plan artifact")
    review_round = session.review_rounds[1]
    review_job_id = review_round.summary.split("job ")[-1].rstrip(".")
    runtime.fail(review_job_id, summary="review failed", error="claude auth failed")

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="summary",
            session_id=session.id,
            requirement=None,
            mode="success_first",
            runtime="command",
            reroute="on",
            provider="claude",
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert "recovery: inspect_delegated_job -> retry_review -> revise_plan" in out
        assert "recovery_provider: claude (round=review, mode=planned)" in out
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_summary_command_reports_fallback_recovery_provider_policy(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.command import ClaudeCodeAdapter, CommandJobRuntime, ProviderStatus
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    runtime = CommandJobRuntime(
        root=tmp_path / "jobs",
        runner=_FakeClaudeRunner(),
        adapters={"claude": ClaudeCodeAdapter()},
    )
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
    )
    team.provider_health_check = lambda provider: ProviderStatus(
        provider=provider,
        available=False,
        detail=f"{provider} unavailable",
    ) if provider == "claude" else ProviderStatus(provider=provider, available=True, detail="ok")
    session = _cli_session(team, "Build a persisted plan artifact")
    review_round = session.review_rounds[1]
    review_job_id = review_round.summary.split("job ")[-1].rstrip(".")
    runtime.fail(review_job_id, summary="review failed", error="claude auth failed")

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="summary",
            session_id=session.id,
            requirement=None,
            mode="success_first",
            runtime="command",
            reroute="on",
            provider="claude",
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert "recovery_provider: mock (round=review, mode=planned, fallback_from=claude," in out
        assert "fallback_reason=reviewer_unavailable" in out
        assert "fallback_detail=claude unavailable" in out
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_retry_review_command_round_trips_session(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.jobs import FileJobRuntime
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    runtime = FileJobRuntime(root=tmp_path / "jobs")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
    )
    session = _cli_session(team, "Build a persisted plan artifact")
    review_round = session.review_rounds[1]
    review_job_id = review_round.summary.split("job ")[-1].rstrip(".")
    runtime.fail(review_job_id, summary="review failed", error="claude auth failed")

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="retry-review",
            session_id=session.id,
            requirement=None,
            mode="success_first",
            runtime="command",
            reroute="on",
            provider="claude",
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["review_rounds"][-1]["round_type"] == "review_retry"
        assert "inspect_delegated_job" not in payload["status_summary"]["next_actions"]
        assert payload["status_summary"]["recovery_actions"] in ([], ["inspect_compliance"])
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_next_command_reports_retry_review_command_for_failed_claude_job(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.jobs import FileJobRuntime
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    runtime = FileJobRuntime(root=tmp_path / "jobs")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
    )
    session = _cli_session(team, "Build a persisted plan artifact")
    review_round = session.review_rounds[1]
    review_job_id = review_round.summary.split("job ")[-1].rstrip(".")
    runtime.fail(review_job_id, summary="review failed", error="claude auth failed")

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="next",
            session_id=session.id,
            requirement=None,
            mode="success_first",
            runtime="command",
            reroute="on",
            provider="claude",
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert "team retry-review" in out
        assert session.id in out
        assert "alternatives: inspect_delegated_job, revise_plan" in out
        assert "context: required_gaps=0 optional_followups=0 delegated_failures=1" in out
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_resume_command_can_apply_execution_reentry(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = _cli_session(team, "Build a persisted plan artifact")

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="resume",
            session_id=session.id,
            apply=True,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] in {"accepted", "needs_followup"}
        assert payload["resume"]["linked_execution_run_id"] is not None
        assert payload["status_summary"]["resume_reason"] == "execution_completed"
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_resume_command_can_apply_approval_reentry(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = _cli_session(team, "Build plan with adversarial challenge")
    revised = team.revise(session.id, summary="Closed adversarial gap", closed_gap_ids=[session.gaps[0].id])

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="resume",
            session_id=revised.id,
            apply=True,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "approved_for_execution"
        assert payload["gate_verdict"] == "approved"
        assert payload["review_rounds"][-1]["round_type"] == "approval"
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_resume_command_rejects_apply_for_revision_state(tmp_path) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = _cli_session(team, "Build plan with adversarial challenge")

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="resume",
            session_id=session.id,
            apply=True,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        with pytest.raises(ValueError, match="cannot auto-apply resume action 'revise'"):
            cli.main()
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_resume_command_rejects_apply_for_completed_execution_state(tmp_path) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = _cli_session(team, "Build a persisted plan artifact")
    executed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="resume",
            session_id=executed.id,
            apply=True,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        with pytest.raises(ValueError, match="cannot auto-apply resume action 'inspect_execution'"):
            cli.main()
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_resume_command_reconciles_completed_linked_run_from_executing_session(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = _cli_session(team, "Build a persisted plan artifact")
    executed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)
    executed.status = "executing"
    executed.gate_verdict = "approved"
    executed.resume.current_phase = "executing"
    executed.resume.pending_role = "build"
    team.store.write_session(executed)

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="resume",
            session_id=executed.id,
            apply=False,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "accepted"
        assert payload["status_summary"]["resume_action"] == "inspect_execution"
        assert payload["resume"]["current_phase"] == "accepted"
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_next_command_reports_runbook_for_revision_session(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = _cli_session(team, "Build plan with adversarial challenge")

    original = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="next",
            session_id=session.id,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert "next_command: python -m agent_orchestrator.cli team revise" in out
        assert "alternatives: none" in out
        assert "context: required_gaps=1 optional_followups=0 delegated_failures=0" in out
        assert "selected_topology:" in out
        assert "topology_reason:" in out
    finally:
        cli.argparse.ArgumentParser.parse_args = original


def test_team_retry_adversarial_review_command_round_trips_session(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.jobs import FileJobRuntime
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    runtime = FileJobRuntime(root=tmp_path / "jobs")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
    )
    session = _cli_session(team, "Build a persisted plan artifact")
    adversarial_round = session.review_rounds[2]
    job_id = adversarial_round.summary.split("job ")[-1].rstrip(".")
    runtime.fail(job_id, summary="adversarial failed", error="claude auth failed")

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="retry-adversarial-review",
            session_id=session.id,
            requirement=None,
            mode="success_first",
            runtime="command",
            reroute="on",
            provider="claude",
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["review_rounds"][-1]["round_type"] == "adversarial_review_retry"
        assert payload["status_summary"]["recovery_actions"] in ([], ["inspect_compliance"])
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_next_command_reports_retry_adversarial_review_command_for_failed_claude_job(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.jobs import FileJobRuntime
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    runtime = FileJobRuntime(root=tmp_path / "jobs")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
    )
    session = _cli_session(team, "Build a persisted plan artifact")
    adversarial_round = session.review_rounds[2]
    job_id = adversarial_round.summary.split("job ")[-1].rstrip(".")
    runtime.fail(job_id, summary="adversarial failed", error="claude auth failed")

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="next",
            session_id=session.id,
            requirement=None,
            mode="success_first",
            runtime="command",
            reroute="on",
            provider="claude",
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert "team retry-adversarial-review" in out
        assert session.id in out
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_runbook_command_reports_revision_workflow(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = _cli_session(team, "Build plan with adversarial challenge")

    original = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="runbook",
            session_id=session.id,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert f"session: {session.id}" in out
        assert "operator_runbook:" in out
        assert "selected_topology:" in out
        assert "topology_reason:" in out
        assert "decision_rationale:" in out
        assert "1. Close every required gap with `python -m agent_orchestrator.cli team revise" in out
        assert "2. Re-run `team summary` or `team next` to confirm approval is now allowed." in out
        assert "3. Use `team approve` only after required gaps are closed." in out
    finally:
        cli.argparse.ArgumentParser.parse_args = original


def test_team_runbook_command_reports_approval_when_required_gaps_closed(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = _cli_session(team, "Build plan with followup checklist and recovery guidance")

    original = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="runbook",
            session_id=session.id,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert "next: approve" in out
        assert "1. Approve the reviewed plan with `python -m agent_orchestrator.cli team approve" in out
        assert "Close every required gap" not in out
    finally:
        cli.argparse.ArgumentParser.parse_args = original


def test_team_next_command_reports_execution_inspection_after_completion(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = _cli_session(team, "Build a persisted plan artifact")
    executed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="next",
            session_id=executed.id,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert "action: inspect_execution" in out
        assert "next_command: python -m agent_orchestrator.cli team inspect-execution" in out
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_inspect_execution_command_reports_linked_run_payload(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = _cli_session(team, "Build a persisted plan artifact")
    executed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="inspect-execution",
            session_id=executed.id,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert "execution_outcome: accepted" in out
        assert "goal:" in out
        assert "selected_topology:" in out
        assert "execution_context_policy: policy=resume_if_same_task" in out
        payload = json.loads(out[out.index('{\n  "run_id"'):])
        assert payload["run_id"] == executed.resume.linked_execution_run_id
        assert payload["metadata"]["approved_plan"]["session_id"] == executed.id
        assert payload["metadata"]["provenance"]["plan_session_id"] == executed.id
        assert payload["session_summary"]["outcome"] == "accepted"
        assert payload["session_summary"]["execution_context_policy"]["policy"] == "resume_if_same_task"
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_inspect_execution_command_rejects_session_without_linked_run(tmp_path) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = _cli_session(team, "Build a persisted plan artifact")

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="inspect-execution",
            session_id=session.id,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
            context_policy="resume_if_same_task",
        )
        with pytest.raises(ValueError, match="linked execution run"):
            cli.main()
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_runbook_command_reports_execution_followup_after_completion(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = team.approve(_cli_session(team, "Build plan with followup checklist").id)
    executed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="runbook",
            session_id=executed.id,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert "operator_runbook:" in out
        assert "Inspect the linked execution run with `python -m agent_orchestrator.cli team inspect-execution" in out
        assert "follow-up" in out or "followup" in out
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_runbook_command_reports_execution_blocked_recovery(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = _cli_session(team, "Build a persisted plan artifact")
    executed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)

    run_path = tmp_path / "runs" / f"{executed.resume.linked_execution_run_id}.json"
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    payload["status"] = "blocked"
    payload["accepted"] = False
    run_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    executed.status = "executing"
    executed.gate_verdict = "approved"
    executed.resume.current_phase = "executing"
    executed.resume.pending_role = "build"
    team.store.write_session(executed)

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="runbook",
            session_id=executed.id,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert "operator_runbook:" in out
        assert "Inspect the linked execution run" in out
        assert "blocked state" in out
        assert "Re-run execution only after" in out
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_runbook_command_reports_execution_provenance_mismatch_recovery(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = _cli_session(team, "Build a persisted plan artifact")
    executed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)

    run_path = tmp_path / "runs" / f"{executed.resume.linked_execution_run_id}.json"
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    payload["metadata"]["plan_session_id"] = "plan-wrong"
    payload["status"] = "blocked"
    payload["accepted"] = False
    run_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    executed.status = "executing"
    executed.gate_verdict = "approved"
    executed.resume.current_phase = "executing"
    executed.resume.pending_role = "build"
    team.store.write_session(executed)

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="runbook",
            session_id=executed.id,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert "Inspect the compliance blocker" in out
        assert "run/session mismatch" in out or "mismatch" in out
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_runbook_command_reports_failed_delegation_recovery(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.jobs import FileJobRuntime
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    runtime = FileJobRuntime(root=tmp_path / "jobs")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        runtime=runtime,
    )
    session = _cli_session(team, "Build a persisted plan artifact")
    review_round = session.review_rounds[1]
    review_job_id = review_round.summary.split("job ")[-1].rstrip(".")
    runtime.fail(review_job_id, summary="review failed", error="claude auth failed")

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="runbook",
            session_id=session.id,
            requirement=None,
            mode="success_first",
            runtime="command",
            reroute="on",
            provider="claude",
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert "operator_runbook:" in out
        assert "1. Inspect the failed delegated Claude review job." in out
        assert "2. Retry the delegated review with `python -m agent_orchestrator.cli team retry-review" in out
        assert "3. Switch to `team revise` if the failure uncovered a real planning gap." in out
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_summary_command_reports_compliance_blocking(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    (tmp_path / "README.md").write_text("# temp\n", encoding="utf-8")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    session = _cli_session(team, "Build a persisted plan artifact")

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="summary",
            session_id=session.id,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert "next: execute" in out
        assert "missing required docs" in out
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_runbook_command_reports_compliance_recovery(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    (tmp_path / "README.md").write_text("# temp\n", encoding="utf-8")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    session = _cli_session(team, "Build a persisted plan artifact")

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="runbook",
            session_id=session.id,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert "operator_runbook:" in out
        assert "1. Use `team status` to inspect the current session state." not in out
        assert "Execute the approved plan" in out or "Inspect the linked execution run" in out
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_next_command_reports_compliance_check_for_blocking_session(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    (tmp_path / "README.md").write_text("# temp\n", encoding="utf-8")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    session = _cli_session(team, "Build a persisted plan artifact")

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="next",
            session_id=session.id,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert "action: execute" in out
        assert "next_command: python -m agent_orchestrator.cli team execute" in out
        assert session.id in out
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_runbook_command_reports_warning_only_compliance_guidance(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    write_minimal_process_docs(tmp_path)
    package_dir = tmp_path / "src" / "agent_orchestrator"
    (package_dir / "stub.py").write_text(
        '"""Stub module."""\n\nfrom __future__ import annotations\n\n# DEPS: __future__\n# RESPONSIBILITY: Keep the changed file compliant while warning on unrelated debt.\n# MODULE: tests\n# ---\n\nVALUE = 1\n',
        encoding="utf-8",
    )
    (package_dir / "legacy.py").write_text(
        '"""Legacy module."""\n\nfrom __future__ import annotations\n\n# DEPS: __future__\n# RESPONSIBILITY: 待补充\n# MODULE: tests\n# ---\n\nVALUE = 1\n',
        encoding="utf-8",
    )
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    team.refresh_documentation_sync()
    session = _cli_session(team, "Build a persisted plan artifact")
    session.compliance = team.check_compliance(changed_files=["src/agent_orchestrator/stub.py"])

    cli._print_team_runbook(session)
    out = capsys.readouterr().out

    assert "non-blocking compliance warning" in out
    assert "You may continue the current session" in out
    assert "warning: src/agent_orchestrator/legacy.py" in out or "legacy.py has placeholder" in out


def test_team_summary_command_preserves_warning_only_compliance_guidance(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    write_minimal_process_docs(tmp_path)
    package_dir = tmp_path / "src" / "agent_orchestrator"
    (package_dir / "legacy.py").write_text(
        '"""Legacy module."""\n\nfrom __future__ import annotations\n\n# DEPS: __future__\n# RESPONSIBILITY: 待补充\n# MODULE: tests\n# ---\n\nVALUE = 1\n',
        encoding="utf-8",
    )
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    team.refresh_documentation_sync()
    session = _cli_session(team, "Build a persisted plan artifact")
    session.compliance = team.check_session_compliance(session.id, changed_files=["src/agent_orchestrator/stub.py"])
    team.store.write_session(session)

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="summary",
            session_id=session.id,
            requirement=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
            runtime="mock",
            provider=None,
        )
        cli.main()
        out = capsys.readouterr().out
        assert "next: inspect_compliance" in out
        assert "non-blocking compliance warnings exist" in out
        assert "legacy.py" in out
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_inspect_blockers_command_prints_execution_blocker_summary(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    write_minimal_process_docs(tmp_path)
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = _cli_session(team, "Build a persisted plan artifact")
    executed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)

    run_path = tmp_path / "runs" / f"{executed.resume.linked_execution_run_id}.json"
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    payload["status"] = "blocked"
    payload["accepted"] = False
    run_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    executed.status = "executing"
    executed.gate_verdict = "approved"
    executed.resume.current_phase = "executing"
    executed.resume.pending_role = "build"
    team.store.write_session(executed)

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="inspect-blockers",
            session_id=executed.id,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert f"session: {executed.id}" in out
        assert "block_source: execution_run" in out
        assert "block_detail: run_blocked" in out
        assert "resume_action: inspect_blockers" in out
        assert f"team inspect-blockers {executed.id}" in out
        json_payload = json.loads(out[out.index("{"):])
        assert json_payload["blocker_summary"]["block_source"] == "execution_run"
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_inspect_blockers_command_prints_delegated_job_summary(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.jobs import FileJobRuntime
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    write_minimal_process_docs(tmp_path)
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
        project_root=tmp_path,
        runtime=runtime,
    )
    session = _cli_session(team, "Build a persisted plan artifact")
    review_round = session.review_rounds[1]
    review_job_id = review_round.summary.split("job ")[-1].rstrip(".")
    runtime.fail(review_job_id, summary="review failed", error="claude auth failed")

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="inspect-blockers",
            session_id=session.id,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        out = capsys.readouterr().out
        assert "block_source: delegated_job" in out
        assert "resume_action: retry_review" in out
        assert f"team inspect-blockers {session.id}" in out
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_team_check_compliance_command_reports_blocking_failure(tmp_path, capsys) -> None:
    from agent_orchestrator import cli

    (tmp_path / "README.md").write_text("# temp\n", encoding="utf-8")

    original = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="check-compliance",
            session_id=None,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        with pytest.raises(SystemExit, match="1"):
            cli.main()
        out = capsys.readouterr().out
        assert "\"status\": \"blocked\"" in out
        assert "missing required docs" in out
    finally:
        cli.argparse.ArgumentParser.parse_args = original


def test_team_check_compliance_command_passes_changed_files_to_team(tmp_path, capsys) -> None:
    from agent_orchestrator import cli

    captured: dict[str, object] = {}

    class _FakeTeam:
        def check_compliance(self, changed_files=None):
            captured["changed_files"] = changed_files
            return {
                "status": "passed",
                "blocking": False,
                "checks": [],
                "blocking_reasons": [],
                "warnings": [],
                "checked_files": [],
                "required_actions": [],
                "recommended_commands": ["python -m agent_orchestrator.cli team check-compliance"],
            }

    original_build = cli._build_team_orchestrator
    original_parse = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: _FakeTeam()
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="check-compliance",
            session_id=None,
            changed_file=["src/agent_orchestrator/planning.py", "docs/process/root-map.md"],
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "passed"
        assert captured["changed_files"] == [
            "src/agent_orchestrator/planning.py",
            "docs/process/root-map.md",
        ]
    finally:
        cli._build_team_orchestrator = original_build
        cli.argparse.ArgumentParser.parse_args = original_parse


def test_team_check_compliance_command_reports_structured_contract_fields(tmp_path, capsys) -> None:
    from agent_orchestrator import cli

    write_minimal_process_docs(tmp_path)

    original = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="check-compliance",
            session_id=None,
            changed_file=["src/agent_orchestrator/stub.py"],
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert "warnings" in payload
        assert "checked_files" in payload
        assert "required_actions" in payload
        assert "recommended_commands" in payload
        assert any(check["name"] == "role_contracts_current" for check in payload["checks"])
    finally:
        cli.argparse.ArgumentParser.parse_args = original


def test_install_hooks_command_installs_pre_commit_script(tmp_path, capsys) -> None:
    from agent_orchestrator import cli

    repo_root = tmp_path / "repo"
    git_hooks = repo_root / ".git" / "hooks"
    scripts_dir = repo_root / "scripts" / "git-hooks"
    git_hooks.mkdir(parents=True, exist_ok=True)
    scripts_dir.mkdir(parents=True, exist_ok=True)
    source_hook = scripts_dir / "pre-commit"
    source_hook.write_text("#!/bin/sh\necho hook\n", encoding="utf-8")

    original = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="install-hooks",
            root=str(repo_root),
        )
        cli.main()
        out = capsys.readouterr().out
        installed = git_hooks / "pre-commit"
        marker = repo_root / ".agent_orchestrator" / "hooks.json"
        assert installed.exists()
        assert marker.exists()
        assert installed.read_text(encoding="utf-8") == source_hook.read_text(encoding="utf-8")
        assert "Installed git hook" in out
        assert "Recorded managed hook marker" in out
    finally:
        cli.argparse.ArgumentParser.parse_args = original


def test_team_start_command_exposes_structured_brief(tmp_path, capsys) -> None:
    from agent_orchestrator import cli

    original = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="start",
            session_id=None,
            requirement="Build a persisted plan artifact",
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["structured_brief"]["goal"]
        assert payload["structured_brief"]["acceptance_criteria"]
    finally:
        cli.argparse.ArgumentParser.parse_args = original


def test_team_summary_json_format_outputs_session_payload(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = _cli_session(team, "Build a persisted plan artifact")

    original_build_team = cli._build_team_orchestrator
    original = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="summary",
            session_id=session.id,
            runtime="mock",
            provider=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
            format="json",
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["id"] == session.id
        assert payload["status_summary"]["primary_action"] == "execute"
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original


def test_team_execute_command_rejects_unapproved_session(tmp_path) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    blocked = _cli_session(team, "Auth migration with roadmap drift")

    original = cli.argparse.ArgumentParser.parse_args
    try:
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="execute",
            session_id=blocked.id,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        with pytest.raises(ValueError, match="approved plan"):
            cli.main()
    finally:
        cli.argparse.ArgumentParser.parse_args = original


def test_team_inspect_knowledge_command_reports_records(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.planning import PlanStore, TeamOrchestrator

    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    team.orchestrator.run_store.root = tmp_path / "runs"
    session = _cli_session(team, "Build a persisted plan artifact")
    executed = team.execute(session.id, OrchestrationMode.SUCCESS_FIRST)

    original_build_team = cli._build_team_orchestrator
    original_parse_args = cli.argparse.ArgumentParser.parse_args
    try:
        cli._build_team_orchestrator = lambda runtime_name, provider, plans_root, runs_root: team
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="inspect-knowledge",
            session_id=executed.id,
            requirement=None,
            mode="success_first",
            runtime="mock",
            reroute="on",
            provider=None,
            agent=None,
            depth=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
            format="json",
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["counts"]["decisions"] >= 1
        assert payload["counts"]["lessons"] >= 1
    finally:
        cli._build_team_orchestrator = original_build_team
        cli.argparse.ArgumentParser.parse_args = original_parse_args


def test_evidence_capture_command_writes_case_file_output(tmp_path, capsys) -> None:
    from agent_orchestrator import cli

    write_minimal_process_docs(tmp_path)
    case_file = tmp_path / "cases.json"
    output_path = tmp_path / "evidence.json"
    case_file.write_text(
        json.dumps([{"label": "artifact", "requirement": "Build a persisted plan artifact"}]),
        encoding="utf-8",
    )

    original = cli.argparse.ArgumentParser.parse_args
    original_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="evidence",
            evidence_command="capture",
            case_file=str(case_file),
            output=str(output_path),
        )
        cli.main()
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        printed = json.loads(capsys.readouterr().out)
        assert printed["summary"]["case_count"] == 1
        assert payload["cases"][0]["label"] == "artifact"
    finally:
        os.chdir(original_cwd)
        cli.argparse.ArgumentParser.parse_args = original


def test_evidence_report_command_writes_markdown(tmp_path, capsys) -> None:
    from agent_orchestrator import cli

    write_minimal_process_docs(tmp_path)
    report_path = tmp_path / "report.md"

    original = cli.argparse.ArgumentParser.parse_args
    original_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="evidence",
            evidence_command="report",
            case_file=None,
            output=str(report_path),
            json_output=None,
        )
        cli.main()
        out = capsys.readouterr().out
        assert "Wrote evidence report" in out
        assert "# v1.x Evidence Report" in report_path.read_text(encoding="utf-8")
    finally:
        os.chdir(original_cwd)
        cli.argparse.ArgumentParser.parse_args = original


def test_evidence_report_json_format_outputs_report_path_payload(tmp_path, capsys) -> None:
    from agent_orchestrator import cli

    write_minimal_process_docs(tmp_path)
    report_path = tmp_path / "report.md"

    original = cli.argparse.ArgumentParser.parse_args
    original_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="evidence",
            evidence_command="report",
            case_file=None,
            output=str(report_path),
            json_output=None,
            format="json",
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["output"] == str(report_path)
        assert payload["summary"]["case_count"] >= 1
    finally:
        os.chdir(original_cwd)
        cli.argparse.ArgumentParser.parse_args = original


def test_evidence_compare_command_writes_trend_report(tmp_path, capsys) -> None:
    from agent_orchestrator import cli
    from agent_orchestrator.evidence import WorkflowEvidenceCase, capture_workflow_evidence

    write_minimal_process_docs(tmp_path)
    baseline_path = tmp_path / "baseline.json"
    current_path = tmp_path / "current.json"
    trend_path = tmp_path / "trend.md"
    capture_workflow_evidence(
        [WorkflowEvidenceCase(requirement="Build a persisted plan artifact", label="artifact")],
        project_root=tmp_path,
        output_path=baseline_path,
    )
    capture_workflow_evidence(
        [
            WorkflowEvidenceCase(requirement="Build a persisted plan artifact", label="artifact"),
            WorkflowEvidenceCase(requirement="Build plan with followup checklist", label="followup"),
        ],
        project_root=tmp_path,
        output_path=current_path,
    )

    original = cli.argparse.ArgumentParser.parse_args
    original_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="evidence",
            evidence_command="compare",
            baseline=str(baseline_path),
            current=str(current_path),
            output=str(trend_path),
            format="json",
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["output"] == str(trend_path)
        assert payload["deltas"]["case_count"] == 1
        assert "# v1.x Evidence Trend" in trend_path.read_text(encoding="utf-8")
    finally:
        os.chdir(original_cwd)
        cli.argparse.ArgumentParser.parse_args = original


def test_team_refresh_docs_command_writes_canonical_docs(tmp_path, capsys) -> None:
    from agent_orchestrator import cli

    (tmp_path / "src" / "agent_orchestrator").mkdir(parents=True)
    (tmp_path / "src" / "agent_orchestrator" / "demo.py").write_text(
        '"""Demo module."""\n\nfrom __future__ import annotations\n\n# DEPS: __future__\n# RESPONSIBILITY: Demo module.\n# MODULE: tests\n# ---\n',
        encoding="utf-8",
    )
    original = cli.argparse.ArgumentParser.parse_args
    original_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="refresh-docs",
            runtime="mock",
            provider=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["documents"]["module_manifest"]["status"] == "passed"
        assert (tmp_path / "docs" / "process" / "module-manifest.md").exists()
    finally:
        os.chdir(original_cwd)
        cli.argparse.ArgumentParser.parse_args = original


def test_team_repair_compliance_command_reports_remaining_actions(tmp_path, capsys) -> None:
    from agent_orchestrator import cli

    write_minimal_process_docs(tmp_path)
    original = cli.argparse.ArgumentParser.parse_args
    original_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="repair-compliance",
            session_id=None,
            changed_file=[],
            runtime="mock",
            provider=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert "refresh_results" in payload
        assert "required_actions" in payload
        assert "recommended_commands" in payload
        assert payload["compliance"]["status"] in {"passed", "warning"}
    finally:
        os.chdir(original_cwd)
        cli.argparse.ArgumentParser.parse_args = original


def test_team_repair_compliance_fix_headers_repairs_safe_missing_header(tmp_path, capsys) -> None:
    from agent_orchestrator import cli

    write_minimal_process_docs(tmp_path)
    package = tmp_path / "src" / "agent_orchestrator"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text('"""Package."""\n', encoding="utf-8")
    target = package / "safe_fix.py"
    target.write_text(
        '"""Safe fix module."""\n\nfrom __future__ import annotations\n\nVALUE = 1\n',
        encoding="utf-8",
    )
    original = cli.argparse.ArgumentParser.parse_args
    original_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        cli.argparse.ArgumentParser.parse_args = lambda self: cli.argparse.Namespace(
            command="team",
            team_command="repair-compliance",
            session_id=None,
            changed_file=["src/agent_orchestrator/safe_fix.py"],
            runtime="mock",
            provider=None,
            plans_root=str(tmp_path / "plans"),
            runs_root=str(tmp_path / "runs"),
            fix_headers=True,
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["header_repair"]["changed_files"] == ["src/agent_orchestrator/safe_fix.py"]
        assert payload["compliance"]["status"] in {"passed", "warning"}
        assert "# MODULE: decision_core" in target.read_text(encoding="utf-8")
    finally:
        os.chdir(original_cwd)
        cli.argparse.ArgumentParser.parse_args = original

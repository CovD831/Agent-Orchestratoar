import json
import os
from datetime import UTC, datetime, timedelta
from dataclasses import replace

from agent_orchestrator import ExecutionContract, OrchestrationMode, Orchestrator, PlanStore, TeamOrchestrator, get_policy
from agent_orchestrator.agent_config import AgentConfig, AgentProfile
from agent_orchestrator.adapters import RuntimeProviderAdapter, RuntimeProviderReviewRescueAdapter
from agent_orchestrator.command import ClaudeCodeAdapter, CodexCliAdapter, CommandJobRuntime, CommandResult, ProviderStatus
from agent_orchestrator.jobs import AgentJob, InMemoryJobRuntime, JobRequest
from agent_orchestrator.run_store import RunStore
from agent_orchestrator.routing import PolicyRouter
from agent_orchestrator.tasks import WorkUnit, WorkUnitResult
from test_support import start_approved_session


class _MixedSession:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.session_id = "session-1"
        self.thread_id = "thread-1"
        self._completed = False

    def poll(self) -> CommandResult | None:
        if self._completed:
            return self.result
        self._completed = True
        return None

    def wait(self, timeout: int | None = None) -> CommandResult:
        return self.result

    def send(self, message: str) -> dict[str, object]:
        return {"session_id": self.session_id, "thread_id": self.thread_id, "message": message, "status": "accepted"}

    def cancel(self) -> dict[str, object]:
        return {"session_id": self.session_id, "thread_id": self.thread_id, "status": "cancelled"}


class _MixedRunner:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.commands: list[list[str]] = []

    def run(self, command: list[str], *, cwd: str, env: dict[str, str] | None = None) -> CommandResult:
        self.commands.append(command)
        return self.result

    def spawn(self, command: list[str], *, cwd: str, env: dict[str, str] | None = None) -> _MixedSession:
        self.commands.append(command)
        return _MixedSession(self.result)


class _StuckRuntime(InMemoryJobRuntime):
    def start(self, request: JobRequest) -> AgentJob:
        job = super().start(request)
        stuck = replace(job, summary="still running", phase="working")
        self.jobs[job.id] = stuck
        return stuck


def test_success_first_uses_full_parent_architecture() -> None:
    run = Orchestrator().run("Refactor auth integration", OrchestrationMode.SUCCESS_FIRST)

    assert run.accepted is True
    assert run.final_state == "accepted"
    assert run.policy.max_depth == 3
    assert run.policy.agent_enabled is True
    assert run.policy.topology_depth == 3
    assert run.policy.provider_flow == ("claude", "codex", "claude")
    assert run.policy.planner_agents == 4
    assert run.policy.review_required is True
    assert len(run.work_units) == 3
    assert [unit.provider_hint for unit in run.work_units] == ["claude", "codex", "claude"]
    assert run.work_units[1].depends_on == [run.work_units[0].id]
    assert run.work_units[2].depends_on == [run.work_units[1].id]
    assert len(run.jobs) == 6
    assert len(run.job_ids) == 6
    assert run.job_status_summary["completed"] == 6
    assert {job.kind for job in run.jobs} == {"implementation", "review"}
    assert {job.provider for job in run.jobs} == {"claude", "codex"}
    assert all("review passed" in result.tests for result in run.results)
    assert all(result.job_ids for result in run.results)
    assert run.attempts and run.attempts[0].attempt_index == 0


def test_speed_first_adds_aggressive_parallelism() -> None:
    run = Orchestrator().run("Build dashboard", OrchestrationMode.SPEED_FIRST)

    assert run.accepted is True
    assert run.policy.parallelism == "aggressive"
    assert run.policy.topology_depth == 2
    assert len(run.work_units) == 2


def test_cost_first_limits_work_units_and_depth() -> None:
    run = Orchestrator().run("Build dashboard", OrchestrationMode.COST_FIRST)

    assert run.accepted is True
    assert run.policy.max_depth == 1
    assert run.policy.agent_enabled is False
    assert run.policy.topology_depth == 0
    assert run.policy.parallelism == "limited"
    assert len(run.work_units) == 1
    assert run.results[0].tests == ["mock validation passed"]


def test_agent_disabled_uses_direct_execution_path() -> None:
    run = Orchestrator().run("Build dashboard", OrchestrationMode.SUCCESS_FIRST, agent_enabled=False)

    assert run.accepted is True
    assert run.policy.agent_enabled is False
    assert run.policy.topology_depth == 0
    assert run.policy.provider_flow == ()
    assert len(run.work_units) == 1
    assert run.work_units[0].owner_type == "single_worker"


def test_runtime_provider_adapter_uses_worker_agent_model_and_prompt() -> None:
    runtime = InMemoryJobRuntime()
    config = AgentConfig(
        profiles={
            **AgentConfig.defaults().profiles,
            "worker": AgentProfile(
                role="worker",
                provider="claude",
                model="opus",
                prompt_template="Worker custom: {default_prompt}",
            ),
        }
    )
    adapter = RuntimeProviderAdapter(
        runtime=runtime,
        kind="implementation",
        default_provider="codex",
        agent_config=config,
    )
    work_unit = WorkUnit(
        goal="Implement configurable worker",
        context="ctx",
        inputs=[],
        outputs=[],
        acceptance_criteria=[],
        risk_level="low",
        parallelizable=False,
        owner_type="single_worker",
        max_depth=1,
        failure_policy="retry",
        id="wu-1",
    )

    adapter.execute(work_unit, get_policy(OrchestrationMode.SUCCESS_FIRST))
    job = next(iter(runtime.jobs.values()))

    assert job.provider == "claude"
    assert job.model == "opus"
    assert job.prompt == "Worker custom: Implement configurable worker"
    assert job.runtime_mode == "cli_inherit"


def test_agent_config_defaults_use_direct_api_for_governance_roles() -> None:
    config = AgentConfig.defaults()

    assert config.profile("planner").runtime_mode == "direct_api"
    assert config.profile("plan_reviewer").runtime_mode == "direct_api"
    assert config.profile("adversarial_reviewer").runtime_mode == "direct_api"
    assert config.profile("execution_reviewer").runtime_mode == "direct_api"
    assert config.profile("worker").runtime_mode == "cli_inherit"
    assert config.profile("rescue").runtime_mode == "cli_inherit"


def test_depth_override_trims_success_first_topology() -> None:
    run = Orchestrator().run("Build dashboard", OrchestrationMode.SUCCESS_FIRST, depth=2)

    assert run.accepted is True
    assert run.policy.agent_enabled is True
    assert run.policy.topology_depth == 2
    assert run.policy.provider_flow == ("claude", "codex")
    assert [unit.provider_hint for unit in run.work_units] == ["claude", "codex"]
    assert len(run.work_units) == 2


def test_command_runtime_uses_mixed_provider_flow(tmp_path) -> None:
    runner = _MixedRunner(CommandResult(command=["fake"], exit_code=0, stdout=json.dumps({"result": "ok", "is_error": False}), stderr=""))
    runtime = CommandJobRuntime(
        root=tmp_path,
        runner=runner,
        adapters={"codex": CodexCliAdapter(), "claude": ClaudeCodeAdapter()},
    )
    orchestrator = Orchestrator(
        worker=RuntimeProviderAdapter(runtime=runtime, kind="implementation"),
        reviewer=RuntimeProviderReviewRescueAdapter(runtime=runtime),
    )

    run = orchestrator.run("Build dashboard", OrchestrationMode.SUCCESS_FIRST)

    assert run.accepted is True
    assert any(job.provider == "codex" and job.command[:2] == ["codex", "exec"] for job in run.jobs)
    assert any(job.provider == "claude" and job.command[:2] == ["claude", "-p"] for job in run.jobs)


def test_runtime_provider_adapter_fails_when_job_never_reaches_terminal_state() -> None:
    runtime = _StuckRuntime()
    adapter = RuntimeProviderAdapter(runtime=runtime, kind="implementation", poll_attempts=1, poll_interval_seconds=0)

    result = adapter.execute(
        WorkUnit(
            goal="Implement stuck task",
            context="exercise timeout handling",
            inputs=["input"],
            outputs=["output"],
            acceptance_criteria=["finish"],
            risk_level="low",
            parallelizable=False,
            owner_type="single_worker",
            max_depth=1,
            failure_policy="retry",
            provider_hint="codex",
        ),
        get_policy(OrchestrationMode.SUCCESS_FIRST),
    )

    assert result.status == "failed"
    assert result.needs_rescue is True
    assert "polling window" in result.summary
    job = runtime.status(result.job_id)
    assert job.status == "failed"
    assert job.parsed_payload is not None
    assert job.parsed_payload["timeout"]["poll_attempts"] == 1


def test_runtime_provider_adapter_records_provider_fallback_artifact(tmp_path) -> None:
    runner = _MixedRunner(CommandResult(command=["fake"], exit_code=0, stdout="ok", stderr=""))
    runtime = CommandJobRuntime(root=tmp_path, runner=runner, adapters={"codex": CodexCliAdapter()})
    adapter = RuntimeProviderAdapter(runtime=runtime, kind="implementation")

    result = adapter.execute(
        WorkUnit(
            goal="Implement with unsupported provider hint",
            context="exercise runtime fallback metadata",
            inputs=["input"],
            outputs=["output"],
            acceptance_criteria=["finish"],
            risk_level="low",
            parallelizable=False,
            owner_type="single_worker",
            max_depth=1,
            failure_policy="retry",
            provider_hint="gemini",
        ),
        get_policy(OrchestrationMode.SUCCESS_FIRST),
    )

    assert result.status == "succeeded"
    job = runtime.status(result.job_id)
    assert job.provider == "codex"
    assert job.metadata["provider_runtime"] == {
        "preferred_provider": "gemini",
        "actual_provider": "codex",
        "fallback_source": "runtime_provider_adapter",
        "fallback_reason": "unsupported_provider_hint",
        "fallback_detail": "Provider hint 'gemini' is unsupported by the runtime adapter; using 'codex'.",
    }
    assert result.job_lifecycle[0]["provider_runtime"] == job.metadata["provider_runtime"]


def test_runtime_provider_adapter_falls_back_when_preferred_adapter_is_missing(tmp_path) -> None:
    runner = _MixedRunner(CommandResult(command=["fake"], exit_code=0, stdout="ok", stderr=""))
    runtime = CommandJobRuntime(root=tmp_path, runner=runner, adapters={"codex": CodexCliAdapter()})
    adapter = RuntimeProviderAdapter(runtime=runtime, kind="implementation")

    result = adapter.execute(
        WorkUnit(
            goal="Implement with missing preferred adapter",
            context="exercise adapter fallback metadata",
            inputs=["input"],
            outputs=["output"],
            acceptance_criteria=["finish"],
            risk_level="low",
            parallelizable=False,
            owner_type="single_worker",
            max_depth=1,
            failure_policy="retry",
            provider_hint="claude",
        ),
        get_policy(OrchestrationMode.SUCCESS_FIRST),
    )

    assert result.status == "succeeded"
    job = runtime.status(result.job_id)
    assert job.provider == "codex"
    assert job.metadata["provider_runtime"]["preferred_provider"] == "claude"
    assert job.metadata["provider_runtime"]["actual_provider"] == "codex"
    assert job.metadata["provider_runtime"]["fallback_source"] == "runtime_provider_adapter"
    assert job.metadata["provider_runtime"]["fallback_reason"] == "adapter_missing"
    assert "claude runtime adapter unavailable" in job.metadata["provider_runtime"]["fallback_detail"]
    assert result.job_lifecycle[0]["provider_runtime"] == job.metadata["provider_runtime"]


def test_runtime_provider_adapter_falls_back_when_preferred_provider_is_unavailable(tmp_path) -> None:
    runner = _MixedRunner(
        CommandResult(
            command=["fake"],
            exit_code=0,
            stdout=json.dumps({"result": "ok", "is_error": False}),
            stderr="",
        )
    )
    runtime = CommandJobRuntime(
        root=tmp_path,
        runner=runner,
        adapters={"codex": CodexCliAdapter(), "claude": ClaudeCodeAdapter()},
    )

    def provider_health(provider: str) -> ProviderStatus:
        if provider == "codex":
            return ProviderStatus(provider="codex", available=False, detail="codex unavailable")
        return ProviderStatus(provider="claude", available=True, detail="claude ok")

    adapter = RuntimeProviderAdapter(
        runtime=runtime,
        kind="implementation",
        provider_health_check=provider_health,
    )

    result = adapter.execute(
        WorkUnit(
            goal="Implement with unavailable preferred provider",
            context="exercise health-check fallback metadata",
            inputs=["input"],
            outputs=["output"],
            acceptance_criteria=["finish"],
            risk_level="low",
            parallelizable=False,
            owner_type="single_worker",
            max_depth=1,
            failure_policy="retry",
            provider_hint="codex",
        ),
        get_policy(OrchestrationMode.SUCCESS_FIRST),
    )

    assert result.status == "succeeded"
    job = runtime.status(result.job_id)
    assert job.provider == "claude"
    assert job.command[:2] == ["claude", "-p"]
    assert job.metadata["provider_runtime"]["preferred_provider"] == "codex"
    assert job.metadata["provider_runtime"]["actual_provider"] == "claude"
    assert job.metadata["provider_runtime"]["fallback_reason"] == "provider_unavailable"
    assert "codex unavailable" in job.metadata["provider_runtime"]["fallback_detail"]
    assert result.job_lifecycle[0]["provider_runtime"] == job.metadata["provider_runtime"]


def test_runtime_provider_review_adapter_records_reviewer_fallback_artifact(tmp_path) -> None:
    runner = _MixedRunner(CommandResult(command=["fake"], exit_code=0, stdout="ok", stderr=""))
    runtime = CommandJobRuntime(root=tmp_path, runner=runner, adapters={"codex": CodexCliAdapter()})
    adapter = RuntimeProviderReviewRescueAdapter(runtime=runtime)

    reviewed = adapter.review_or_rescue(
        WorkUnit(
            goal="Review with missing preferred adapter",
            context="exercise reviewer adapter fallback metadata",
            inputs=["input"],
            outputs=["output"],
            acceptance_criteria=["finish"],
            risk_level="medium",
            parallelizable=False,
            owner_type="claude_team",
            max_depth=1,
            failure_policy="rescue",
            provider_hint="claude",
        ),
        WorkUnitResult(
            work_unit_id="work-1",
            status="succeeded",
            summary="worker ok",
            patch="patch",
            tests=["validation passed"],
            needs_rescue=False,
            job_id="job-origin",
            job_ids=["job-origin"],
            job_status="completed",
            job_phase="done",
            job_lifecycle=[],
        ),
        get_policy(OrchestrationMode.SUCCESS_FIRST),
    )

    assert reviewed.status == "succeeded"
    job = runtime.status(reviewed.job_id)
    assert job.provider == "codex"
    assert job.metadata["provider_runtime"]["preferred_provider"] == "claude"
    assert job.metadata["provider_runtime"]["actual_provider"] == "codex"
    assert job.metadata["provider_runtime"]["fallback_source"] == "runtime_provider_review_rescue_adapter"
    assert job.metadata["provider_runtime"]["fallback_reason"] == "adapter_missing"
    assert reviewed.job_lifecycle[-1]["provider_runtime"] == job.metadata["provider_runtime"]


def test_runtime_provider_review_adapter_fails_when_review_job_never_reaches_terminal_state() -> None:
    runtime = _StuckRuntime()
    adapter = RuntimeProviderReviewRescueAdapter(
        runtime=runtime,
        poll_attempts=1,
        poll_interval_seconds=0,
    )

    reviewed = adapter.review_or_rescue(
        WorkUnit(
            goal="Review stuck task",
            context="exercise timeout handling",
            inputs=["input"],
            outputs=["output"],
            acceptance_criteria=["finish"],
            risk_level="medium",
            parallelizable=False,
            owner_type="claude_team",
            max_depth=1,
            failure_policy="rescue",
            provider_hint="claude",
        ),
        WorkUnitResult(
            work_unit_id="work-1",
            status="succeeded",
            summary="worker ok",
            patch="patch",
            tests=["validation passed"],
            needs_rescue=False,
            job_id="job-origin",
            job_ids=["job-origin"],
            job_status="completed",
            job_phase="done",
            job_lifecycle=[],
        ),
        get_policy(OrchestrationMode.SUCCESS_FIRST),
    )

    assert reviewed.status == "failed"
    assert reviewed.needs_rescue is True
    assert reviewed.tests[-1] == "review failed"
    job = runtime.status(reviewed.job_id)
    assert job.status == "failed"
    assert job.parsed_payload is not None
    assert job.parsed_payload["timeout"]["kind"] == "review"


def test_speed_first_failure_upgrades_to_success_first() -> None:
    run = Orchestrator().run("Fail the auth migration", OrchestrationMode.SPEED_FIRST)

    assert len(run.attempts) == 2
    assert run.final_mode == OrchestrationMode.SUCCESS_FIRST
    assert run.reroute_history[0]["from_mode"] == "speed_first"
    assert run.reroute_history[0]["to_mode"] == "success_first"
    assert run.reroute_history[0]["upgrade_kind"] == "mode_upgrade"


def test_reroute_can_be_disabled() -> None:
    run = Orchestrator().run("Fail the auth migration", OrchestrationMode.SPEED_FIRST, reroute=False)

    assert len(run.attempts) == 1
    assert run.reroute_history == []


def test_single_failed_work_unit_uses_dependency_rescue_without_upgrade() -> None:
    run = Orchestrator().run("Fail task", OrchestrationMode.SUCCESS_FIRST)

    assert len(run.attempts) == 1
    assert run.accepted is True
    assert run.reroute_history == []
    assert run.attempts[0].dependency_rescue_results
    assert run.attempts[0].replayed_work_unit_ids


def test_dependency_rescue_replays_downstream_units() -> None:
    run = Orchestrator().run("Fail auth migration", OrchestrationMode.SPEED_FIRST)

    assert run.attempts[0].replayed_work_unit_ids
    assert set(run.attempts[0].replayed_work_unit_ids) >= set(run.attempts[0].recovered_work_unit_ids)


def test_dependency_rescue_failure_then_upgrades_once() -> None:
    run = Orchestrator().run("Fail the auth migration", OrchestrationMode.COST_FIRST)

    assert run.attempts[0].dependency_rescue_results
    assert len(run.reroute_history) == 2
    assert run.reroute_history[0]["from_mode"] == "cost_first"
    assert run.reroute_history[0]["to_mode"] == "speed_first"
    assert run.reroute_history[0]["from_depth"] == 0
    assert run.reroute_history[0]["to_depth"] == 2
    assert run.reroute_history[0]["upgrade_kind"] == "mode_upgrade"
    assert run.reroute_history[1]["from_mode"] == "speed_first"
    assert run.reroute_history[1]["to_mode"] == "success_first"
    assert run.reroute_history[1]["upgrade_kind"] == "mode_upgrade"


def test_high_risk_review_only_upgrades_once() -> None:
    run = Orchestrator().run("Fail the auth migration", OrchestrationMode.COST_FIRST)

    assert len(run.attempts) == 3
    assert run.reroute_history[0]["from_mode"] == "cost_first"
    assert run.reroute_history[1]["from_mode"] == "speed_first"
    assert run.reroute_history[1]["to_mode"] == "success_first"
    assert run.final_mode == OrchestrationMode.SUCCESS_FIRST
    assert any(
        attempt.failure_decision and attempt.failure_decision.next_mode is not None
        for attempt in run.attempts[:2]
    )


def test_policies_are_derived_from_one_interface() -> None:
    success = get_policy(OrchestrationMode.SUCCESS_FIRST)
    speed = get_policy(OrchestrationMode.SPEED_FIRST)
    cost = get_policy(OrchestrationMode.COST_FIRST)

    assert success.max_depth > speed.max_depth > cost.max_depth
    assert success.parallelism == "controlled"
    assert speed.parallelism == "aggressive"
    assert cost.parallelism == "limited"


def test_success_first_depth_upgrade_precedes_abort() -> None:
    run = Orchestrator().run("Fail the auth migration", OrchestrationMode.SUCCESS_FIRST, depth=1)

    assert len(run.attempts) == 3
    assert [attempt.policy.topology_depth for attempt in run.attempts] == [1, 2, 3]
    assert run.reroute_history[0]["upgrade_kind"] == "depth_upgrade"
    assert run.reroute_history[0]["to_depth"] == 2
    assert run.reroute_history[1]["upgrade_kind"] == "depth_upgrade"
    assert run.reroute_history[1]["to_depth"] == 3
    assert run.final_mode == OrchestrationMode.SUCCESS_FIRST


def test_speed_first_depth_upgrade_precedes_mode_upgrade() -> None:
    run = Orchestrator().run("Fail the auth migration", OrchestrationMode.SPEED_FIRST, depth=1)

    assert len(run.attempts) == 3
    assert [attempt.policy.mode for attempt in run.attempts] == [
        OrchestrationMode.SPEED_FIRST,
        OrchestrationMode.SPEED_FIRST,
        OrchestrationMode.SUCCESS_FIRST,
    ]
    assert [attempt.policy.topology_depth for attempt in run.attempts] == [1, 2, 3]
    assert run.reroute_history[0]["upgrade_kind"] == "depth_upgrade"
    assert run.reroute_history[1]["upgrade_kind"] == "mode_upgrade"


def test_success_first_depth_three_does_not_auto_upgrade() -> None:
    run = Orchestrator().run("Fail the auth migration", OrchestrationMode.SUCCESS_FIRST, depth=3)

    assert len(run.attempts) == 1
    assert run.reroute_history == []


def test_auto_mode_uses_policy_router() -> None:
    router = PolicyRouter()
    requirement = "Implement multiple independent modules in parallel"
    run = Orchestrator(router=router).run(requirement, None)

    assert run.routing_decision is not None
    assert run.routing_decision.mode.value == "speed_first"
    assert run.policy.parallelism == "aggressive"


def test_run_exposes_decision_contract_artifacts() -> None:
    run = Orchestrator().run("Build dashboard", OrchestrationMode.SUCCESS_FIRST)

    assert run.signals is not None
    assert run.signals.task["parallelism"] in {"low", "high"}
    assert run.signals.risk["contract_risk"] in {"low", "medium", "high"}
    assert run.signals.dependency["work_unit_count"] == len(run.work_units)
    assert run.decision_artifact is not None
    assert run.decision_artifact.route["selected_mode"] == "success_first"
    assert run.decision_artifact.review_level["policy"] == "required"
    assert run.decision_artifact.rescue_mode["policy"] == "always_available"
    assert run.decision_artifact.stop_reason in {"accepted", "blocked", "rerouted"}


def test_attempt_decision_artifact_tracks_failure_outcome() -> None:
    run = Orchestrator().run("Fail the auth migration", OrchestrationMode.SPEED_FIRST)
    attempt = run.attempts[0]

    assert attempt.signals is not None
    assert attempt.signals.failure["has_failures"] is True
    assert attempt.decision_artifact is not None
    assert attempt.decision_artifact.replay_scope["policy"] in {"dependency_affected", "failed_only", "none"}
    assert attempt.decision_artifact.reroute_policy["enabled"] is True
    assert attempt.decision_artifact.stop_reason in {"rerouted", "blocked", "accepted"}


def test_auto_mode_decision_contract_preserves_router_signals() -> None:
    run = Orchestrator().run("Implement multiple independent modules in parallel", None)

    assert run.signals is not None
    assert run.signals.task["route_source"] == "router"
    assert run.signals.task["parallelism"] == "high"
    assert run.signals.risk["routing_risk"] in {"low", "high"}
    assert run.decision_artifact is not None
    assert run.decision_artifact.route["source"] == "router"
    assert run.decision_artifact.route["selected_mode"] == run.final_mode.value


def test_run_round_trip_preserves_decision_contract() -> None:
    original = Orchestrator().run("Build dashboard", OrchestrationMode.SUCCESS_FIRST)
    restored = type(original).from_dict(original.to_dict())

    assert restored.signals is not None
    assert restored.signals.task == original.signals.task
    assert restored.signals.dependency == original.signals.dependency
    assert restored.decision_artifact is not None
    assert restored.decision_artifact.route == original.decision_artifact.route
    assert restored.decision_artifact.stop_reason == original.decision_artifact.stop_reason
    assert restored.metadata == original.metadata


def test_direct_run_persists_entrypoint_provenance_metadata(tmp_path) -> None:
    orchestrator = Orchestrator()
    orchestrator.run_store.root = tmp_path

    run = orchestrator.run("Build dashboard", OrchestrationMode.SUCCESS_FIRST)
    payload = json.loads((tmp_path / f"{run.run_id}.json").read_text(encoding="utf-8"))

    assert payload["metadata"]["entrypoint"] == "direct_run"
    assert payload["metadata"]["provenance"]["source_requirement"] == "Build dashboard"
    assert payload["metadata"]["provenance"]["selected_mode"] == "success_first"
    assert payload["metadata"]["provenance"]["selected_topology"] == "team_with_adversarial_review"
    assert payload["metadata"]["provenance"]["selected_provider_runtime"]["author"] == "codex"
    assert payload["metadata"]["provenance"]["selected_provider_runtime"]["preferred_author"] == "codex"
    assert payload["metadata"]["provenance"]["selected_provider_runtime"]["actual_author"] == "codex"
    assert payload["metadata"]["provenance"]["selected_provider_runtime"]["author_fallback_reason"] is None
    assert payload["metadata"]["provenance"]["selected_provider_runtime"]["preferred_reviewer"] == "claude"
    assert payload["metadata"]["provenance"]["selected_provider_runtime"]["actual_reviewer"] == "claude"
    assert payload["metadata"]["provenance"]["selected_provider_runtime"]["fallback_reason"] is None
    assert payload["metadata"]["execution_contract"]["source"] == "approved_plan_style_direct_run"
    assert payload["metadata"]["execution_contract"]["goal"] == "Build dashboard"
    assert payload["metadata"]["execution_contract"]["topology"]["selected_mode"] == "success_first"
    assert payload["metadata"]["execution_contract"]["topology"]["selected_topology"] == "team_with_adversarial_review"
    assert payload["metadata"]["execution_contract"]["provider_recommendation"]["author"] == "codex"
    assert payload["metadata"]["execution_contract"]["provider_recommendation"]["actual_author"] == "codex"
    assert payload["metadata"]["execution_contract"]["provider_recommendation"]["reviewer"] == "claude"
    assert payload["metadata"]["execution_contract"]["provider_recommendation"]["actual_reviewer"] == "claude"
    assert payload["metadata"]["execution_contract"]["provider_recommendation"]["fallback_source"] is None
    assert payload["metadata"]["execution_contract"]["review_policy"]["policy_name"] == "adversarial_required"
    assert payload["metadata"]["execution_contract"]["fallback_policy"]["author"]["actual"] == "codex"
    assert payload["metadata"]["execution_contract"]["fallback_policy"]["author"]["preferred"] == "codex"
    assert payload["metadata"]["execution_contract"]["fallback_policy"]["author"]["fallback_source"] is None
    assert payload["metadata"]["execution_contract"]["fallback_policy"]["reviewer"]["actual"] == "claude"
    assert payload["metadata"]["execution_contract"]["fallback_policy"]["reviewer"]["preferred"] == "claude"
    assert payload["metadata"]["execution_contract"]["fallback_policy"]["reviewer"]["fallback_source"] is None
    assert payload["metadata"]["execution_contract"]["compliance_snapshot"]["source"] == "direct_run"
    assert payload["metadata"]["execution_contract"]["gating"]["contract_source"] == "direct_requirement_with_planning_contract"
    assert payload["metadata"]["approved_plan_summary"]["session_id"] is None
    assert payload["metadata"]["approved_plan_summary"]["selected_topology"] == "team_with_adversarial_review"
    assert payload["metadata"]["approved_plan_summary"]["review_policy"]["policy_name"] == "adversarial_required"


def test_direct_run_records_review_policy_override_and_provider_health_snapshot() -> None:
    health_snapshot = {"providers": [{"provider": "codex", "available": False, "recommended_fallback": "claude"}]}

    run = Orchestrator().run(
        "Build a persisted plan artifact",
        OrchestrationMode.SUCCESS_FIRST,
        review_policy_override="required-human",
        provider_health_snapshot=health_snapshot,
    )

    review_policy = run.metadata["execution_contract"]["review_policy"]
    assert review_policy["policy_name"] == "human_escalation_required"
    assert review_policy["override_source"] == "cli"
    assert run.metadata["provider_health_snapshot"] == health_snapshot


def test_direct_run_exposes_execution_contract_on_run_object() -> None:
    run = Orchestrator().run("Build dashboard", OrchestrationMode.SUCCESS_FIRST)

    assert run.metadata["entrypoint"] == "direct_run"
    assert run.metadata["approved_plan_summary"]["session_id"] is None
    assert run.metadata["execution_contract"]["source"] == "approved_plan_style_direct_run"
    assert run.metadata["execution_contract"]["acceptance_criteria"] == run.contract.acceptance_criteria
    assert run.metadata["execution_contract"]["provider_recommendation"]["author"] == "codex"


def test_direct_run_agent_disabled_uses_solo_topology_in_execution_contract() -> None:
    run = Orchestrator().run("Build dashboard", OrchestrationMode.SUCCESS_FIRST, agent_enabled=False)

    assert run.metadata["execution_contract"]["topology"]["selected_topology"] == "solo"


def test_start_run_persists_execution_contract_after_completion(tmp_path) -> None:
    orchestrator = Orchestrator()
    orchestrator.run_store.root = tmp_path

    handle = orchestrator.start_run("Build dashboard", OrchestrationMode.SUCCESS_FIRST)
    run = orchestrator._wait_for_run(handle.run_id)
    payload = json.loads((tmp_path / f"{run.run_id}.json").read_text(encoding="utf-8"))

    assert payload["metadata"]["entrypoint"] == "direct_run"
    assert payload["metadata"]["execution_contract"]["goal"] == "Build dashboard"
    assert payload["metadata"]["execution_contract"]["topology"]["provider_flow"] == ["claude", "codex", "claude"]


def test_team_and_direct_run_execution_contracts_share_core_schema(tmp_path) -> None:
    direct_run = Orchestrator().run("Build dashboard", OrchestrationMode.SUCCESS_FIRST)
    team = TeamOrchestrator(
        orchestrator=Orchestrator(),
        store=PlanStore(root=tmp_path / "plans"),
    )
    session = start_approved_session(team, "Build a persisted plan artifact")

    direct_contract = direct_run.metadata["execution_contract"]
    plan_contract = session.approved_plan["execution_contract"]

    assert set(direct_contract.keys()) == set(plan_contract.keys())
    assert set(direct_contract["topology"].keys()) == set(plan_contract["topology"].keys())
    assert set(direct_contract["gating"].keys()) == set(plan_contract["gating"].keys())


def test_execution_contract_round_trip_preserves_shared_schema() -> None:
    run = Orchestrator().run("Build dashboard", OrchestrationMode.SUCCESS_FIRST)

    original = ExecutionContract.from_dict(run.metadata["execution_contract"])
    restored = ExecutionContract.from_dict(original.to_dict())

    assert restored.to_dict() == original.to_dict()


def test_start_run_returns_handle(tmp_path) -> None:
    orchestrator = Orchestrator()
    orchestrator.run_store.root = tmp_path
    handle = orchestrator.start_run("Build dashboard", OrchestrationMode.SUCCESS_FIRST)

    assert handle.run_id
    assert handle.status in {"queued", "running"}
    assert handle.job_ids == []


def test_poll_run_round_trips_persisted_payload(tmp_path) -> None:
    orchestrator = Orchestrator()
    orchestrator.run_store.root = tmp_path
    run = orchestrator.run("Build dashboard", OrchestrationMode.SUCCESS_FIRST)
    loaded = orchestrator.poll_run(run.run_id)

    assert loaded.run_id == run.run_id
    assert loaded.final_mode == run.final_mode
    assert loaded.active_attempt_id == run.active_attempt_id


def test_lock_metadata_refreshes_and_is_readable(tmp_path) -> None:
    store = RunStore(root=tmp_path, stale_after_seconds=60)

    assert store.acquire_run_lock("run-1", owner="orchestrator", reason="test") is True
    first = store.read_run_lock("run-1")
    assert first is not None
    assert first["owner"] == "orchestrator"
    assert first["reason"] == "test"
    assert first["state"] == "active"

    heartbeat_before = first["heartbeat_at"]
    assert store.refresh_run_lock("run-1", owner="orchestrator", reason="running") is True
    second = store.read_run_lock("run-1")
    assert second is not None
    assert second["heartbeat_at"] >= heartbeat_before
    assert second["reason"] == "running"


def test_stale_lock_is_reclaimed(tmp_path) -> None:
    store = RunStore(root=tmp_path, stale_after_seconds=0.1)
    lock_path = tmp_path / "run-2.lock"
    lock_path.write_text(
        json.dumps(
            {
                "run_id": "run-2",
                "pid": os.getpid(),
                "owner": "orchestrator",
                "reason": "stale",
                "started_at": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
                "heartbeat_at": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    lock_before = store.read_run_lock("run-2")
    assert lock_before is not None
    assert lock_before["stale"] is True
    assert store.acquire_run_lock("run-2", owner="orchestrator", reason="reclaim") is True
    lock_after = store.read_run_lock("run-2")
    assert lock_after is not None
    assert lock_after["stale"] is False
    assert lock_after["reason"] == "reclaim"


def test_restore_pending_runs_continues_after_restart(tmp_path) -> None:
    orchestrator = Orchestrator()
    orchestrator.run_store.root = tmp_path
    handle = orchestrator.start_run("Build dashboard", OrchestrationMode.SUCCESS_FIRST)
    assert handle.run_id

    restored = Orchestrator(run_store=orchestrator.run_store)
    run = restored.resume_run(handle.run_id)

    assert run.run_id == handle.run_id
    assert run.status in {"queued", "running", "completed", "blocked"}


def test_resume_run_is_idempotent_when_lock_held(tmp_path) -> None:
    orchestrator = Orchestrator()
    orchestrator.run_store.root = tmp_path
    handle = orchestrator.start_run("Build dashboard", OrchestrationMode.SUCCESS_FIRST)

    first = orchestrator.resume_run(handle.run_id)
    second = orchestrator.resume_run(handle.run_id)

    assert first.run_id == second.run_id
    assert first.active_attempt_id == second.active_attempt_id


def test_poll_run_works_while_lock_is_held(tmp_path) -> None:
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
                "reason": "poll_test",
                "started_at": datetime.now(UTC).isoformat(),
                "heartbeat_at": datetime.now(UTC).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    polled = orchestrator.poll_run(run.run_id)

    assert polled.run_id == run.run_id
    assert polled.lock_status is not None
    assert polled.lock_status["state"] == "active"

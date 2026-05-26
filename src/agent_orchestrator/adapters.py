"""Adapter interfaces and deterministic MVP implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from time import sleep
from typing import Any, Protocol, cast

from agent_orchestrator.agent_config import AgentConfig, AgentProfile
from agent_orchestrator.jobs import AgentJob, InMemoryJobRuntime, JobRequest, JobRuntime
from agent_orchestrator.policies import OrchestrationMode, PolicyProfile
from agent_orchestrator.review import Finding, ReviewResult
from agent_orchestrator.tasks import RiskLevel, TaskContract, WorkUnit, WorkUnitResult


class PlannerAdapter(Protocol):
    """Turns fuzzy requirements into a clarified task contract."""

    def clarify(self, requirement: str, policy: PolicyProfile) -> TaskContract:
        """Create a task contract from a user requirement."""


class DecomposerAdapter(Protocol):
    """Splits a clarified task contract into executable work units."""

    def decompose(self, contract: TaskContract, policy: PolicyProfile) -> list[WorkUnit]:
        """Create executable work units."""


class WorkerAdapter(Protocol):
    """Executes a single work unit."""

    def execute(self, work_unit: WorkUnit, policy: PolicyProfile) -> WorkUnitResult:
        """Execute a work unit and return a result."""


class ReviewRescueAdapter(Protocol):
    """Reviews or rescues worker output."""

    def review_or_rescue(
        self,
        work_unit: WorkUnit,
        result: WorkUnitResult,
        policy: PolicyProfile,
    ) -> WorkUnitResult:
        """Review successful output or rescue failed output."""


@dataclass(slots=True)
class MockClaudePlanner:
    """MVP Claude-style planner that produces stable task contracts."""

    def clarify(self, requirement: str, policy: PolicyProfile) -> TaskContract:
        goal = requirement.strip()
        if not goal:
            goal = "Clarify and implement the requested change"

        topology = policy.execution_topology
        if not topology.agent_enabled:
            context = "Run through the control plane without spawning agent topology."
            non_goals = ["Do not recurse into agent delegation when agent mode is disabled"]
        else:
            context = (
                "Use the success-first parent architecture and downgrade behavior "
                f"through policy when requested. Provider flow: {' -> '.join(topology.provider_flow)}."
            )
            non_goals = ["Do not recurse beyond the selected policy depth"]

        return TaskContract(
            goal=goal,
            non_goals=non_goals,
            context=context,
            inputs=[goal],
            outputs=["task tree", "worker results", "review summary"],
            acceptance_criteria=[
                "Requirement is represented as executable work units",
                "Worker results include tests or validation notes",
                "Failures are routed according to policy",
            ],
            risk_level=_infer_risk(goal),
            parallelizable=True,
            owner_type="claude_team",
            max_depth=policy.max_depth,
            failure_policy="rescue" if policy.rescue_enabled else "retry",
        )


@dataclass(slots=True)
class MockClaudeDecomposer:
    """MVP decomposer that models Claude team-style division of labor."""

    def decompose(self, contract: TaskContract, policy: PolicyProfile) -> list[WorkUnit]:
        context = f"{contract.context} {contract.goal}"
        flow = policy.provider_flow
        if not policy.agent_enabled or policy.topology_depth == 0:
            return [
                WorkUnit(
                    goal="Execute the requested change directly",
                    context=context,
                    inputs=contract.inputs,
                    outputs=["patch", "validation notes"],
                    acceptance_criteria=["Direct execution completes without agent delegation"],
                    risk_level=contract.risk_level,
                    parallelizable=False,
                    owner_type="single_worker",
                    max_depth=contract.max_depth,
                    failure_policy=contract.failure_policy,
                    provider_hint="codex",
                    depends_on=[],
                )
            ]

        base_units = [
            WorkUnit(
                goal="Define task contract and acceptance criteria",
                context=context,
                inputs=contract.inputs,
                outputs=["contract"],
                acceptance_criteria=["Contract has goal, constraints, and acceptance criteria"],
                risk_level="medium",
                parallelizable=True,
                owner_type="codex_swarm",
                max_depth=contract.max_depth,
                failure_policy=contract.failure_policy,
                provider_hint=_flow_provider(flow, 0, fallback="claude"),
                depends_on=[],
            ),
            WorkUnit(
                goal="Implement worker execution path",
                context=context,
                inputs=["task contract"],
                outputs=["patch", "validation notes"],
                acceptance_criteria=["Worker returns a structured result"],
                risk_level=contract.risk_level,
                parallelizable=True,
                owner_type="codex_swarm",
                max_depth=contract.max_depth,
                failure_policy=contract.failure_policy,
                provider_hint=_flow_provider(flow, 1, fallback="codex"),
                depends_on=[],
            ),
            WorkUnit(
                goal="Validate merge readiness",
                context=context,
                inputs=["worker results"],
                outputs=["review summary"],
                acceptance_criteria=["Review determines whether output is acceptable"],
                risk_level="high" if policy.review_required is True else "medium",
                parallelizable=False,
                owner_type="claude_team",
                max_depth=contract.max_depth,
                failure_policy="rescue",
                provider_hint=_flow_provider(flow, 2, fallback="claude"),
                depends_on=[],
            ),
        ]

        base_units[1].depends_on = [base_units[0].id]
        base_units[2].depends_on = [base_units[1].id]

        if policy.topology_depth <= 1 or policy.parallelism == "limited":
            return base_units[:1]
        if policy.topology_depth == 2:
            return base_units[:2]
        if policy.parallelism == "aggressive":
            compatibility = WorkUnit(
                    goal="Run speculative compatibility check",
                    context=context,
                    inputs=["worker results"],
                    outputs=["compatibility notes"],
                    acceptance_criteria=["Compatibility risks are listed"],
                    risk_level="low",
                    parallelizable=True,
                    owner_type="codex_swarm",
                    max_depth=contract.max_depth,
                    failure_policy="retry",
                    provider_hint=_flow_provider(flow, 1, fallback="codex"),
                    depends_on=[base_units[1].id],
                )
            return base_units + [compatibility]
        return base_units


@dataclass(slots=True)
class MockCodexWorker:
    """MVP Codex-style worker that simulates deterministic implementation."""

    runtime: JobRuntime = field(default_factory=InMemoryJobRuntime)

    def execute(self, work_unit: WorkUnit, policy: PolicyProfile) -> WorkUnitResult:
        provider = _work_unit_provider(work_unit, default="codex")
        job = self.runtime.start(
            JobRequest(
                task_id=work_unit.id,
                provider=provider,
                kind="implementation",
                prompt=work_unit.goal,
                cwd=str(Path.cwd()),
                max_depth=policy.max_depth,
                metadata={
                    "context": work_unit.context,
                    "inputs": work_unit.inputs,
                    "outputs": work_unit.outputs,
                    "acceptance_criteria": work_unit.acceptance_criteria,
                },
            )
        )

        lowered = f"{work_unit.goal} {work_unit.context} {' '.join(work_unit.inputs)}".lower()
        if "fail" in lowered:
            failed_job = _runtime_fail(
                self.runtime,
                job.id,
                summary=f"Worker hit a simulated execution failure in {job.id}.",
                error="Simulated execution failure.",
                stdout=f"Simulated failure for prompt: {work_unit.goal}",
                parsed_payload={"request": {"work_unit_id": work_unit.id}},
            )
            return WorkUnitResult(
                work_unit_id=work_unit.id,
                status="failed",
                summary=failed_job.summary or f"Worker hit a simulated execution failure in {job.id}.",
                patch=None,
                tests=["not run"],
                needs_rescue=True,
                job_id=failed_job.id,
                job_ids=[failed_job.id],
                job_status=failed_job.status,
                job_phase=failed_job.phase,
                job_lifecycle=[_job_ref(failed_job)],
            )

        completed_job = _runtime_complete(
            self.runtime,
            job.id,
            summary=f"Completed via {job.id}: {work_unit.goal}",
            stdout=f"Completed prompt: {work_unit.goal}",
            parsed_payload={"request": {"work_unit_id": work_unit.id}},
        )
        return WorkUnitResult(
            work_unit_id=work_unit.id,
            status="succeeded",
            summary=completed_job.summary or f"Completed via {job.id}: {work_unit.goal}",
            patch=f"mock-patch-for-{work_unit.id}",
            tests=["mock validation passed"],
            needs_rescue=False,
            job_id=completed_job.id,
            job_ids=[completed_job.id],
            job_status=completed_job.status,
            job_phase=completed_job.phase,
            job_lifecycle=[_job_ref(completed_job)],
        )


@dataclass(slots=True)
class MockClaudeReviewRescue:
    """MVP Claude-style review/rescue team."""

    runtime: JobRuntime = field(default_factory=InMemoryJobRuntime)

    def review_or_rescue(
        self,
        work_unit: WorkUnit,
        result: WorkUnitResult,
        policy: PolicyProfile,
    ) -> WorkUnitResult:
        review_provider = _review_provider(policy)
        if result.status == "failed" and policy.rescue_enabled:
            job = self.runtime.start(
                JobRequest(
                    task_id=work_unit.id,
                    provider=review_provider,
                    kind="rescue",
                    prompt=f"Rescue failed work unit: {work_unit.goal}",
                    cwd=str(Path.cwd()),
                    max_depth=policy.max_depth,
                    failure_reason=result.summary,
                )
            )
            rescued_job = _runtime_complete(
                self.runtime,
                job.id,
                summary=f"Rescued via {job.id}: {work_unit.goal}",
                stdout=f"Rescue completed for: {work_unit.goal}",
                parsed_payload={"request": {"work_unit_id": work_unit.id, "origin_status": result.status}},
            )
            return WorkUnitResult(
                work_unit_id=work_unit.id,
                status="rescued",
                summary=rescued_job.summary or f"Rescued via {job.id}: {work_unit.goal}",
                patch=result.patch or f"rescued-patch-for-{work_unit.id}",
                tests=["rescue validation passed"],
                needs_rescue=False,
                job_id=rescued_job.id,
                job_ids=_merge_job_ids(result, rescued_job),
                job_status=rescued_job.status,
                job_phase=rescued_job.phase,
                job_lifecycle=[*result.job_lifecycle, _job_ref(rescued_job)],
                recovery_origin_status=result.status,
            )

        if _should_review(work_unit, policy):
            job = self.runtime.start(
                JobRequest(
                    task_id=work_unit.id,
                    provider=review_provider,
                    kind="review",
                    prompt=f"Review work unit result: {work_unit.goal}",
                    cwd=str(Path.cwd()),
                    max_depth=policy.max_depth,
                )
            )
            review_result = _build_review_result(work_unit, result, policy)
            reviewed_job = _runtime_complete(
                self.runtime,
                job.id,
                summary=f"Reviewed by Claude team via {job.id}: {result.summary}",
                stdout=f"Review completed for: {work_unit.goal}",
                parsed_payload={
                    "request": {"work_unit_id": work_unit.id},
                    "review_result": review_result.to_dict(),
                },
                phase="reviewing",
            )
            return WorkUnitResult(
                work_unit_id=work_unit.id,
                status=result.status,
                summary=reviewed_job.summary or f"Reviewed by Claude team via {job.id}: {result.summary}",
                patch=result.patch,
                tests=[*result.tests, "review passed"],
                needs_rescue=False,
                job_id=reviewed_job.id,
                job_ids=_merge_job_ids(result, reviewed_job),
                job_status=reviewed_job.status,
                job_phase=reviewed_job.phase,
                job_lifecycle=[*result.job_lifecycle, _job_ref(reviewed_job)],
                review_result=review_result,
            )

        return result


def _infer_risk(goal: str) -> RiskLevel:
    lowered = goal.lower()
    if any(token in lowered for token in ["migration", "security", "payment", "auth"]):
        return "high"
    if any(token in lowered for token in ["refactor", "integration", "parallel"]):
        return "medium"
    return "low"


def _should_review(work_unit: WorkUnit, policy: PolicyProfile) -> bool:
    if policy.review_required is True:
        return True
    if policy.review_required == "risk_based":
        return work_unit.risk_level in {"medium", "high"}
    return False


def _build_review_result(work_unit: WorkUnit, result: WorkUnitResult, policy: PolicyProfile) -> ReviewResult:
    lowered = f"{work_unit.goal} {work_unit.context} {result.summary}".lower()
    if policy.mode != OrchestrationMode.SUCCESS_FIRST and any(token in lowered for token in ["security", "auth", "payment", "migration"]):
        return ReviewResult(
            verdict="needs_attention",
            summary="High-risk findings require escalation.",
            findings=[
                Finding(
                    severity="high",
                    title="Escalate to stronger mode",
                    body="This work unit touches a high-risk area and should be rerun in a stronger mode.",
                    file="orchestrator",
                    line_start=1,
                    line_end=1,
                    confidence=0.9,
                    recommendation="Upgrade to success_first and rerun the task.",
                )
            ],
            next_steps=["Upgrade orchestration mode and rerun the task."],
        )

    return ReviewResult(verdict="approve", summary="Review passed.", next_steps=["Continue as planned."])


@dataclass(slots=True)
class RuntimeProviderAdapter:
    """Executes work units through a concrete provider-backed JobRuntime."""

    runtime: JobRuntime
    kind: str
    default_provider: str = "codex"
    poll_interval_seconds: float = 0.01
    poll_attempts: int = 200
    provider_health_check: Any | None = None
    agent_config: AgentConfig = field(default_factory=AgentConfig.defaults)

    def execute(self, work_unit: WorkUnit, policy: PolicyProfile) -> WorkUnitResult:
        profile = self.agent_config.profile("worker")
        provider_selection = _provider_selection(
            work_unit,
            default=profile.provider or self.default_provider,
            runtime=self.runtime,
            provider_health_check=self.provider_health_check,
            fallback_source="runtime_provider_adapter",
        )
        provider = provider_selection["actual_provider"]
        prompt = _profile_prompt(profile, work_unit.goal, work_unit=work_unit)
        job = self.runtime.start(
            JobRequest(
                task_id=work_unit.id,
                provider=provider,  # type: ignore[arg-type]
                kind=self.kind,  # type: ignore[arg-type]
                prompt=prompt,
                cwd=str(Path.cwd()),
                model=profile.model,
                reasoning_effort=profile.reasoning_effort,  # type: ignore[arg-type]
                sandbox=profile.sandbox,  # type: ignore[arg-type]
                runtime_mode=profile.runtime_mode,
                max_depth=policy.max_depth,
                metadata={
                    "context": work_unit.context,
                    "inputs": work_unit.inputs,
                    "outputs": work_unit.outputs,
                    "acceptance_criteria": work_unit.acceptance_criteria,
                    "provider_runtime": provider_selection,
                },
            )
        )

        completed_job = self.runtime.status(job.id)
        for _ in range(self.poll_attempts):
            if completed_job.status in {"completed", "failed", "cancelled"}:
                break
            sleep(self.poll_interval_seconds)
            completed_job = self.runtime.status(job.id)

        if completed_job.status == "running":
            completed_job = _runtime_fail(
                self.runtime,
                job.id,
                summary="Provider job timed out while still running.",
                error=(
                    "Provider job exceeded the polling window without reaching a terminal status."
                ),
                parsed_payload={
                    "request": {"work_unit_id": work_unit.id},
                    "timeout": {
                        "poll_attempts": self.poll_attempts,
                        "poll_interval_seconds": self.poll_interval_seconds,
                    },
                },
            )

        if completed_job.status == "failed":
            return WorkUnitResult(
                work_unit_id=work_unit.id,
                status="failed",
                summary=completed_job.error or completed_job.summary or "Provider job failed.",
                patch=None,
                tests=["provider command failed"],
                needs_rescue=True,
                job_id=completed_job.id,
                job_ids=[completed_job.id],
                job_status=completed_job.status,
                job_phase=completed_job.phase,
                job_lifecycle=[_job_ref(completed_job)],
            )
        if completed_job.status == "cancelled":
            return WorkUnitResult(
                work_unit_id=work_unit.id,
                status="failed",
                summary=completed_job.summary or "Provider job was cancelled.",
                patch=None,
                tests=["provider command cancelled"],
                needs_rescue=True,
                job_id=completed_job.id,
                job_ids=[completed_job.id],
                job_status=completed_job.status,
                job_phase=completed_job.phase,
                job_lifecycle=[_job_ref(completed_job)],
            )

        return WorkUnitResult(
            work_unit_id=work_unit.id,
            status="succeeded",
            summary=completed_job.summary or "Provider job completed.",
            patch=None,
            tests=["provider command completed"],
            needs_rescue=False,
            job_id=completed_job.id,
            job_ids=[completed_job.id],
            job_status=completed_job.status,
            job_phase=completed_job.phase,
            job_lifecycle=[_job_ref(completed_job)],
        )


@dataclass(slots=True)
class RuntimeProviderReviewRescueAdapter:
    """Executes review/rescue work units through a command-backed runtime."""

    runtime: JobRuntime
    default_provider: str = "claude"
    poll_interval_seconds: float = 0.01
    poll_attempts: int = 200
    provider_health_check: Any | None = None
    agent_config: AgentConfig = field(default_factory=AgentConfig.defaults)

    def review_or_rescue(
        self,
        work_unit: WorkUnit,
        result: WorkUnitResult,
        policy: PolicyProfile,
    ) -> WorkUnitResult:
        kind = "rescue" if result.status == "failed" and policy.rescue_enabled else "review"
        profile = self.agent_config.profile("rescue" if kind == "rescue" else "execution_reviewer")
        provider_selection = _provider_selection(
            work_unit,
            default=profile.provider or self.default_provider,
            runtime=self.runtime,
            provider_health_check=self.provider_health_check,
            fallback_source="runtime_provider_review_rescue_adapter",
        )
        provider = provider_selection["actual_provider"]
        failure_reason = result.summary if kind == "rescue" else None
        prompt = _profile_prompt(
            profile,
            f"{kind.title()} work unit: {work_unit.goal}",
            work_unit=work_unit,
            origin_status=result.status,
        )
        job = self.runtime.start(
            JobRequest(
                task_id=work_unit.id,
                provider=provider,  # type: ignore[arg-type]
                kind=kind,  # type: ignore[arg-type]
                prompt=prompt,
                cwd=str(Path.cwd()),
                model=profile.model,
                reasoning_effort=profile.reasoning_effort,  # type: ignore[arg-type]
                sandbox=profile.sandbox,  # type: ignore[arg-type]
                runtime_mode=profile.runtime_mode,
                max_depth=policy.max_depth,
                failure_reason=failure_reason,
                metadata={
                    "context": work_unit.context,
                    "inputs": work_unit.inputs,
                    "outputs": work_unit.outputs,
                    "acceptance_criteria": work_unit.acceptance_criteria,
                    "origin_status": result.status,
                    "provider_runtime": provider_selection,
                },
            )
        )

        completed_job = self.runtime.status(job.id)
        for _ in range(self.poll_attempts):
            if completed_job.status in {"completed", "failed", "cancelled"}:
                break
            sleep(self.poll_interval_seconds)
            completed_job = self.runtime.status(job.id)

        if completed_job.status == "running":
            completed_job = _runtime_fail(
                self.runtime,
                job.id,
                summary=f"Provider {kind} job timed out while still running.",
                error=(
                    f"Provider {kind} job exceeded the polling window without reaching a terminal status."
                ),
                parsed_payload={
                    "request": {"work_unit_id": work_unit.id},
                    "timeout": {
                        "poll_attempts": self.poll_attempts,
                        "poll_interval_seconds": self.poll_interval_seconds,
                        "kind": kind,
                    },
                },
            )

        if completed_job.status in {"failed", "cancelled"}:
            return WorkUnitResult(
                work_unit_id=work_unit.id,
                status="failed",
                summary=completed_job.error or completed_job.summary or "Provider review job failed.",
                patch=result.patch,
                tests=[*result.tests, f"{kind} failed"],
                needs_rescue=True,
                job_id=completed_job.id,
                job_ids=[completed_job.id, *result.job_ids],
                job_status=completed_job.status,
                job_phase=completed_job.phase,
                job_lifecycle=[*result.job_lifecycle, _job_ref(completed_job)],
                recovery_origin_status=result.status if result.status != "failed" else result.recovery_origin_status,
            )

        parsed_review = _parse_provider_review_payload(completed_job.parsed_payload)
        if kind == "review" and parsed_review is None:
            parsed_review = ReviewResult(
                verdict="approve",
                summary=completed_job.summary or f"Reviewed by provider {provider}.",
                next_steps=["Continue as planned."],
            )
        if kind == "rescue" and policy.rescue_enabled:
            return WorkUnitResult(
                work_unit_id=work_unit.id,
                status="rescued",
                summary=completed_job.summary or f"Rescued via {job.id}: {work_unit.goal}",
                patch=result.patch or f"rescued-patch-for-{work_unit.id}",
                tests=[*result.tests, "rescue validation passed"],
                needs_rescue=False,
                job_id=completed_job.id,
                job_ids=[completed_job.id, *result.job_ids],
                job_status=completed_job.status,
                job_phase=completed_job.phase,
                job_lifecycle=[*result.job_lifecycle, _job_ref(completed_job)],
                recovery_origin_status=result.status,
            )

        return WorkUnitResult(
            work_unit_id=work_unit.id,
            status=result.status,
            summary=completed_job.summary or f"Reviewed by provider {provider}: {result.summary}",
            patch=result.patch,
            tests=[*result.tests, "review passed"],
            needs_rescue=False,
            job_id=completed_job.id,
            job_ids=[completed_job.id, *result.job_ids],
            job_status=completed_job.status,
            job_phase=completed_job.phase,
            job_lifecycle=[*result.job_lifecycle, _job_ref(completed_job)],
            review_result=parsed_review,
            recovery_origin_status=result.recovery_origin_status,
        )


def _runtime_complete(
    runtime: JobRuntime,
    job_id: str,
    *,
    summary: str,
    stdout: str | None = None,
    parsed_payload: dict[str, Any] | None = None,
    phase: str = "done",
) -> AgentJob:
    complete = getattr(runtime, "complete", None)
    if callable(complete):
        return cast(
            AgentJob,
            complete(
                job_id,
                summary=summary,
                stdout=stdout,
                raw_output=stdout,
                parsed_payload=parsed_payload,
                phase=phase,
            ),
        )
    return runtime.status(job_id)


def _runtime_fail(
    runtime: JobRuntime,
    job_id: str,
    *,
    summary: str,
    error: str,
    stdout: str | None = None,
    parsed_payload: dict[str, Any] | None = None,
) -> AgentJob:
    fail = getattr(runtime, "fail", None)
    if callable(fail):
        return cast(
            AgentJob,
            fail(
                job_id,
                summary=summary,
                error=error,
                stdout=stdout,
                raw_output=stdout,
                parsed_payload=parsed_payload,
            ),
        )
    return runtime.status(job_id)


def _job_ref(job: AgentJob) -> dict[str, object]:
    payload = {
        "job_id": job.id,
        "provider": job.provider,
        "kind": job.kind,
        "status": job.status,
        "phase": job.phase,
        "session_id": job.session_id,
        "thread_id": job.thread_id,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
    }
    provider_runtime = job.metadata.get("provider_runtime")
    if isinstance(provider_runtime, dict):
        payload["provider_runtime"] = dict(provider_runtime)
    return payload


def _merge_job_ids(result: WorkUnitResult, job: AgentJob) -> list[str]:
    existing = list(result.job_ids)
    if job.id in existing:
        return existing
    return [*existing, job.id]


def _flow_provider(flow: tuple[str, ...], index: int, *, fallback: str) -> str:
    if index < len(flow):
        return flow[index]
    return fallback


def _profile_prompt(profile: AgentProfile, default_prompt: str, **context: object) -> str:
    return profile.render_prompt(default_prompt, **context)


def _work_unit_provider(work_unit: WorkUnit, *, default: str) -> str:
    return str(_provider_selection(work_unit, default=default)["actual_provider"])


def _provider_selection(
    work_unit: WorkUnit,
    *,
    default: str,
    runtime: JobRuntime | None = None,
    provider_health_check: Any | None = None,
    fallback_source: str = "runtime_provider_adapter",
) -> dict[str, object]:
    preferred = work_unit.provider_hint or default
    if preferred not in {"claude", "codex", "mock"}:
        actual = _fallback_provider(default=default, runtime=runtime, provider_health_check=provider_health_check)
        return {
            "preferred_provider": preferred,
            "actual_provider": actual,
            "fallback_source": fallback_source,
            "fallback_reason": "unsupported_provider_hint",
            "fallback_detail": f"Provider hint '{preferred}' is unsupported by the runtime adapter; using '{actual}'.",
        }

    status = _provider_runtime_status(preferred, runtime=runtime, provider_health_check=provider_health_check)
    if status["available"]:
        return {
            "preferred_provider": preferred,
            "actual_provider": preferred,
            "fallback_source": None,
            "fallback_reason": None,
            "fallback_detail": None,
        }

    actual = _fallback_provider(
        default=default,
        runtime=runtime,
        provider_health_check=provider_health_check,
        exclude={preferred},
    )
    fallback_reason = str(status["reason"])
    if actual == preferred:
        detail = f"Preferred provider '{preferred}' is unavailable: {status['detail']}; no fallback provider was available."
    else:
        detail = f"Preferred provider '{preferred}' is unavailable: {status['detail']}; using '{actual}'."
    return {
        "preferred_provider": preferred,
        "actual_provider": actual,
        "fallback_source": fallback_source,
        "fallback_reason": fallback_reason,
        "fallback_detail": detail,
    }


def _fallback_provider(
    *,
    default: str,
    runtime: JobRuntime | None,
    provider_health_check: Any | None,
    exclude: set[str] | None = None,
) -> str:
    excluded = exclude or set()
    configured = _configured_runtime_providers(runtime)
    candidates = [default, "codex", "claude", "mock"]
    for candidate in candidates:
        if candidate in excluded or candidate not in configured:
            continue
        status = _provider_runtime_status(candidate, runtime=runtime, provider_health_check=provider_health_check)
        if status["available"]:
            return candidate
    return default


def _configured_runtime_providers(runtime: JobRuntime | None) -> set[str]:
    adapters = getattr(runtime, "adapters", None)
    if isinstance(adapters, dict):
        return {str(provider) for provider in adapters}
    return {"claude", "codex", "mock"}


def _provider_runtime_status(
    provider: str,
    *,
    runtime: JobRuntime | None,
    provider_health_check: Any | None,
) -> dict[str, object]:
    if provider not in {"claude", "codex", "mock"}:
        return {"available": False, "reason": "unsupported_provider_hint", "detail": f"{provider} is unsupported"}
    if provider not in _configured_runtime_providers(runtime):
        return {"available": False, "reason": "adapter_missing", "detail": f"{provider} runtime adapter unavailable"}
    if provider_health_check is None:
        return {"available": True, "reason": None, "detail": "provider availability was not health-checked"}
    try:
        status = provider_health_check(provider) if callable(provider_health_check) else provider_health_check.check(provider)
    except Exception as exc:
        return {"available": False, "reason": "provider_unavailable", "detail": str(exc) or type(exc).__name__}
    available = bool(getattr(status, "available", False))
    detail = str(getattr(status, "detail", "provider unavailable"))
    return {
        "available": available,
        "reason": None if available else "provider_unavailable",
        "detail": detail,
    }


def _review_provider(policy: PolicyProfile) -> str:
    if policy.provider_flow:
        last = policy.provider_flow[-1]
        if last in {"claude", "codex"}:
            return last
    return "claude"


def _parse_provider_review_payload(payload: dict[str, Any] | None) -> ReviewResult | None:
    if not payload:
        return None
    review_payload = payload.get("review_result")
    if not review_payload:
        return None
    return ReviewResult(
        verdict=review_payload["verdict"],
        summary=str(review_payload["summary"]),
        findings=[
            Finding(
                severity=finding["severity"],
                title=str(finding["title"]),
                body=str(finding["body"]),
                file=str(finding["file"]),
                line_start=int(finding["line_start"]),
                line_end=int(finding["line_end"]),
                confidence=float(finding["confidence"]),
                recommendation=str(finding["recommendation"]),
            )
            for finding in review_payload.get("findings", [])
        ],
        next_steps=list(review_payload.get("next_steps", [])),
    )

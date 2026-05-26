"""Durable job lifecycle models and runtimes."""

from __future__ import annotations

# DEPS: __future__, agent_orchestrator, dataclasses, datetime, json, pathlib, typing, uuid
# RESPONSIBILITY: Model and persist job requests, lifecycle state, and local job runtimes.
# MODULE: runtime
# ---

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from agent_orchestrator.guards import validate_job_request_permissions, validate_runtime_start

Provider = Literal["claude", "codex", "mock"]
JobKind = Literal["research", "implementation", "review", "adversarial_review", "rescue"]
JobStatus = Literal["pending", "running", "idle", "completed", "failed", "cancelled"]
JobPhase = Literal["starting", "working", "reviewing", "done", "failed", "cancelled"]
RuntimeMode = Literal["cli_inherit", "cli_isolated", "direct_api"]
ProviderOperationStatus = Literal[
    "accepted",
    "unsupported",
    "session_missing",
    "auth_required",
    "provider_unavailable",
    "already_terminal",
]
SandboxMode = Literal["read-only", "workspace-write", "danger-full-access"]
ReasoningEffort = Literal["low", "medium", "high", "xhigh"]
DelegationStep = tuple[Provider, JobKind]

TERMINAL_STATUSES: set[JobStatus] = {"completed", "failed", "cancelled"}
READ_ONLY_KINDS: set[JobKind] = {"research", "review", "adversarial_review"}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_job_id() -> str:
    return f"job-{uuid4().hex[:8]}"


@dataclass(frozen=True, slots=True)
class JobRequest:
    task_id: str
    provider: Provider
    kind: JobKind
    prompt: str
    cwd: str
    model: str | None = None
    reasoning_effort: ReasoningEffort = "medium"
    sandbox: SandboxMode | None = None
    runtime_mode: RuntimeMode = "cli_inherit"
    max_depth: int = 3
    delegation_chain: list[DelegationStep] = field(default_factory=list)
    failure_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.failure_reason and self.kind == "rescue":
            raise ValueError("Rescue jobs require a failure_reason.")
        if len(self.delegation_chain) >= self.max_depth:
            raise ValueError("Delegation chain exceeds max_depth.")
        if _is_unjustified_ping_pong(self.delegation_chain, self.provider, self.kind):
            raise ValueError("Unjustified provider ping-pong is not allowed.")
        validate_job_request_permissions(kind=self.kind, sandbox=self.sandbox, metadata=self.metadata)

    @property
    def resolved_sandbox(self) -> SandboxMode:
        if self.sandbox:
            return self.sandbox
        return "read-only" if self.kind in READ_ONLY_KINDS else "workspace-write"

    @property
    def next_delegation_chain(self) -> list[DelegationStep]:
        return [*self.delegation_chain, (self.provider, self.kind)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "provider": self.provider,
            "kind": self.kind,
            "prompt": self.prompt,
            "cwd": self.cwd,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "sandbox": self.resolved_sandbox,
            "runtime_mode": self.runtime_mode,
            "max_depth": self.max_depth,
            "delegation_chain": [list(step) for step in self.delegation_chain],
            "failure_reason": self.failure_reason,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class JobResult:
    job_id: str
    status: JobStatus
    phase: JobPhase
    summary: str
    stdout: str | None = None
    stderr: str | None = None
    error: str | None = None
    raw_output: str | None = None
    parsed_payload: dict[str, Any] | None = None
    started_at: str | None = None
    completed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "phase": self.phase,
            "summary": self.summary,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
            "raw_output": self.raw_output,
            "parsed_payload": self.parsed_payload,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True, slots=True)
class AgentJob:
    id: str
    task_id: str
    provider: Provider
    kind: JobKind
    status: JobStatus
    phase: JobPhase
    prompt: str
    cwd: str
    sandbox: SandboxMode
    reasoning_effort: ReasoningEffort
    runtime_mode: RuntimeMode = "cli_inherit"
    model: str | None = None
    session_id: str | None = None
    thread_id: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    completed_at: str | None = None
    summary: str | None = None
    stdout: str | None = None
    raw_output: str | None = None
    parsed_payload: dict[str, Any] | None = None
    error: str | None = None
    command: list[str] = field(default_factory=list)
    exit_code: int | None = None
    stderr: str | None = None
    pid: int | None = None
    delegation_chain: list[DelegationStep] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "provider": self.provider,
            "kind": self.kind,
            "status": self.status,
            "phase": self.phase,
            "prompt": self.prompt,
            "cwd": self.cwd,
            "sandbox": self.sandbox,
            "reasoning_effort": self.reasoning_effort,
            "runtime_mode": self.runtime_mode,
            "model": self.model,
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "summary": self.summary,
            "stdout": self.stdout,
            "raw_output": self.raw_output,
            "parsed_payload": self.parsed_payload,
            "error": self.error,
            "command": self.command,
            "exit_code": self.exit_code,
            "stderr": self.stderr,
            "pid": self.pid,
            "delegation_chain": [list(step) for step in self.delegation_chain],
            "messages": self.messages,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentJob:
        return cls(
            id=data["id"],
            task_id=data["task_id"],
            provider=data["provider"],
            kind=data["kind"],
            status=data["status"],
            phase=data["phase"],
            prompt=data["prompt"],
            cwd=data["cwd"],
            sandbox=data["sandbox"],
            reasoning_effort=data["reasoning_effort"],
            runtime_mode=data.get("runtime_mode", "cli_inherit"),
            model=data.get("model"),
            session_id=data.get("session_id"),
            thread_id=data.get("thread_id"),
            created_at=data["created_at"],
            updated_at=data.get("updated_at", data["created_at"]),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            summary=data.get("summary"),
            stdout=data.get("stdout"),
            raw_output=data.get("raw_output"),
            parsed_payload=data.get("parsed_payload"),
            error=data.get("error"),
            command=list(data.get("command", [])),
            exit_code=data.get("exit_code"),
            stderr=data.get("stderr"),
            pid=data.get("pid"),
            delegation_chain=[tuple(step) for step in data.get("delegation_chain", [])],
            messages=list(data.get("messages", [])),
            metadata=dict(data.get("metadata", {})),
        )

    def result(self) -> JobResult:
        return JobResult(
            job_id=self.id,
            status=self.status,
            phase=self.phase,
            summary=self.summary or "",
            stdout=self.stdout,
            stderr=self.stderr,
            error=self.error,
            raw_output=self.raw_output,
            parsed_payload=self.parsed_payload,
            started_at=self.started_at,
            completed_at=self.completed_at,
        )


class JobRuntime(Protocol):
    def start(self, request: JobRequest) -> AgentJob:
        """Start a job."""

    def status(self, job_id: str) -> AgentJob:
        """Return current job state."""

    def result(self, job_id: str) -> JobResult:
        """Return a job result."""

    def send(self, job_id: str, message: str) -> AgentJob:
        """Send a follow-up message to a job."""

    def cancel(self, job_id: str) -> AgentJob:
        """Cancel a job."""


@dataclass(slots=True)
class InMemoryJobRuntime:
    jobs: dict[str, AgentJob] = field(default_factory=dict)

    def start(self, request: JobRequest) -> AgentJob:
        validate_runtime_start(request)
        now = now_iso()
        job = AgentJob(
            id=new_job_id(),
            task_id=request.task_id,
            provider=request.provider,
            kind=request.kind,
            status="running",
            phase="starting",
            prompt=request.prompt,
            cwd=request.cwd,
            sandbox=request.resolved_sandbox,
            reasoning_effort=request.reasoning_effort,
            runtime_mode=request.runtime_mode,
            model=request.model,
            session_id=f"{request.provider}-session-{uuid4().hex[:8]}",
            thread_id=f"{request.provider}-thread-{uuid4().hex[:8]}",
            created_at=now,
            updated_at=now,
            started_at=now,
            summary=f"{request.provider} {request.kind} job started.",
            delegation_chain=request.next_delegation_chain,
            metadata=request.metadata,
        )
        self.jobs[job.id] = job
        return job

    def status(self, job_id: str) -> AgentJob:
        return self._get(job_id)

    def result(self, job_id: str) -> JobResult:
        return self._get(job_id).result()

    def send(self, job_id: str, message: str) -> AgentJob:
        job = self._get(job_id)
        if job.status in TERMINAL_STATUSES:
            return _with_operation(job, action="send", status="already_terminal", detail="Job is already terminal.")
        updated = replace(
            job,
            messages=[*job.messages, message],
            status="running",
            phase="working",
            updated_at=now_iso(),
        )
        updated = _with_operation(updated, action="send", status="accepted", detail="Message accepted by mock runtime.")
        self.jobs[job_id] = updated
        return updated

    def cancel(self, job_id: str) -> AgentJob:
        job = self._get(job_id)
        if job.status in TERMINAL_STATUSES:
            return _with_operation(job, action="cancel", status="already_terminal", detail="Job is already terminal.")
        timestamp = now_iso()
        updated = replace(
            job,
            status="cancelled",
            phase="cancelled",
            completed_at=timestamp,
            updated_at=timestamp,
            summary=job.summary or "Job cancelled.",
        )
        updated = _with_operation(updated, action="cancel", status="accepted", detail="Job cancellation accepted.")
        self.jobs[job_id] = updated
        return updated

    def complete(
        self,
        job_id: str,
        *,
        summary: str,
        stdout: str | None = None,
        stderr: str | None = None,
        raw_output: str | None = None,
        parsed_payload: dict[str, Any] | None = None,
        exit_code: int | None = None,
        phase: JobPhase = "done",
    ) -> AgentJob:
        job = self._get(job_id)
        return self._store_terminal(
            replace(
                job,
                status="completed",
                phase=phase,
                summary=summary,
                stdout=stdout,
                stderr=stderr,
                raw_output=raw_output if raw_output is not None else stdout,
                parsed_payload=parsed_payload,
                error=None,
                exit_code=exit_code,
                completed_at=now_iso(),
                updated_at=now_iso(),
            )
        )

    def fail(
        self,
        job_id: str,
        *,
        summary: str,
        error: str,
        stdout: str | None = None,
        stderr: str | None = None,
        raw_output: str | None = None,
        parsed_payload: dict[str, Any] | None = None,
        exit_code: int | None = None,
    ) -> AgentJob:
        job = self._get(job_id)
        return self._store_terminal(
            replace(
                job,
                status="failed",
                phase="failed",
                summary=summary,
                stdout=stdout,
                stderr=stderr,
                raw_output=raw_output if raw_output is not None else stdout,
                parsed_payload=parsed_payload,
                error=error,
                exit_code=exit_code,
                completed_at=now_iso(),
                updated_at=now_iso(),
            )
        )

    def _get(self, job_id: str) -> AgentJob:
        try:
            return self.jobs[job_id]
        except KeyError as exc:
            raise KeyError(f"Unknown job id: {job_id}") from exc

    def _store_terminal(self, job: AgentJob) -> AgentJob:
        self.jobs[job.id] = job
        return job


@dataclass(slots=True)
class FileJobRuntime:
    root: Path | str = ".agent_orchestrator/jobs"

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    def start(self, request: JobRequest) -> AgentJob:
        validate_runtime_start(request)
        now = now_iso()
        job = AgentJob(
            id=new_job_id(),
            task_id=request.task_id,
            provider=request.provider,
            kind=request.kind,
            status="running",
            phase="starting",
            prompt=request.prompt,
            cwd=request.cwd,
            sandbox=request.resolved_sandbox,
            reasoning_effort=request.reasoning_effort,
            runtime_mode=request.runtime_mode,
            model=request.model,
            session_id=f"{request.provider}-session-{uuid4().hex[:8]}",
            thread_id=f"{request.provider}-thread-{uuid4().hex[:8]}",
            created_at=now,
            updated_at=now,
            started_at=now,
            summary=f"{request.provider} {request.kind} job started.",
            delegation_chain=request.next_delegation_chain,
            metadata=request.metadata,
        )
        self._write_job(job)
        self._append_log(job.id, f"started: {request.provider} {request.kind}")
        self._update_index(job.id)
        return job

    def status(self, job_id: str) -> AgentJob:
        return self._read_job(job_id)

    def result(self, job_id: str) -> JobResult:
        return self._read_job(job_id).result()

    def send(self, job_id: str, message: str) -> AgentJob:
        job = self._read_job(job_id)
        if job.status in TERMINAL_STATUSES:
            return self._store_operation(
                job,
                action="send",
                status="already_terminal",
                detail="Job is already terminal.",
            )
        updated = replace(
            job,
            messages=[*job.messages, message],
            status="running",
            phase="working",
            updated_at=now_iso(),
        )
        updated = _with_operation(updated, action="send", status="accepted", detail="Message recorded for job.")
        self._write_job(updated)
        self._append_log(job_id, f"message: {message}")
        return updated

    def cancel(self, job_id: str) -> AgentJob:
        job = self._read_job(job_id)
        if job.status in TERMINAL_STATUSES:
            return self._store_operation(
                job,
                action="cancel",
                status="already_terminal",
                detail="Job is already terminal.",
            )
        timestamp = now_iso()
        updated = replace(
            job,
            status="cancelled",
            phase="cancelled",
            completed_at=timestamp,
            updated_at=timestamp,
            summary=job.summary or "Job cancelled.",
        )
        updated = _with_operation(updated, action="cancel", status="accepted", detail="Job cancellation accepted.")
        self._write_job(updated)
        self._append_log(job_id, "cancelled")
        return updated

    def complete(
        self,
        job_id: str,
        *,
        summary: str,
        stdout: str | None = None,
        stderr: str | None = None,
        raw_output: str | None = None,
        parsed_payload: dict[str, Any] | None = None,
        exit_code: int | None = None,
        phase: JobPhase = "done",
    ) -> AgentJob:
        job = self._read_job(job_id)
        timestamp = now_iso()
        updated = replace(
            job,
            status="completed",
            phase=phase,
            summary=summary,
            stdout=stdout,
            stderr=stderr,
            raw_output=raw_output if raw_output is not None else stdout,
            parsed_payload=parsed_payload,
            error=None,
            exit_code=exit_code,
            completed_at=timestamp,
            updated_at=timestamp,
        )
        self._write_job(updated)
        if stdout:
            self._append_log(job_id, f"stdout:\n{stdout}")
        if stderr:
            self._append_log(job_id, f"stderr:\n{stderr}")
        return updated

    def fail(
        self,
        job_id: str,
        *,
        summary: str,
        error: str,
        stdout: str | None = None,
        stderr: str | None = None,
        raw_output: str | None = None,
        parsed_payload: dict[str, Any] | None = None,
        exit_code: int | None = None,
    ) -> AgentJob:
        job = self._read_job(job_id)
        timestamp = now_iso()
        updated = replace(
            job,
            status="failed",
            phase="failed",
            summary=summary,
            stdout=stdout,
            stderr=stderr,
            raw_output=raw_output if raw_output is not None else stdout,
            parsed_payload=parsed_payload,
            error=error,
            exit_code=exit_code,
            completed_at=timestamp,
            updated_at=timestamp,
        )
        self._write_job(updated)
        if stdout:
            self._append_log(job_id, f"stdout:\n{stdout}")
        if stderr:
            self._append_log(job_id, f"stderr:\n{stderr}")
        self._append_log(job_id, f"error: {error}")
        return updated

    def list_recent(self) -> list[AgentJob]:
        index_path = self.root / "index.json"
        if not index_path.exists():
            return []
        data = json.loads(index_path.read_text(encoding="utf-8"))
        return [self._read_job(job_id) for job_id in data.get("recent", []) if self._job_path(job_id).exists()]

    def _job_path(self, job_id: str) -> Path:
        return self.root / f"{job_id}.json"

    def _log_path(self, job_id: str) -> Path:
        return self.root / f"{job_id}.log"

    def _read_job(self, job_id: str) -> AgentJob:
        path = self._job_path(job_id)
        if not path.exists():
            raise KeyError(f"Unknown job id: {job_id}")
        return AgentJob.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def _store_operation(
        self,
        job: AgentJob,
        *,
        action: str,
        status: ProviderOperationStatus,
        detail: str,
        reason: str | None = None,
    ) -> AgentJob:
        updated = _with_operation(job, action=action, status=status, detail=detail, reason=reason)
        self._write_job(updated)
        return updated

    def _write_job(self, job: AgentJob) -> None:
        current_path = self._job_path(job.id)
        if current_path.exists():
            current = self._read_job(job.id)
            if current.status in TERMINAL_STATUSES and job.status not in TERMINAL_STATUSES:
                raise ValueError("Terminal jobs cannot be updated to non-terminal status.")

        tmp_path = self.root / f"{job.id}.{uuid4().hex}.tmp"
        tmp_path.write_text(json.dumps(job.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(current_path)

    def _append_log(self, job_id: str, message: str) -> None:
        with self._log_path(job_id).open("a", encoding="utf-8") as handle:
            handle.write(f"[{now_iso()}] {message}\n")

    def _update_index(self, job_id: str) -> None:
        index_path = self.root / "index.json"
        if index_path.exists():
            data = json.loads(index_path.read_text(encoding="utf-8"))
            recent = list(data.get("recent", []))
        else:
            recent = []
        next_recent = [job_id, *[entry for entry in recent if entry != job_id]][:20]
        tmp_path = self.root / f"index.{uuid4().hex}.tmp"
        tmp_path.write_text(
            json.dumps({"recent": next_recent, "updated_at": now_iso()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(index_path)


def _is_unjustified_ping_pong(
    chain: list[DelegationStep],
    provider: Provider,
    kind: JobKind,
) -> bool:
    if len(chain) < 2:
        return False
    previous_provider, previous_kind = chain[-1]
    before_provider, _ = chain[-2]
    return (
        before_provider == provider
        and previous_provider != provider
        and previous_kind == kind
        and kind != "rescue"
    )


def _with_operation(
    job: AgentJob,
    *,
    action: str,
    status: ProviderOperationStatus,
    detail: str,
    reason: str | None = None,
) -> AgentJob:
    operation = {
        "action": action,
        "status": status,
        "reason": reason or status,
        "detail": detail,
        "updated_at": now_iso(),
    }
    parsed_payload = dict(job.parsed_payload or {})
    parsed_payload["operation"] = operation
    return replace(job, parsed_payload=parsed_payload, updated_at=str(operation["updated_at"]))

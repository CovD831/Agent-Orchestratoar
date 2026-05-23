"""Command-based provider integration for real Claude/Codex runs."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from threading import Lock, Thread
from time import sleep
from typing import Any, Protocol

from agent_orchestrator.jobs import (
    AgentJob,
    FileJobRuntime,
    JobRequest,
    JobResult,
    TERMINAL_STATUSES,
    Provider,
    now_iso,
    new_job_id,
)


@dataclass(frozen=True, slots=True)
class CommandSpec:
    command: list[str]
    env: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    error: str | None = None


class ProviderSession(Protocol):
    session_id: str
    thread_id: str

    def poll(self) -> CommandResult | None:
        """Return a final command result when ready, otherwise None."""

    def wait(self, timeout: int | None = None) -> CommandResult:
        """Block until the provider session produces a terminal result."""

    def send(self, message: str) -> dict[str, Any]:
        """Send a follow-up message to the running session."""

    def cancel(self) -> dict[str, Any]:
        """Cancel the running session."""


class CommandRunner(Protocol):
    def run(self, command: list[str], *, cwd: str, env: dict[str, str] | None = None) -> CommandResult:
        """Run a command and capture output."""

    def spawn(self, command: list[str], *, cwd: str, env: dict[str, str] | None = None) -> ProviderSession:
        """Spawn a background session, when supported."""


@dataclass(slots=True)
class SubprocessCommandSession:
    command: list[str]
    process: subprocess.Popen[str]
    session_id: str
    thread_id: str
    sent_messages: list[str] = field(default_factory=list)

    @property
    def pid(self) -> int:
        return int(self.process.pid)

    def poll(self) -> CommandResult | None:
        if self.process.poll() is None:
            return None
        stdout, stderr = self.process.communicate()
        return CommandResult(
            command=self.command,
            exit_code=self.process.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
            error=None,
        )

    def wait(self, timeout: int | None = None) -> CommandResult:
        try:
            stdout, stderr = self.process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            return CommandResult(
                command=self.command,
                exit_code=None,
                stdout="",
                stderr="",
                error=f"Command timed out after {timeout} seconds.",
            )
        return CommandResult(
            command=self.command,
            exit_code=self.process.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
            error=None,
        )

    def send(self, message: str) -> dict[str, Any]:
        self.sent_messages.append(message)
        return {
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "message": message,
            "message_count": len(self.sent_messages),
            "status": "accepted",
        }

    def cancel(self) -> dict[str, Any]:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        return {
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "status": "cancelled",
        }


@dataclass(slots=True)
class SubprocessCommandRunner:
    timeout_seconds: int | None = None

    def spawn(
        self,
        command: list[str],
        *,
        cwd: str,
        env: dict[str, str] | None = None,
    ) -> SubprocessCommandSession:
        session_token = new_job_id().replace("job-", "session-")
        thread_token = new_job_id().replace("job-", "thread-")
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise FileNotFoundError(str(exc)) from exc
        return SubprocessCommandSession(
            command=command,
            process=process,
            session_id=session_token,
            thread_id=thread_token,
        )

    def run(self, command: list[str], *, cwd: str, env: dict[str, str] | None = None) -> CommandResult:
        session = self.spawn(command, cwd=cwd, env=env)
        return session.wait(timeout=self.timeout_seconds)


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    provider: Provider
    available: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "available": self.available,
            "detail": self.detail,
        }


@dataclass(slots=True)
class ProviderHealthCheck:
    runner: CommandRunner = field(default_factory=SubprocessCommandRunner)

    def check(self, provider: Provider) -> ProviderStatus:
        if provider == "mock":
            return ProviderStatus(provider=provider, available=True, detail="mock provider is always available")

        binary = _provider_binary(provider)
        if shutil.which(binary) is None:
            return ProviderStatus(provider=provider, available=False, detail=f"{binary} not found")

        result = self.runner.run([binary, "--version"], cwd=".")
        if result.error:
            return ProviderStatus(provider=provider, available=False, detail=result.error)
        if result.exit_code != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.exit_code}"
            return ProviderStatus(provider=provider, available=False, detail=detail)
        return ProviderStatus(provider=provider, available=True, detail=result.stdout.strip() or "ok")


@dataclass(slots=True)
class PromptRenderer:
    def render(self, request: JobRequest) -> str:
        return "\n\n".join(
            [
                f"Provider Intent\n{request.provider} should perform a {request.kind} job.",
                f"Task\n{request.prompt}",
                f"Context\n{request.metadata.get('context', 'No additional context provided.')}",
                f"Inputs\n{_format_list(request.metadata.get('inputs', []))}",
                f"Expected Output\n{_format_list(request.metadata.get('outputs', []))}",
                f"Acceptance Criteria\n{_format_list(request.metadata.get('acceptance_criteria', []))}",
                (
                    "Safety Boundaries\n"
                    f"Use sandbox={request.resolved_sandbox}. Do not exceed max_depth={request.max_depth}. "
                    "Do not auto-fix review findings unless this is an explicit rescue or implementation job."
                ),
                (
                    "Return Format\n"
                    "Return a concise summary, touched files if any, validation performed, and any blockers."
                ),
            ]
        )


class ProviderAdapter(Protocol):
    provider: Provider

    def build_command(self, request: JobRequest) -> CommandSpec:
        """Build a command for a provider."""

    def parse_result(self, request: JobRequest, result: CommandResult) -> tuple[str, dict[str, Any] | None, str | None]:
        """Return summary, parsed payload, and optional error message."""

    def parse_session_metadata(self, request: JobRequest, session: ProviderSession | None) -> dict[str, Any]:
        """Return session metadata to persist on the job."""

    def send_follow_up(self, job: AgentJob, message: str, session: ProviderSession | None) -> dict[str, Any]:
        """Send a follow-up through the provider session."""

    def cancel_session(self, job: AgentJob, session: ProviderSession | None) -> dict[str, Any]:
        """Cancel a provider session."""


@dataclass(slots=True)
class CodexCliAdapter:
    renderer: PromptRenderer = field(default_factory=PromptRenderer)
    provider: Provider = "codex"
    default_model: str = "gpt-5.4"

    def build_command(self, request: JobRequest) -> CommandSpec:
        prompt = self.renderer.render(request)
        return CommandSpec(
            command=[
                "codex",
                "exec",
                "--model",
                request.model or self.default_model,
                "--sandbox",
                request.resolved_sandbox,
                prompt,
            ]
        )

    def parse_result(self, request: JobRequest, result: CommandResult) -> tuple[str, dict[str, Any] | None, str | None]:
        summary = result.stdout.strip() or result.stderr.strip() or "Codex returned no output."
        return summary, {"stdout": result.stdout, "stderr": result.stderr}, None

    def parse_session_metadata(self, request: JobRequest, session: ProviderSession | None) -> dict[str, Any]:
        if session is None:
            return {}
        return {"session_id": session.session_id, "thread_id": session.thread_id}

    def send_follow_up(self, job: AgentJob, message: str, session: ProviderSession | None) -> dict[str, Any]:
        if session is None:
            return {"status": "unsupported", "message": message}
        return session.send(message)

    def cancel_session(self, job: AgentJob, session: ProviderSession | None) -> dict[str, Any]:
        if session is None:
            return {"status": "cancelled"}
        return session.cancel()


@dataclass(slots=True)
class ClaudeCodeAdapter:
    renderer: PromptRenderer = field(default_factory=PromptRenderer)
    provider: Provider = "claude"
    default_model: str = "sonnet"

    def build_command(self, request: JobRequest) -> CommandSpec:
        prompt = self.renderer.render(request)
        return CommandSpec(
            command=[
                "claude",
                "-p",
                "--output-format",
                "json",
                "--model",
                request.model or self.default_model,
                prompt,
            ]
        )

    def parse_result(self, request: JobRequest, result: CommandResult) -> tuple[str, dict[str, Any] | None, str | None]:
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if not stdout:
            return "", None, stderr or "Claude Code returned no JSON output."

        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError:
            combined = f"{stdout}\n{stderr}".strip()
            if _looks_like_auth_prompt(combined):
                return "", None, "Claude Code returned an authentication prompt. Sign in to Claude Code and retry."
            return "", None, "Claude Code returned non-JSON output. Verify the Claude Code installation and retry."

        summary = str(envelope.get("result") or "")
        payload = {"envelope": envelope}
        if envelope.get("is_error"):
            return summary, payload, summary or "Claude Code returned is_error=true."
        return summary, payload, None

    def parse_session_metadata(self, request: JobRequest, session: ProviderSession | None) -> dict[str, Any]:
        if session is None:
            return {}
        return {"session_id": session.session_id, "thread_id": session.thread_id}

    def send_follow_up(self, job: AgentJob, message: str, session: ProviderSession | None) -> dict[str, Any]:
        if session is None:
            return {"status": "unsupported", "message": message}
        return session.send(message)

    def cancel_session(self, job: AgentJob, session: ProviderSession | None) -> dict[str, Any]:
        if session is None:
            return {"status": "cancelled"}
        return session.cancel()


@dataclass(slots=True)
class CommandJobRuntime(FileJobRuntime):
    runner: CommandRunner = field(default_factory=SubprocessCommandRunner)
    adapters: dict[Provider, ProviderAdapter] = field(
        default_factory=lambda: {
            "codex": CodexCliAdapter(),
            "claude": ClaudeCodeAdapter(),
        }
    )
    poll_interval_seconds: float = 0.1
    poll_timeout_seconds: float = 600.0
    _sessions: dict[str, ProviderSession] = field(default_factory=dict, init=False, repr=False)
    _providers: dict[str, ProviderAdapter] = field(default_factory=dict, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def start(self, request: JobRequest) -> AgentJob:
        adapter = self.adapters.get(request.provider)
        if not adapter:
            raise ValueError(f"No command adapter configured for provider: {request.provider}")

        command_spec = adapter.build_command(request)
        job = AgentJob(
            id=new_job_id(),
            task_id=request.task_id,
            provider=request.provider,
            kind=request.kind,
            status="running",
            phase="working",
            prompt=request.prompt,
            cwd=request.cwd,
            sandbox=request.resolved_sandbox,
            reasoning_effort=request.reasoning_effort,
            model=request.model,
            started_at=now_iso(),
            updated_at=now_iso(),
            summary=f"{request.provider} {request.kind} job started.",
            command=command_spec.command,
            delegation_chain=request.next_delegation_chain,
            metadata=request.metadata,
        )
        self._write_job(job)
        self._update_index(job.id)
        self._append_log(job.id, f"starting: {' '.join(command_spec.command[:4])} ...")

        try:
            session = self._spawn_session(command_spec.command, request.cwd, command_spec.env)
        except Exception as exc:
            return self._fail_job_from_exception(
                job.id,
                command_spec.command,
                "Failed to spawn provider command",
                exc,
            ) or self._read_job(job.id)

        try:
            session_metadata = adapter.parse_session_metadata(request, session)
        except Exception as exc:
            return self._fail_job_from_exception(
                job.id,
                command_spec.command,
                "Failed to read provider session metadata",
                exc,
            ) or self._read_job(job.id)

        if session is not None:
            with self._lock:
                self._sessions[job.id] = session
                self._providers[job.id] = adapter

        if job.session_id is None:
            job = replace(job, session_id=f"session-{job.id}", thread_id=f"thread-{job.id}")
            self._write_job(job)

        if session_metadata:
            job = replace(
                job,
                session_id=str(session_metadata.get("session_id") or ""),
                thread_id=str(session_metadata.get("thread_id") or ""),
                parsed_payload=_merge_payload(job.parsed_payload, {"session": session_metadata}),
            )
            self._write_job(job)

        worker = Thread(
            target=self._run_background_job,
            args=(job.id, request, adapter, command_spec, session),
            daemon=True,
        )
        worker.start()
        return job

    def status(self, job_id: str) -> AgentJob:
        job = self._read_job(job_id)
        return self._refresh_job(job)

    def result(self, job_id: str) -> JobResult:
        return self.status(job_id).result()

    def send(self, job_id: str, message: str) -> AgentJob:
        job = self._read_job(job_id)
        if job.status in {"completed", "failed", "cancelled"}:
            return job
        with self._lock:
            session = self._sessions.get(job_id)
            adapter = self._providers.get(job_id)
        if adapter is None:
            return FileJobRuntime.send(self, job_id, message)

        payload = adapter.send_follow_up(job, message, session)
        updated = FileJobRuntime.send(self, job_id, message)
        refreshed = replace(
            updated,
            session_id=str(payload.get("session_id") or updated.session_id),
            thread_id=str(payload.get("thread_id") or updated.thread_id),
            parsed_payload=_merge_payload(updated.parsed_payload, {"follow_up": payload}),
        )
        self._write_job(refreshed)
        return refreshed

    def cancel(self, job_id: str) -> AgentJob:
        job = self.status(job_id)
        if job.status in {"completed", "failed", "cancelled"}:
            return job
        with self._lock:
            session = self._sessions.get(job_id)
            adapter = self._providers.get(job_id)
        payload = adapter.cancel_session(job, session) if adapter else {"status": "cancelled"}
        cancelled = FileJobRuntime.cancel(self, job_id)
        refreshed = replace(cancelled, parsed_payload=_merge_payload(cancelled.parsed_payload, {"cancel": payload}))
        self._write_job(refreshed)
        return refreshed

    def _spawn_session(self, command: list[str], cwd: str, env: dict[str, str] | None) -> ProviderSession | None:
        spawn = getattr(self.runner, "spawn", None)
        if callable(spawn):
            try:
                return spawn(command, cwd=cwd, env=env)
            except FileNotFoundError:
                return None
        return None

    def _run_background_job(
        self,
        job_id: str,
        request: JobRequest,
        adapter: ProviderAdapter,
        command_spec: CommandSpec,
        session: ProviderSession | None,
    ) -> None:
        try:
            if self.status(job_id).status in TERMINAL_STATUSES:
                return

            try:
                command_result = self._wait_for_command(command_spec, request, session)
            except Exception as exc:
                command_result = _command_result_from_exception(
                    command_spec.command,
                    "Provider command failed before producing a result",
                    exc,
                )

            if self.status(job_id).status in TERMINAL_STATUSES:
                return

            try:
                self._finalize_job(job_id, request, adapter, command_result)
            except Exception as exc:
                self._fail_job_from_exception(
                    job_id,
                    command_spec.command,
                    "Failed to finalize provider command",
                    exc,
                )
        except Exception as exc:
            self._fail_job_from_exception(
                job_id,
                command_spec.command,
                "Background command job failed",
                exc,
            )
        finally:
            with self._lock:
                self._sessions.pop(job_id, None)
                self._providers.pop(job_id, None)

    def _wait_for_command(
        self,
        command_spec: CommandSpec,
        request: JobRequest,
        session: ProviderSession | None,
    ) -> CommandResult:
        if session is not None:
            polls = max(1, int(self.poll_timeout_seconds / self.poll_interval_seconds))
            for _ in range(polls):
                result = session.poll()
                if result is not None:
                    return result
                sleep(self.poll_interval_seconds)
            return session.wait(timeout=int(self.poll_timeout_seconds))
        return self.runner.run(command_spec.command, cwd=request.cwd, env=command_spec.env)

    def _refresh_job(self, job: AgentJob) -> AgentJob:
        with self._lock:
            session = self._sessions.get(job.id)
            adapter = self._providers.get(job.id)
        if session is not None:
            pid = getattr(session, "pid", None)
            if pid is not None and job.pid != pid:
                job = replace(job, pid=int(pid), updated_at=now_iso())
                self._write_job(job)
            if job.status == "running":
                try:
                    result = session.poll()
                except Exception as exc:
                    return self._fail_job_from_exception(
                        job.id,
                        job.command,
                        "Provider session poll failed",
                        exc,
                    ) or self._read_job(job.id)
                if result is not None and adapter is not None:
                    request = JobRequest(
                        task_id=job.task_id,
                        provider=job.provider,
                        kind=job.kind,
                        prompt=job.prompt,
                        cwd=job.cwd,
                        model=job.model,
                        reasoning_effort=job.reasoning_effort,
                        sandbox=job.sandbox,
                        metadata=job.metadata,
                    )
                    try:
                        job = self._finalize_job(job.id, request, adapter, result)
                    except Exception as exc:
                        return self._fail_job_from_exception(
                            job.id,
                            job.command,
                            "Failed to finalize provider command",
                            exc,
                        ) or self._read_job(job.id)
        return job

    def _finalize_job(
        self,
        job_id: str,
        request: JobRequest,
        adapter: ProviderAdapter,
        command_result: CommandResult,
    ) -> AgentJob:
        current = self._read_job(job_id)
        summary, parsed_payload, parse_error = adapter.parse_result(request, command_result)
        failed = command_result.error is not None or command_result.exit_code != 0 or parse_error is not None
        error = command_result.error or parse_error
        if command_result.exit_code not in (0, None) and not error:
            error = command_result.stderr.strip() or command_result.stdout.strip() or f"exit {command_result.exit_code}"
        if failed and not summary:
            summary = error or command_result.stderr.strip() or command_result.stdout.strip() or "Provider job failed."

        merged_payload = _merge_payload(current.parsed_payload, parsed_payload)
        if failed:
            return self.fail(
                job_id,
                summary=summary,
                error=error or "Provider job failed.",
                stdout=command_result.stdout,
                stderr=command_result.stderr,
                raw_output=command_result.stdout,
                parsed_payload=merged_payload,
                exit_code=command_result.exit_code,
            )
        return self.complete(
            job_id,
            summary=summary,
            stdout=command_result.stdout,
            stderr=command_result.stderr,
            raw_output=command_result.stdout,
            parsed_payload=merged_payload,
            exit_code=command_result.exit_code,
            phase="done",
        )

    def _fail_job_from_exception(
        self,
        job_id: str,
        command: list[str],
        context: str,
        exc: Exception,
    ) -> AgentJob | None:
        message = _format_exception(context, exc)
        payload = {
            "exception": {
                "context": context,
                "type": type(exc).__name__,
                "message": str(exc),
                "command": command,
            }
        }
        try:
            current = self._read_job(job_id)
        except Exception:
            return None
        if current.status in TERMINAL_STATUSES:
            return current
        try:
            return self.fail(
                job_id,
                summary=message,
                error=message,
                stdout="",
                stderr=message,
                raw_output="",
                parsed_payload=_merge_payload(current.parsed_payload, payload),
                exit_code=None,
            )
        except Exception:
            return None


def _provider_binary(provider: Provider) -> str:
    if provider == "claude":
        return "claude"
    if provider == "codex":
        return "codex"
    return provider


def _format_list(value: object) -> str:
    if isinstance(value, list) and value:
        return "\n".join(f"- {entry}" for entry in value)
    return "- None"


def _looks_like_auth_prompt(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in ["log in", "login", "sign in", "authenticate", "auth token", "token expired"])


def _merge_payload(current: dict[str, Any] | None, incoming: dict[str, Any] | None) -> dict[str, Any] | None:
    if current is None and incoming is None:
        return None
    merged: dict[str, Any] = {}
    if current:
        merged.update(current)
    if incoming:
        merged.update(incoming)
    return merged


def _command_result_from_exception(command: list[str], context: str, exc: Exception) -> CommandResult:
    message = _format_exception(context, exc)
    return CommandResult(
        command=command,
        exit_code=None,
        stdout="",
        stderr=message,
        error=message,
    )


def _format_exception(context: str, exc: Exception) -> str:
    detail = str(exc).strip()
    if detail:
        return f"{context}: {type(exc).__name__}: {detail}"
    return f"{context}: {type(exc).__name__}"

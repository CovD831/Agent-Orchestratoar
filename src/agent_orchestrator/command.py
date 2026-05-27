"""Command-based provider integration for real Claude/Codex runs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
from agent_orchestrator.guards import validate_runtime_start


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


class DirectApiClient(Protocol):
    def complete(self, request: JobRequest) -> dict[str, Any]:
        """Run a single-turn provider API request and return a structured payload."""


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
    binary: str | None = None
    recommended_fallback: Provider | None = None
    cache_tier: str = "live"
    cached_at: str | None = None
    expires_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "available": self.available,
            "detail": self.detail,
            "binary": self.binary,
            "recommended_fallback": self.recommended_fallback,
            "cache_tier": self.cache_tier,
            "cached_at": self.cached_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object], *, cache_tier: str) -> "ProviderStatus":
        return cls(
            provider=str(data.get("provider", "mock")),  # type: ignore[arg-type]
            available=bool(data.get("available", False)),
            detail=str(data.get("detail", "")),
            binary=str(data["binary"]) if data.get("binary") is not None else None,
            recommended_fallback=str(data["recommended_fallback"]) if data.get("recommended_fallback") is not None else None,  # type: ignore[arg-type]
            cache_tier=cache_tier,
            cached_at=str(data["cached_at"]) if data.get("cached_at") is not None else None,
            expires_at=str(data["expires_at"]) if data.get("expires_at") is not None else None,
        )


_MEMORY_PROVIDER_HEALTH_CACHE: dict[tuple[str, Provider], dict[str, object]] = {}


@dataclass(slots=True)
class ProviderHealthCheck:
    runner: CommandRunner = field(default_factory=SubprocessCommandRunner)
    cache_path: Path | None = None
    ttl_seconds: int = 60
    use_cache: bool = False

    def check(self, provider: Provider, *, refresh: bool = False) -> ProviderStatus:
        if self.use_cache and not refresh:
            cached = self._read_cached(provider)
            if cached is not None:
                return cached

        status = self._check_live(provider)
        if self.use_cache:
            status = self._write_cached(status)
        return status

    def _check_live(self, provider: Provider) -> ProviderStatus:
        if provider == "mock":
            return ProviderStatus(
                provider=provider,
                available=True,
                detail="mock provider is always available",
                binary=None,
                recommended_fallback=None,
            )

        binary = _provider_binary(provider)
        fallback = _recommended_provider_fallback(provider)
        if shutil.which(binary) is None:
            return ProviderStatus(
                provider=provider,
                available=False,
                detail=f"{binary} not found",
                binary=binary,
                recommended_fallback=fallback,
            )

        result = self.runner.run([binary, "--version"], cwd=".")
        if result.error:
            return ProviderStatus(
                provider=provider,
                available=False,
                detail=result.error,
                binary=binary,
                recommended_fallback=fallback,
            )
        if result.exit_code != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.exit_code}"
            return ProviderStatus(
                provider=provider,
                available=False,
                detail=detail,
                binary=binary,
                recommended_fallback=fallback,
            )
        return ProviderStatus(
            provider=provider,
            available=True,
            detail=result.stdout.strip() or "ok",
            binary=binary,
            recommended_fallback=None,
        )

    def _read_cached(self, provider: Provider) -> ProviderStatus | None:
        cache_key = self._cache_key(provider)
        cached = _MEMORY_PROVIDER_HEALTH_CACHE.get(cache_key)
        if _cache_entry_valid(cached):
            status = cached.get("status", {}) if isinstance(cached, dict) else {}
            return ProviderStatus.from_dict(status, cache_tier="memory") if isinstance(status, dict) else None

        path = self._resolved_cache_path()
        if path is None or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        entries = payload.get("entries", {}) if isinstance(payload, dict) else {}
        entry = entries.get(provider) if isinstance(entries, dict) else None
        if not _cache_entry_valid(entry):
            return None
        _MEMORY_PROVIDER_HEALTH_CACHE[cache_key] = dict(entry)
        status = entry.get("status", {}) if isinstance(entry, dict) else {}
        return ProviderStatus.from_dict(status, cache_tier="disk") if isinstance(status, dict) else None

    def _write_cached(self, status: ProviderStatus) -> ProviderStatus:
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=max(1, int(self.ttl_seconds)))
        cached_status = ProviderStatus(
            provider=status.provider,
            available=status.available,
            detail=status.detail,
            binary=status.binary,
            recommended_fallback=status.recommended_fallback,
            cache_tier="live",
            cached_at=now.isoformat(),
            expires_at=expires.isoformat(),
        )
        entry = {
            "expires_at_epoch": expires.timestamp(),
            "status": cached_status.to_dict(),
        }
        _MEMORY_PROVIDER_HEALTH_CACHE[self._cache_key(status.provider)] = entry
        path = self._resolved_cache_path()
        if path is None:
            return cached_status
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": "1.0", "entries": {}}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    payload.update(existing)
            except Exception:
                payload = {"schema_version": "1.0", "entries": {}}
        entries = payload.get("entries", {})
        if not isinstance(entries, dict):
            entries = {}
        entries[status.provider] = entry
        payload["entries"] = entries
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(path)
        return cached_status

    def _resolved_cache_path(self) -> Path | None:
        if not self.use_cache:
            return None
        return self.cache_path or Path(".agent_orchestrator") / "cache" / "provider-health.json"

    def _cache_key(self, provider: Provider) -> tuple[str, Provider]:
        path = self._resolved_cache_path()
        return (str(path.resolve()) if path else "memory-only", provider)


@dataclass(slots=True)
class StaticDirectApiClient:
    """Fakeable direct API client used until provider SDKs are wired in."""

    def complete(self, request: JobRequest) -> dict[str, Any]:
        return {
            "summary": f"Direct API {request.provider} {request.kind} completed.",
            "usage": {"input_tokens": None, "output_tokens": None, "source": "not_reported"},
        }


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
                    f"Runtime mode is {request.runtime_mode}. "
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
        command = [
            "codex",
            "exec",
            "--model",
            request.model or self.default_model,
            "--sandbox",
            request.resolved_sandbox,
        ]
        pilot = _codex_pilot_metadata(request)
        if pilot.get("json_events"):
            command.append("--json")
        output_last_message = pilot.get("output_last_message")
        if isinstance(output_last_message, str) and output_last_message:
            command.extend(["--output-last-message", output_last_message])
        return CommandSpec(command=[*command, prompt])

    def parse_result(self, request: JobRequest, result: CommandResult) -> tuple[str, dict[str, Any] | None, str | None]:
        pilot = _codex_pilot_metadata(request)
        if pilot.get("json_events"):
            parsed = _parse_codex_exec_jsonl(result.stdout)
            final_message = _read_optional_text(pilot.get("output_last_message"))
            summary = final_message or parsed.get("final_message") or result.stderr.strip() or "Codex returned no final message."
            payload = {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "codex_exec_json": parsed,
                "provider_session_ref": {
                    "format": "agent_orchestrator.provider_session_ref.v1",
                    "provider": self.provider,
                    "runtime_id": "codex_exec_json",
                    "session_id": parsed.get("session_id"),
                    "thread_id": parsed.get("thread_id"),
                    "cwd": request.cwd,
                    "provider_owned": True,
                    "continuation_guarantee": "provider_owned",
                },
                "codex_pilot": {
                    "runtime_id": "codex_exec_json",
                    "json_events": True,
                    "output_last_message": pilot.get("output_last_message"),
                    "final_message_source": "output_last_message" if final_message else parsed.get("final_message_source"),
                    "usage_cost_policy": "placeholder unless codex reports usage directly",
                },
            }
            if parsed.get("usage"):
                payload["usage"] = parsed["usage"]
            return summary, payload, None
        summary = result.stdout.strip() or result.stderr.strip() or "Codex returned no output."
        return summary, {"stdout": result.stdout, "stderr": result.stderr}, None

    def parse_session_metadata(self, request: JobRequest, session: ProviderSession | None) -> dict[str, Any]:
        if session is None:
            return {}
        return {"session_id": session.session_id, "thread_id": session.thread_id}

    def send_follow_up(self, job: AgentJob, message: str, session: ProviderSession | None) -> dict[str, Any]:
        if session is None:
            return {"status": "session_missing", "reason": "session_missing", "detail": "No live Codex session is attached.", "message": message}
        return session.send(message)

    def cancel_session(self, job: AgentJob, session: ProviderSession | None) -> dict[str, Any]:
        if session is None:
            return {"status": "session_missing", "reason": "session_missing", "detail": "No live Codex session is attached."}
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
            return {"status": "session_missing", "reason": "session_missing", "detail": "No live Claude session is attached.", "message": message}
        return session.send(message)

    def cancel_session(self, job: AgentJob, session: ProviderSession | None) -> dict[str, Any]:
        if session is None:
            return {"status": "session_missing", "reason": "session_missing", "detail": "No live Claude session is attached."}
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
        validate_runtime_start(request)
        adapter = self.adapters.get(request.provider)
        if not adapter:
            raise ValueError(f"No command adapter configured for provider: {request.provider}")

        command_spec = adapter.build_command(request)
        job_id = new_job_id()
        runtime_env, runtime_metadata = _runtime_environment(request, self.root, job_id)
        command_spec = CommandSpec(
            command=command_spec.command,
            env=_merge_env(command_spec.env, runtime_env),
        )
        job = AgentJob(
            id=job_id,
            task_id=request.task_id,
            provider=request.provider,
            kind=request.kind,
            status="running",
            phase="working",
            prompt=request.prompt,
            cwd=request.cwd,
            sandbox=request.resolved_sandbox,
            reasoning_effort=request.reasoning_effort,
            runtime_mode=request.runtime_mode,
            model=request.model,
            started_at=now_iso(),
            updated_at=now_iso(),
            summary=f"{request.provider} {request.kind} job started.",
            command=command_spec.command,
            delegation_chain=request.next_delegation_chain,
            metadata={**request.metadata, "runtime_mode": runtime_metadata},
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
            return self._store_operation(
                job,
                action="send",
                status="already_terminal",
                detail="Job is already terminal.",
            )
        with self._lock:
            session = self._sessions.get(job_id)
            adapter = self._providers.get(job_id)
        if adapter is None:
            return FileJobRuntime.send(self, job_id, message)

        payload = _normalize_provider_operation(adapter.send_follow_up(job, message, session), action="send")
        updated = FileJobRuntime.send(self, job_id, message)
        payload = {
            **payload,
            "job_id": updated.id,
            "provider": updated.provider,
            "runtime_mode": updated.runtime_mode,
            "session_id": payload.get("session_id") or updated.session_id,
            "thread_id": payload.get("thread_id") or updated.thread_id,
            "terminal_state": updated.status in TERMINAL_STATUSES,
        }
        receipts = []
        if updated.parsed_payload and isinstance(updated.parsed_payload.get("runtime_operation_receipts"), list):
            receipts = list(updated.parsed_payload.get("runtime_operation_receipts", []))
        refreshed = replace(
            updated,
            session_id=str(payload.get("session_id") or updated.session_id),
            thread_id=str(payload.get("thread_id") or updated.thread_id),
            parsed_payload=_merge_payload(
                updated.parsed_payload,
                {"follow_up": payload, "operation": payload, "runtime_operation_receipts": [*receipts, payload][-10:]},
            ),
        )
        self._write_job(refreshed)
        return refreshed

    def cancel(self, job_id: str) -> AgentJob:
        job = self.status(job_id)
        if job.status in {"completed", "failed", "cancelled"}:
            return self._store_operation(
                job,
                action="cancel",
                status="already_terminal",
                detail="Job is already terminal.",
            )
        with self._lock:
            session = self._sessions.get(job_id)
            adapter = self._providers.get(job_id)
        payload = _normalize_provider_operation(
            adapter.cancel_session(job, session) if adapter else {"status": "accepted", "detail": "Fallback file runtime cancellation accepted."},
            action="cancel",
        )
        cancelled = FileJobRuntime.cancel(self, job_id)
        payload = {
            **payload,
            "job_id": cancelled.id,
            "provider": cancelled.provider,
            "runtime_mode": cancelled.runtime_mode,
            "session_id": payload.get("session_id") or cancelled.session_id,
            "thread_id": payload.get("thread_id") or cancelled.thread_id,
            "terminal_state": True,
        }
        receipts = []
        if cancelled.parsed_payload and isinstance(cancelled.parsed_payload.get("runtime_operation_receipts"), list):
            receipts = list(cancelled.parsed_payload.get("runtime_operation_receipts", []))
        refreshed = replace(
            cancelled,
            parsed_payload=_merge_payload(
                cancelled.parsed_payload,
                {"cancel": payload, "operation": payload, "runtime_operation_receipts": [*receipts, payload][-10:]},
            ),
        )
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
                        runtime_mode=job.runtime_mode,
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


@dataclass(slots=True)
class DirectApiJobRuntime(FileJobRuntime):
    client: DirectApiClient = field(default_factory=StaticDirectApiClient)

    def start(self, request: JobRequest) -> AgentJob:
        validate_runtime_start(request)
        if request.runtime_mode != "direct_api":
            request = replace(request, runtime_mode="direct_api")
        job = FileJobRuntime.start(self, request)
        auth = direct_api_auth_status(request.provider)
        metadata = {
            **job.metadata,
            "runtime_mode": {
                "mode": "direct_api",
                "inherits_user_config": False,
                "config_source": "environment_api_key",
                "project_cwd": request.cwd,
                "sandbox": request.resolved_sandbox,
                "direct_api_tool_loop": False,
            },
            "direct_api_auth": auth,
        }
        job = replace(job, runtime_mode="direct_api", metadata=metadata)
        self._write_job(job)
        if not auth["available"]:
            return self.fail(
                job.id,
                summary=f"{request.provider} direct API authentication is required.",
                error="auth_required",
                parsed_payload={
                    "provider": request.provider,
                    "kind": request.kind,
                    "auth": auth,
                    "runtime_mode": "direct_api",
                },
            )
        payload = self.client.complete(request)
        summary = str(payload.get("summary") or f"{request.provider} direct API job completed.")
        parsed_payload = {
            "provider": request.provider,
            "model": request.model,
            "kind": request.kind,
            "summary": summary,
            "usage": payload.get("usage", {"input_tokens": None, "output_tokens": None, "source": "not_reported"}),
            "runtime_mode": "direct_api",
            "tool_loop": False,
        }
        return self.complete(
            job.id,
            summary=summary,
            stdout=summary,
            raw_output=summary,
            parsed_payload=parsed_payload,
            phase="done",
        )


@dataclass(slots=True)
class RuntimeModeRouter:
    cli_runtime: CommandJobRuntime = field(default_factory=CommandJobRuntime)
    direct_api_runtime: DirectApiJobRuntime = field(default_factory=DirectApiJobRuntime)
    _routes: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    @property
    def adapters(self) -> dict[Provider, ProviderAdapter]:
        return self.cli_runtime.adapters

    def start(self, request: JobRequest) -> AgentJob:
        if request.runtime_mode == "direct_api":
            job = self.direct_api_runtime.start(request)
            self._routes[job.id] = "direct_api"
            return job
        job = self.cli_runtime.start(request)
        self._routes[job.id] = "cli"
        return job

    def status(self, job_id: str) -> AgentJob:
        return self._runtime(job_id).status(job_id)

    def result(self, job_id: str) -> JobResult:
        return self._runtime(job_id).result(job_id)

    def send(self, job_id: str, message: str) -> AgentJob:
        return self._runtime(job_id).send(job_id, message)

    def cancel(self, job_id: str) -> AgentJob:
        return self._runtime(job_id).cancel(job_id)

    def _runtime(self, job_id: str) -> FileJobRuntime:
        if self._routes.get(job_id) == "direct_api":
            return self.direct_api_runtime
        return self.cli_runtime


def _provider_binary(provider: Provider) -> str:
    if provider == "claude":
        return "claude"
    if provider == "codex":
        return "codex"
    return provider


def direct_api_auth_status(provider: Provider) -> dict[str, object]:
    key_name = "OPENAI_API_KEY" if provider == "codex" else "ANTHROPIC_API_KEY" if provider == "claude" else None
    if key_name is None:
        return {
            "provider": provider,
            "available": True,
            "status": "not_required",
            "key_name": None,
            "masked": None,
        }
    value = os.environ.get(key_name, "")
    present = bool(value)
    return {
        "provider": provider,
        "available": present,
        "status": "present" if present else "auth_required",
        "key_name": key_name,
        "masked": _mask_secret(value) if present else None,
    }


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:3]}...{value[-4:]}"


def _recommended_provider_fallback(provider: Provider) -> Provider | None:
    if provider == "codex":
        return "claude"
    if provider == "claude":
        return "codex"
    return "mock"


def _runtime_mode_metadata(request: JobRequest, jobs_root: Path) -> dict[str, object]:
    inherited = request.runtime_mode == "cli_inherit"
    return {
        "mode": request.runtime_mode,
        "inherits_user_config": inherited,
        "config_source": "user_and_project_cli_config" if inherited else request.runtime_mode,
        "effective_home": os.path.expanduser("~") if inherited else None,
        "project_cwd": request.cwd,
        "sandbox": request.resolved_sandbox,
        "jobs_root": str(jobs_root),
        "direct_api_tool_loop": False,
    }


def _runtime_environment(request: JobRequest, jobs_root: Path, job_id: str) -> tuple[dict[str, str] | None, dict[str, object]]:
    if request.runtime_mode != "cli_isolated":
        return None, _runtime_mode_metadata(request, jobs_root)
    runtime_home = jobs_root.parent / "runtime-homes" / job_id
    runtime_home.mkdir(parents=True, exist_ok=True)
    env = {
        "HOME": str(runtime_home),
        "XDG_CONFIG_HOME": str(runtime_home / ".config"),
        "XDG_CACHE_HOME": str(runtime_home / ".cache"),
    }
    metadata = _runtime_mode_metadata(request, jobs_root)
    metadata.update(
        {
            "effective_home": str(runtime_home),
            "config_source": "isolated_runtime_home",
            "runtime_home": str(runtime_home),
            "xdg_config_home": env["XDG_CONFIG_HOME"],
            "xdg_cache_home": env["XDG_CACHE_HOME"],
        }
    )
    return env, metadata


def _merge_env(base: dict[str, str] | None, overlay: dict[str, str] | None) -> dict[str, str] | None:
    if not base and not overlay:
        return None
    merged = dict(os.environ)
    if base:
        merged.update(base)
    if overlay:
        merged.update(overlay)
    return merged


def _cache_entry_valid(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    expires = entry.get("expires_at_epoch")
    if not isinstance(expires, int | float):
        return False
    return expires > datetime.now(UTC).timestamp()


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


def _codex_pilot_metadata(request: JobRequest) -> dict[str, Any]:
    pilot = request.metadata.get("codex_pilot") if isinstance(request.metadata, dict) else None
    return dict(pilot) if isinstance(pilot, dict) else {}


def _parse_codex_exec_jsonl(stdout: str) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    malformed_count = 0
    final_message: str | None = None
    final_message_source: str | None = None
    session_id: str | None = None
    thread_id: str | None = None
    usage: dict[str, Any] | None = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            malformed_count += 1
            continue
        if not isinstance(event, dict):
            malformed_count += 1
            continue
        events.append(event)
        session_id = session_id or _first_text(event, "session_id", "sessionId", "conversation_id", "conversationId")
        thread_id = thread_id or _first_text(event, "thread_id", "threadId", "conversation_id", "conversationId")
        usage = usage or _extract_usage(event)
        candidate = _extract_codex_final_message(event)
        if candidate:
            final_message = candidate
            final_message_source = str(event.get("type") or event.get("event") or event.get("kind") or "json_event")
    status_counts: dict[str, int] = {}
    for event in events:
        key = str(event.get("type") or event.get("event") or event.get("kind") or "unknown")
        status_counts[key] = status_counts.get(key, 0) + 1
    return {
        "format": "agent_orchestrator.codex_exec_json.v1",
        "event_count": len(events),
        "malformed_event_count": malformed_count,
        "event_types": status_counts,
        "events": events[-20:],
        "final_message": final_message,
        "final_message_source": final_message_source,
        "session_id": session_id,
        "thread_id": thread_id,
        "usage": usage,
    }


def _extract_codex_final_message(event: dict[str, Any]) -> str | None:
    for key in ("final_message", "last_message", "message", "text", "content", "output"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    for key in ("text", "content", "message"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_text(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_usage(event: dict[str, Any]) -> dict[str, Any] | None:
    usage = event.get("usage") if isinstance(event.get("usage"), dict) else None
    if usage is None:
        return None
    normalized = {
        "input_tokens": usage.get("input_tokens") or usage.get("prompt_tokens"),
        "output_tokens": usage.get("output_tokens") or usage.get("completion_tokens"),
        "estimated_cost_usd": usage.get("estimated_cost_usd"),
        "source": usage.get("source") or "codex_exec_json",
    }
    return normalized


def _read_optional_text(path_value: object) -> str | None:
    if not isinstance(path_value, str) or not path_value:
        return None
    path = Path(path_value)
    if not path.exists() or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _normalize_provider_operation(payload: dict[str, Any], *, action: str) -> dict[str, Any]:
    raw_status = str(payload.get("status") or "")
    status_map = {
        "accepted": "accepted",
        "cancelled": "accepted",
        "unsupported": "unsupported",
        "session_missing": "session_missing",
        "auth_required": "auth_required",
        "provider_unavailable": "provider_unavailable",
        "already_terminal": "already_terminal",
    }
    detail = str(payload.get("detail") or payload.get("error") or payload.get("message") or raw_status or "Operation completed.")
    normalized = dict(payload)
    timestamp = now_iso()
    normalized["action"] = action
    normalized["status"] = status_map.get(raw_status, "accepted")
    normalized["reason"] = str(payload.get("reason") or normalized["status"])
    normalized["detail"] = detail
    normalized["format"] = "agent_orchestrator.runtime_operation_receipt.v1"
    normalized["id"] = str(payload.get("id") or new_job_id().replace("job-", "receipt-"))
    normalized["records_only"] = True
    normalized["updated_at"] = timestamp
    return normalized


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

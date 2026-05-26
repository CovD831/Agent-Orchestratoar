"""Optional tmux-backed job runtime."""
from __future__ import annotations

# DEPS: __future__, agent_orchestrator, dataclasses, shutil, subprocess, typing
# RESPONSIBILITY: Provide an attachable terminal runtime without changing default execution behavior.
# MODULE: infrastructure
# ---

import shutil
import subprocess
from dataclasses import dataclass, field, replace
from typing import Protocol

from agent_orchestrator.jobs import AgentJob, FileJobRuntime, JobRequest, JobResult, TERMINAL_STATUSES, _with_operation, now_iso
from agent_orchestrator.guards import validate_runtime_start


class TmuxRunner(Protocol):
    def available(self) -> bool:
        """Return whether tmux can be used."""

    def new_session(self, session_name: str, command: str, *, cwd: str) -> None:
        """Create a detached tmux session."""

    def send_keys(self, session_name: str, message: str) -> None:
        """Send keys to a tmux session."""

    def capture_pane(self, session_name: str) -> str:
        """Capture pane output."""

    def kill_session(self, session_name: str) -> None:
        """Kill a tmux session."""


@dataclass(slots=True)
class SubprocessTmuxRunner:
    def available(self) -> bool:
        return shutil.which("tmux") is not None

    def new_session(self, session_name: str, command: str, *, cwd: str) -> None:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name, "-c", cwd, command],
            check=True,
            text=True,
            capture_output=True,
        )

    def send_keys(self, session_name: str, message: str) -> None:
        subprocess.run(["tmux", "send-keys", "-t", session_name, message, "Enter"], check=True, text=True, capture_output=True)

    def capture_pane(self, session_name: str) -> str:
        result = subprocess.run(["tmux", "capture-pane", "-p", "-t", session_name], check=True, text=True, capture_output=True)
        return result.stdout

    def kill_session(self, session_name: str) -> None:
        subprocess.run(["tmux", "kill-session", "-t", session_name], check=True, text=True, capture_output=True)


@dataclass(slots=True)
class TmuxJobRuntime(FileJobRuntime):
    runner: TmuxRunner = field(default_factory=SubprocessTmuxRunner)

    def start(self, request: JobRequest) -> AgentJob:
        validate_runtime_start(request)
        job = FileJobRuntime.start(self, request)
        session_name = _session_name(job.id)
        terminal_ref = f"tmux:{session_name}"
        metadata = {
            **job.metadata,
            "terminal_ref": terminal_ref,
            "attach_available": False,
            "tmux_session": session_name,
        }
        if not self.runner.available():
            failed = replace(
                job,
                status="failed",
                phase="failed",
                completed_at=now_iso(),
                updated_at=now_iso(),
                summary="tmux is not available.",
                error="tmux not found",
                metadata=metadata,
            )
            self._write_job(failed)
            self._append_log(job.id, "tmux not available")
            return failed

        command = _command_for_request(request)
        self.runner.new_session(session_name, command, cwd=request.cwd)
        started = replace(
            job,
            phase="working",
            command=["tmux", "new-session", "-d", "-s", session_name, command],
            metadata={**metadata, "attach_available": True},
            summary=f"{request.provider} {request.kind} job running in {terminal_ref}.",
            updated_at=now_iso(),
        )
        self._write_job(started)
        self._append_log(job.id, f"terminal_ref: {terminal_ref}")
        return started

    def status(self, job_id: str) -> AgentJob:
        job = self._read_job(job_id)
        if job.status in TERMINAL_STATUSES:
            return job
        session_name = str(job.metadata.get("tmux_session") or _session_name(job.id))
        try:
            stdout = self.runner.capture_pane(session_name)
        except Exception as exc:
            failed = replace(
                job,
                status="failed",
                phase="failed",
                completed_at=now_iso(),
                updated_at=now_iso(),
                error=str(exc),
                summary=f"tmux session {session_name} is not available.",
            )
            self._write_job(failed)
            return failed
        updated = replace(job, stdout=stdout, raw_output=stdout, updated_at=now_iso())
        self._write_job(updated)
        return updated

    def result(self, job_id: str) -> JobResult:
        return self.status(job_id).result()

    def send(self, job_id: str, message: str) -> AgentJob:
        job = self._read_job(job_id)
        if job.status in TERMINAL_STATUSES:
            return self._store_operation(
                job,
                action="send",
                status="already_terminal",
                detail="Job is already terminal.",
            )
        session_name = str(job.metadata.get("tmux_session") or _session_name(job.id))
        self.runner.send_keys(session_name, message)
        updated = replace(job, messages=[*job.messages, message], phase="working", updated_at=now_iso())
        updated = _with_operation(updated, action="send", status="accepted", detail="Message sent to tmux session.")
        self._write_job(updated)
        self._append_log(job_id, f"tmux send: {message}")
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
        session_name = str(job.metadata.get("tmux_session") or _session_name(job.id))
        try:
            self.runner.kill_session(session_name)
        except Exception as exc:
            self._append_log(job_id, f"tmux cancel error: {exc}")
        timestamp = now_iso()
        updated = replace(
            job,
            status="cancelled",
            phase="cancelled",
            completed_at=timestamp,
            updated_at=timestamp,
            summary=job.summary or "tmux job cancelled.",
        )
        updated = _with_operation(updated, action="cancel", status="accepted", detail="tmux session cancellation accepted.")
        self._write_job(updated)
        self._append_log(job_id, "tmux cancelled")
        return updated


def _session_name(job_id: str) -> str:
    return f"agent-{job_id}"


def _command_for_request(request: JobRequest) -> str:
    prompt = request.prompt.replace('"', '\\"')
    return f"printf '%s\\n' \"{prompt}\"; exec $SHELL"

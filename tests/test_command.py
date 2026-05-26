import json
import time

from agent_orchestrator.command import (
    ClaudeCodeAdapter,
    CodexCliAdapter,
    CommandJobRuntime,
    CommandResult,
    DirectApiJobRuntime,
    PromptRenderer,
    ProviderHealthCheck,
    RuntimeModeRouter,
    _MEMORY_PROVIDER_HEALTH_CACHE,
    direct_api_auth_status,
)
from agent_orchestrator.jobs import JobRequest


class FakeRunner:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.commands: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []

    def run(self, command: list[str], *, cwd: str, env: dict[str, str] | None = None) -> CommandResult:
        self.commands.append(command)
        self.envs.append(env)
        return self.result


def test_provider_health_check_uses_memory_and_disk_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("agent_orchestrator.command.shutil.which", lambda binary: binary)
    cache_path = tmp_path / "provider-health.json"
    runner = FakeRunner(CommandResult(command=["codex", "--version"], exit_code=0, stdout="codex 1.0", stderr=""))
    checker = ProviderHealthCheck(runner=runner, use_cache=True, cache_path=cache_path, ttl_seconds=60)

    live = checker.check("codex")
    memory = checker.check("codex")

    assert live.cache_tier == "live"
    assert memory.cache_tier == "memory"
    assert len(runner.commands) == 1
    assert cache_path.exists()

    _MEMORY_PROVIDER_HEALTH_CACHE.clear()
    disk_runner = FakeRunner(CommandResult(command=["codex", "--version"], exit_code=0, stdout="codex 2.0", stderr=""))
    disk_checker = ProviderHealthCheck(runner=disk_runner, use_cache=True, cache_path=cache_path, ttl_seconds=60)
    disk = disk_checker.check("codex")

    assert disk.cache_tier == "disk"
    assert disk.detail == "codex 1.0"
    assert disk_runner.commands == []


def test_provider_health_check_refresh_bypasses_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("agent_orchestrator.command.shutil.which", lambda binary: binary)
    cache_path = tmp_path / "provider-health.json"
    runner = FakeRunner(CommandResult(command=["claude", "--version"], exit_code=0, stdout="claude 1.0", stderr=""))
    checker = ProviderHealthCheck(runner=runner, use_cache=True, cache_path=cache_path, ttl_seconds=60)

    checker.check("claude")
    refreshed = checker.check("claude", refresh=True)

    assert refreshed.cache_tier == "live"
    assert len(runner.commands) == 2


def test_provider_health_check_reports_missing_binary_with_fallback(monkeypatch) -> None:
    monkeypatch.setattr("agent_orchestrator.command.shutil.which", lambda binary: None)
    runner = FakeRunner(CommandResult(command=["unused"], exit_code=0, stdout="", stderr=""))
    checker = ProviderHealthCheck(runner=runner, use_cache=False)

    codex = checker.check("codex", refresh=True)
    claude = checker.check("claude", refresh=True)
    mock = checker.check("mock", refresh=True)

    assert codex.available is False
    assert codex.detail == "codex not found"
    assert codex.recommended_fallback == "claude"
    assert claude.available is False
    assert claude.detail == "claude not found"
    assert claude.recommended_fallback == "codex"
    assert mock.available is True
    assert mock.detail == "mock provider is always available"
    assert runner.commands == []


class SlowRunner(FakeRunner):
    def run(self, command: list[str], *, cwd: str, env: dict[str, str] | None = None) -> CommandResult:
        time.sleep(0.05)
        return super().run(command, cwd=cwd, env=env)


class FakeSession:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.session_id = "session-1"
        self.thread_id = "thread-1"
        self.sent_messages: list[str] = []
        self.pid = 1234
        self.cancelled = False
        self._completed = False

    def poll(self) -> CommandResult | None:
        if self.cancelled:
            return None
        if not self._completed:
            self._completed = True
            return None
        return self.result

    def wait(self, timeout: int | None = None) -> CommandResult:
        return self.result

    def send(self, message: str) -> dict[str, object]:
        self.sent_messages.append(message)
        return {
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "message": message,
            "message_count": len(self.sent_messages),
            "status": "accepted",
        }

    def cancel(self) -> dict[str, object]:
        self.cancelled = True
        return {
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "status": "cancelled",
        }


class SessionRunner(FakeRunner):
    def __init__(self, result: CommandResult) -> None:
        super().__init__(result)
        self.session = FakeSession(result)

    def spawn(self, command: list[str], *, cwd: str, env: dict[str, str] | None = None) -> FakeSession:
        self.commands.append(command)
        self.envs.append(env)
        return self.session


class RaisingRunRunner:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.commands: list[list[str]] = []

    def run(self, command: list[str], *, cwd: str, env: dict[str, str] | None = None) -> CommandResult:
        self.commands.append(command)
        raise self.exc


class RaisingSpawnRunner(RaisingRunRunner):
    def spawn(self, command: list[str], *, cwd: str, env: dict[str, str] | None = None) -> FakeSession:
        self.commands.append(command)
        raise self.exc


class ExplodingSession(FakeSession):
    def poll(self) -> CommandResult | None:
        raise RuntimeError("poll boom")


class ExplodingSessionRunner(SessionRunner):
    def __init__(self) -> None:
        FakeRunner.__init__(self, CommandResult(command=["fake"], exit_code=0, stdout="ok", stderr=""))
        self.session = ExplodingSession(self.result)


class ExplodingAdapter(CodexCliAdapter):
    def parse_result(self, request: JobRequest, result: CommandResult) -> tuple[str, dict[str, object] | None, str | None]:
        raise RuntimeError("parse boom")


def test_command_job_runtime_completes_on_zero_exit(tmp_path) -> None:
    runner = FakeRunner(
        CommandResult(command=["fake"], exit_code=0, stdout="ok", stderr="")
    )
    runtime = CommandJobRuntime(root=tmp_path, runner=runner, adapters={"codex": CodexCliAdapter()})

    job = runtime.start(
        JobRequest(
            task_id="work-1",
            provider="codex",
            kind="implementation",
            prompt="Implement",
            cwd=str(tmp_path),
        )
    )

    assert job.status == "running"
    assert job.phase == "working"
    assert job.command[:2] == ["codex", "exec"]

    for _ in range(50):
        if runtime.status(job.id).status == "completed":
            break
        time.sleep(0.01)

    completed = runtime.status(job.id)
    assert completed.status == "completed"
    assert completed.runtime_mode == "cli_inherit"
    assert completed.metadata["runtime_mode"]["mode"] == "cli_inherit"
    assert completed.metadata["runtime_mode"]["inherits_user_config"] is True
    assert completed.exit_code == 0
    assert completed.raw_output == "ok"
    assert completed.stdout == "ok"
    assert completed.session_id is not None
    assert completed.thread_id is not None
    assert runtime.result(job.id).summary == "ok"


def test_command_job_runtime_fails_on_nonzero_exit(tmp_path) -> None:
    runner = FakeRunner(
        CommandResult(command=["fake"], exit_code=2, stdout="", stderr="bad")
    )
    runtime = CommandJobRuntime(root=tmp_path, runner=runner, adapters={"codex": CodexCliAdapter()})

    job = runtime.start(
        JobRequest(
            task_id="work-2",
            provider="codex",
            kind="implementation",
            prompt="Implement",
            cwd=str(tmp_path),
        )
    )

    for _ in range(50):
        if runtime.status(job.id).status == "failed":
            break
        time.sleep(0.01)

    failed = runtime.status(job.id)
    assert failed.status == "failed"
    assert failed.exit_code == 2
    assert failed.stderr == "bad"
    assert failed.error == "bad"


def test_command_job_runtime_fails_on_missing_command(tmp_path) -> None:
    runner = FakeRunner(
        CommandResult(command=["missing"], exit_code=None, stdout="", stderr="", error="not found")
    )
    runtime = CommandJobRuntime(root=tmp_path, runner=runner, adapters={"codex": CodexCliAdapter()})

    job = runtime.start(
        JobRequest(
            task_id="work-3",
            provider="codex",
            kind="implementation",
            prompt="Implement",
            cwd=str(tmp_path),
        )
    )

    for _ in range(50):
        if runtime.status(job.id).status == "failed":
            break
        time.sleep(0.01)

    failed = runtime.status(job.id)
    assert failed.status == "failed"
    assert failed.error == "not found"


def test_command_job_runtime_fails_when_run_raises_missing_binary(tmp_path) -> None:
    runner = RaisingRunRunner(FileNotFoundError("codex not found"))
    runtime = CommandJobRuntime(root=tmp_path, runner=runner, adapters={"codex": CodexCliAdapter()})

    job = runtime.start(
        JobRequest(
            task_id="work-12",
            provider="codex",
            kind="implementation",
            prompt="Implement",
            cwd=str(tmp_path),
        )
    )

    for _ in range(50):
        if runtime.status(job.id).status == "failed":
            break
        time.sleep(0.01)

    failed = runtime.status(job.id)
    assert failed.status == "failed"
    assert failed.stderr is not None
    assert "FileNotFoundError" in failed.error
    assert "FileNotFoundError" in failed.summary
    assert "codex not found" in failed.stderr
    assert runtime.result(job.id).status == "failed"


def test_command_job_runtime_fails_when_spawn_raises(tmp_path) -> None:
    runner = RaisingSpawnRunner(RuntimeError("spawn boom"))
    runtime = CommandJobRuntime(root=tmp_path, runner=runner, adapters={"codex": CodexCliAdapter()})

    job = runtime.start(
        JobRequest(
            task_id="work-13",
            provider="codex",
            kind="implementation",
            prompt="Implement",
            cwd=str(tmp_path),
        )
    )

    assert job.status == "failed"
    assert job.stderr is not None
    assert "Failed to spawn provider command" in job.error
    assert "spawn boom" in job.stderr


def test_command_job_runtime_fails_when_background_poll_raises(tmp_path) -> None:
    runner = ExplodingSessionRunner()
    runtime = CommandJobRuntime(root=tmp_path, runner=runner, adapters={"codex": CodexCliAdapter()})

    job = runtime.start(
        JobRequest(
            task_id="work-14",
            provider="codex",
            kind="implementation",
            prompt="Implement",
            cwd=str(tmp_path),
        )
    )

    for _ in range(50):
        if runtime.status(job.id).status == "failed":
            break
        time.sleep(0.01)

    failed = runtime.status(job.id)
    assert failed.status == "failed"
    assert failed.stderr is not None
    assert "poll boom" in failed.error
    assert "RuntimeError" in failed.stderr


def test_command_job_runtime_fails_when_finalize_raises(tmp_path) -> None:
    runner = FakeRunner(
        CommandResult(command=["fake"], exit_code=0, stdout="ok", stderr="")
    )
    runtime = CommandJobRuntime(root=tmp_path, runner=runner, adapters={"codex": ExplodingAdapter()})

    job = runtime.start(
        JobRequest(
            task_id="work-15",
            provider="codex",
            kind="implementation",
            prompt="Implement",
            cwd=str(tmp_path),
        )
    )

    for _ in range(50):
        if runtime.status(job.id).status == "failed":
            break
        time.sleep(0.01)

    failed = runtime.status(job.id)
    assert failed.status == "failed"
    assert failed.stderr is not None
    assert "Failed to finalize provider command" in failed.error
    assert "parse boom" in failed.stderr


def test_command_job_runtime_cancel_marks_terminal(tmp_path) -> None:
    runner = SlowRunner(
        CommandResult(command=["fake"], exit_code=0, stdout="ok", stderr="")
    )
    runtime = CommandJobRuntime(root=tmp_path, runner=runner, adapters={"codex": CodexCliAdapter()})

    job = runtime.start(
        JobRequest(
            task_id="work-9",
            provider="codex",
            kind="implementation",
            prompt="Implement",
            cwd=str(tmp_path),
        )
    )
    cancelled = runtime.cancel(job.id)

    assert cancelled.status == "cancelled"
    assert cancelled.parsed_payload is not None
    assert cancelled.parsed_payload["cancel"]["status"] == "accepted"
    assert cancelled.parsed_payload["operation"]["status"] == "accepted"
    assert runtime.result(job.id).status == "cancelled"


def test_command_job_runtime_send_persists_follow_up_payload(tmp_path) -> None:
    runner = SessionRunner(
        CommandResult(command=["fake"], exit_code=0, stdout="ok", stderr="")
    )
    runtime = CommandJobRuntime(root=tmp_path, runner=runner, adapters={"codex": CodexCliAdapter()})

    job = runtime.start(
        JobRequest(
            task_id="work-10",
            provider="codex",
            kind="implementation",
            prompt="Implement",
            cwd=str(tmp_path),
        )
    )
    sent = runtime.send(job.id, "follow up")

    assert sent.messages == ["follow up"]
    assert sent.parsed_payload is not None
    assert sent.parsed_payload["follow_up"]["message"] == "follow up"
    assert sent.parsed_payload["follow_up"]["status"] == "accepted"
    assert sent.parsed_payload["operation"]["status"] == "accepted"
    assert sent.session_id == "session-1"
    assert sent.thread_id == "thread-1"


def test_command_job_runtime_terminal_send_reports_already_terminal(tmp_path) -> None:
    runner = SessionRunner(CommandResult(command=["fake"], exit_code=0, stdout="ok", stderr=""))
    runtime = CommandJobRuntime(root=tmp_path, runner=runner, adapters={"codex": CodexCliAdapter()})
    job = runtime.start(
        JobRequest(
            task_id="work-terminal",
            provider="codex",
            kind="implementation",
            prompt="Implement",
            cwd=str(tmp_path),
        )
    )
    runtime.cancel(job.id)

    sent = runtime.send(job.id, "late")

    assert sent.parsed_payload is not None
    assert sent.parsed_payload["operation"]["status"] == "already_terminal"


def test_claude_adapter_parses_json_envelope() -> None:
    adapter = ClaudeCodeAdapter()
    stdout = json.dumps({"result": "done", "session_id": "session-1", "is_error": False})
    summary, payload, error = adapter.parse_result(
        JobRequest(
            task_id="work-4",
            provider="claude",
            kind="review",
            prompt="Review",
            cwd="/tmp/project",
        ),
        CommandResult(command=["claude"], exit_code=0, stdout=stdout, stderr=""),
    )

    assert summary == "done"
    assert payload["envelope"]["session_id"] == "session-1"
    assert error is None


def test_command_job_runtime_status_refreshes_from_provider_session(tmp_path) -> None:
    runner = SessionRunner(
        CommandResult(command=["fake"], exit_code=0, stdout="ok", stderr="")
    )
    runtime = CommandJobRuntime(root=tmp_path, runner=runner, adapters={"codex": CodexCliAdapter()})

    job = runtime.start(
        JobRequest(
            task_id="work-11",
            provider="codex",
            kind="implementation",
            prompt="Implement",
            cwd=str(tmp_path),
        )
    )

    refreshed = runtime.status(job.id)
    assert refreshed.pid == 1234

    for _ in range(50):
        refreshed = runtime.status(job.id)
        if refreshed.status == "completed":
            break
        time.sleep(0.01)

    assert refreshed.status == "completed"
    assert refreshed.session_id == "session-1"
    assert refreshed.thread_id == "thread-1"


def test_claude_adapter_marks_is_error_as_failure() -> None:
    adapter = ClaudeCodeAdapter()
    stdout = json.dumps({"result": "failed inside claude", "is_error": True})
    summary, payload, error = adapter.parse_result(
        JobRequest(
            task_id="work-5",
            provider="claude",
            kind="review",
            prompt="Review",
            cwd="/tmp/project",
        ),
        CommandResult(command=["claude"], exit_code=0, stdout=stdout, stderr=""),
    )

    assert summary == "failed inside claude"
    assert payload["envelope"]["is_error"] is True
    assert error == "failed inside claude"


def test_claude_adapter_detects_auth_prompt() -> None:
    adapter = ClaudeCodeAdapter()
    _, _, error = adapter.parse_result(
        JobRequest(
            task_id="work-6",
            provider="claude",
            kind="review",
            prompt="Review",
            cwd="/tmp/project",
        ),
        CommandResult(command=["claude"], exit_code=0, stdout="Please log in first", stderr=""),
    )

    assert error == "Claude Code returned an authentication prompt. Sign in to Claude Code and retry."


def test_codex_adapter_generates_command_spec() -> None:
    adapter = CodexCliAdapter()
    spec = adapter.build_command(
        JobRequest(
            task_id="work-7",
            provider="codex",
            kind="implementation",
            prompt="Implement",
            cwd="/tmp/project",
            model="gpt-test",
        )
    )

    assert spec.command[:6] == ["codex", "exec", "--model", "gpt-test", "--sandbox", "workspace-write"]


def test_command_job_runtime_records_runtime_mode_metadata(tmp_path) -> None:
    runner = SessionRunner(CommandResult(command=["fake"], exit_code=0, stdout="ok", stderr=""))
    runtime = CommandJobRuntime(root=tmp_path, runner=runner, adapters={"codex": CodexCliAdapter()})

    job = runtime.start(
        JobRequest(
            task_id="work-runtime-mode",
            provider="codex",
            kind="implementation",
            prompt="Implement",
            cwd=str(tmp_path),
            runtime_mode="cli_isolated",
        )
    )

    assert job.runtime_mode == "cli_isolated"
    assert job.metadata["runtime_mode"]["mode"] == "cli_isolated"
    assert job.metadata["runtime_mode"]["inherits_user_config"] is False
    assert job.metadata["runtime_mode"]["project_cwd"] == str(tmp_path)
    assert job.metadata["runtime_mode"]["runtime_home"].endswith(f"runtime-homes/{job.id}")
    assert runner.envs[0]["HOME"] == job.metadata["runtime_mode"]["runtime_home"]


def test_prompt_renderer_includes_required_sections() -> None:
    prompt = PromptRenderer().render(
        JobRequest(
            task_id="work-8",
            provider="codex",
            kind="review",
            prompt="Review",
            cwd="/tmp/project",
            metadata={
                "context": "Context here",
                "inputs": ["input"],
                "outputs": ["output"],
                "acceptance_criteria": ["criterion"],
            },
        )
    )

    assert "Provider Intent" in prompt
    assert "Task\nReview" in prompt
    assert "Context\nContext here" in prompt
    assert "Acceptance Criteria" in prompt
    assert "Return Format" in prompt


class FakeDirectApiClient:
    def complete(self, request: JobRequest) -> dict[str, object]:
        return {
            "summary": f"fake direct {request.provider}",
            "usage": {"input_tokens": 10, "output_tokens": 5, "source": "fake"},
        }


def test_direct_api_runtime_reports_auth_required_without_secret(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    runtime = DirectApiJobRuntime(root=tmp_path, client=FakeDirectApiClient())

    job = runtime.start(
        JobRequest(
            task_id="direct-missing-auth",
            provider="codex",
            kind="review",
            prompt="Review",
            cwd=str(tmp_path),
            runtime_mode="direct_api",
        )
    )

    assert job.status == "failed"
    assert job.error == "auth_required"
    assert job.metadata["direct_api_auth"]["status"] == "auth_required"
    assert "OPENAI_API_KEY" in json.dumps(job.to_dict())
    assert "sk-" not in json.dumps(job.to_dict())


def test_direct_api_runtime_completes_with_masked_auth_and_usage(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret-value")
    runtime = DirectApiJobRuntime(root=tmp_path, client=FakeDirectApiClient())

    job = runtime.start(
        JobRequest(
            task_id="direct-auth",
            provider="codex",
            kind="review",
            prompt="Review",
            cwd=str(tmp_path),
            runtime_mode="direct_api",
            model="gpt-test",
        )
    )

    assert job.status == "completed"
    assert job.runtime_mode == "direct_api"
    assert job.metadata["direct_api_auth"]["masked"] == "sk-...alue"
    assert job.parsed_payload["usage"]["source"] == "fake"
    assert "sk-test-secret-value" not in json.dumps(job.to_dict())


def test_direct_api_auth_status_masks_keys(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret-value")
    status = direct_api_auth_status("claude")

    assert status["available"] is True
    assert status["key_name"] == "ANTHROPIC_API_KEY"
    assert status["masked"] == "ant...alue"


def test_runtime_mode_router_sends_direct_api_jobs_to_direct_runtime(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-router-secret")
    cli_runtime = CommandJobRuntime(
        root=tmp_path / "cli",
        runner=SessionRunner(CommandResult(command=["fake"], exit_code=0, stdout="cli", stderr="")),
        adapters={"codex": CodexCliAdapter()},
    )
    direct_runtime = DirectApiJobRuntime(root=tmp_path / "direct", client=FakeDirectApiClient())
    runtime = RuntimeModeRouter(cli_runtime=cli_runtime, direct_api_runtime=direct_runtime)

    job = runtime.start(
        JobRequest(
            task_id="router-direct",
            provider="codex",
            kind="review",
            prompt="Review",
            cwd=str(tmp_path),
            runtime_mode="direct_api",
        )
    )

    assert runtime.status(job.id).status == "completed"
    assert job.id in runtime._routes  # noqa: SLF001
    assert cli_runtime.runner.commands == []

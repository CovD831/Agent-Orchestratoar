import json
from dataclasses import replace

import pytest

from agent_orchestrator.jobs import FileJobRuntime, InMemoryJobRuntime, JobRequest
from agent_orchestrator.guards import validate_artifact_write


def test_in_memory_job_runtime_lifecycle_for_running_job() -> None:
    runtime = InMemoryJobRuntime()
    job = runtime.start(
        JobRequest(
            task_id="work-1",
            provider="mock",
            kind="implementation",
            prompt="Implement the thing",
            cwd="/tmp/project",
        )
    )

    assert runtime.status(job.id).status == "running"
    assert runtime.status(job.id).phase == "starting"

    updated = runtime.send(job.id, "follow up")
    assert updated.messages == ["follow up"]
    assert updated.phase == "working"

    completed = runtime.complete(job.id, summary="done", stdout="ok")
    assert completed.status == "completed"
    assert runtime.result(job.id).summary == "done"
    assert runtime.result(job.id).stdout == "ok"
    assert runtime.send(job.id, "ignored").parsed_payload["operation"]["status"] == "already_terminal"
    assert runtime.cancel(job.id).parsed_payload["operation"]["status"] == "already_terminal"


def test_file_job_runtime_persists_and_lists_recent_jobs(tmp_path) -> None:
    runtime = FileJobRuntime(tmp_path)
    job = runtime.start(
        JobRequest(
            task_id="work-2",
            provider="codex",
            kind="review",
            prompt="Review this",
            cwd="/tmp/project",
        )
    )

    runtime.complete(job.id, summary="Review done", stdout="Completed prompt: Review this")

    assert (tmp_path / f"{job.id}.json").exists()
    assert (tmp_path / f"{job.id}.log").exists()
    assert (tmp_path / "index.json").exists()
    assert runtime.status(job.id).id == job.id
    assert runtime.result(job.id).raw_output == "Completed prompt: Review this"
    assert [recent.id for recent in runtime.list_recent()] == [job.id]

    stored = json.loads((tmp_path / f"{job.id}.json").read_text(encoding="utf-8"))
    assert stored["sandbox"] == "read-only"
    assert stored["status"] == "completed"
    assert stored["stdout"] == "Completed prompt: Review this"


def test_file_job_runtime_failed_and_cancelled_results_are_readable(tmp_path) -> None:
    runtime = FileJobRuntime(tmp_path)
    failed = runtime.start(
        JobRequest(
            task_id="work-failed",
            provider="codex",
            kind="implementation",
            prompt="Fail this",
            cwd="/tmp/project",
        )
    )
    runtime.fail(failed.id, summary="failed", error="boom", stdout="bad", stderr="trace")

    cancelled = runtime.start(
        JobRequest(
            task_id="work-cancelled",
            provider="claude",
            kind="review",
            prompt="Cancel this",
            cwd="/tmp/project",
        )
    )
    runtime.cancel(cancelled.id)

    failed_result = runtime.result(failed.id)
    cancelled_job = runtime.status(cancelled.id)
    assert failed_result.status == "failed"
    assert failed_result.error == "boom"
    assert failed_result.stderr == "trace"
    assert cancelled_job.status == "cancelled"
    assert cancelled_job.completed_at is not None


def test_result_serializes_summary_payload_and_raw_output(tmp_path) -> None:
    runtime = FileJobRuntime(tmp_path)
    job = runtime.start(
        JobRequest(
            task_id="work-serialize",
            provider="claude",
            kind="review",
            prompt="Serialize this",
            cwd="/tmp/project",
        )
    )
    runtime.complete(
        job.id,
        summary="serialized",
        stdout="stdout text",
        raw_output="raw text",
        parsed_payload={"summary": "serialized"},
    )

    payload = runtime.result(job.id).to_dict()
    assert payload["summary"] == "serialized"
    assert payload["raw_output"] == "raw text"
    assert payload["parsed_payload"] == {"summary": "serialized"}


def test_file_job_runtime_recovers_existing_job(tmp_path) -> None:
    runtime = FileJobRuntime(tmp_path)
    job = runtime.start(
        JobRequest(
            task_id="work-recover",
            provider="claude",
            kind="review",
            prompt="Recover this",
            cwd="/tmp/project",
        )
    )
    runtime.complete(job.id, summary="done", stdout="ok")

    recovered = FileJobRuntime(tmp_path).status(job.id)
    assert recovered.status == "completed"
    assert recovered.raw_output == "ok"


def test_file_job_runtime_rejects_terminal_to_running_update(tmp_path) -> None:
    runtime = FileJobRuntime(tmp_path)
    job = runtime.start(
        JobRequest(
            task_id="work-3",
            provider="codex",
            kind="implementation",
            prompt="Implement this",
            cwd="/tmp/project",
        )
    )
    completed = runtime.complete(job.id, summary="done")

    with pytest.raises(ValueError):
        runtime._write_job(replace(completed, status="running", phase="working"))  # noqa: SLF001


def test_job_request_defaults_sandbox_by_kind() -> None:
    review = JobRequest(
        task_id="work-4",
        provider="claude",
        kind="review",
        prompt="Review",
        cwd="/tmp/project",
    )
    rescue = JobRequest(
        task_id="work-5",
        provider="claude",
        kind="rescue",
        prompt="Rescue",
        cwd="/tmp/project",
        failure_reason="worker failed",
    )

    assert review.resolved_sandbox == "read-only"
    assert review.runtime_mode == "cli_inherit"
    assert rescue.resolved_sandbox == "workspace-write"


def test_job_request_and_persisted_job_record_runtime_mode(tmp_path) -> None:
    runtime = FileJobRuntime(tmp_path)
    job = runtime.start(
        JobRequest(
            task_id="work-runtime-mode",
            provider="codex",
            kind="implementation",
            prompt="Implement this",
            cwd="/tmp/project",
            runtime_mode="cli_isolated",
        )
    )

    stored = json.loads((tmp_path / f"{job.id}.json").read_text(encoding="utf-8"))
    assert job.runtime_mode == "cli_isolated"
    assert stored["runtime_mode"] == "cli_isolated"


def test_read_only_job_kinds_reject_writable_sandbox() -> None:
    for kind in ("research", "review", "adversarial_review"):
        with pytest.raises(ValueError, match="must use sandbox=read-only"):
            JobRequest(
                task_id=f"work-{kind}",
                provider="claude",
                kind=kind,  # type: ignore[arg-type]
                prompt="Read only",
                cwd="/tmp/project",
                sandbox="workspace-write",
            )


def test_role_guard_rejects_review_agent_implementation_job() -> None:
    with pytest.raises(ValueError, match="role reviewer cannot create implementation jobs"):
        JobRequest(
            task_id="work-reviewer-impl",
            provider="claude",
            kind="implementation",
            prompt="Implement this",
            cwd="/tmp/project",
            metadata={"role": "reviewer"},
        )


def test_role_guard_rejects_builder_approval_artifact_write() -> None:
    with pytest.raises(ValueError, match="role builder cannot write approved_plan artifacts"):
        validate_artifact_write("builder", "approved_plan")


def test_rescue_requires_failure_reason() -> None:
    with pytest.raises(ValueError):
        JobRequest(
            task_id="work-6",
            provider="claude",
            kind="rescue",
            prompt="Rescue without reason",
            cwd="/tmp/project",
        )


def test_delegation_guard_blocks_max_depth() -> None:
    with pytest.raises(ValueError):
        JobRequest(
            task_id="work-7",
            provider="codex",
            kind="implementation",
            prompt="Too deep",
            cwd="/tmp/project",
            max_depth=2,
            delegation_chain=[("claude", "research"), ("codex", "implementation")],
        )


def test_delegation_guard_blocks_unjustified_ping_pong() -> None:
    with pytest.raises(ValueError):
        JobRequest(
            task_id="work-8",
            provider="claude",
            kind="implementation",
            prompt="Ping pong",
            cwd="/tmp/project",
            delegation_chain=[("claude", "research"), ("codex", "implementation")],
        )

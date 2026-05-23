# DEPS: agent_orchestrator, pathlib, types
# RESPONSIBILITY: Cover planning support compliance, doc sync, and session guidance helpers.
# MODULE: tests
# ---

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent_orchestrator.jobs import FileJobRuntime, JobRequest
from agent_orchestrator.planning_support import (
    build_doc_sync_status_for_project,
    build_session_guidance,
    canonical_process_documentation_bundle,
)


def _write_module(root: Path, name: str, header: str) -> None:
    package = root / "src" / "agent_orchestrator"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text('"""Package."""\n', encoding="utf-8")
    (package / name).write_text(
        f'"""Example module."""\n\nfrom __future__ import annotations\n\n{header}\n\nVALUE = 1\n',
        encoding="utf-8",
    )


def _write_required_docs(root: Path) -> None:
    (root / "docs" / "process").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "architecture").mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("# README\n\n- agent-team-operator-runbook.md\n", encoding="utf-8")
    (root / "docs" / "process" / "长周期主执行计划.md").write_text(
        "# 长周期主执行计划\n\n- 文档同步 / compliance / hook blocking\n",
        encoding="utf-8",
    )
    (root / "docs" / "process" / "agent-orchestrator-implementation-process.md").write_text(
        "# Agent Orchestrator Product Process\n\n- hook-based compliance checks\n",
        encoding="utf-8",
    )
    (root / "docs" / "architecture" / "决策核心-执行拓扑-运行时分层说明.md").write_text(
        "# 决策核心-执行拓扑-运行时分层说明\n\n- 决策核心\n",
        encoding="utf-8",
    )
    (root / "docs" / "process" / "agent-team-operator-runbook.md").write_text(
        "# Agent Team Operator Runbook\n\n"
        "- team summary\n"
        "- team next\n"
        "- team runbook\n"
        "- team resume\n"
        "- team inspect-blockers\n"
        "- team inspect-execution\n"
        "- team retry-review\n"
        "- team retry-adversarial-review\n"
        "- team check-compliance\n"
        "- topology_reason\n"
        "- fallback_reason\n"
        "- fallback_detail\n",
        encoding="utf-8",
    )
    for _, spec in canonical_process_documentation_bundle(root).iter_specs():
        (root / spec.path).write_text(spec.render_markdown(), encoding="utf-8")


def _session(
    *,
    status: str,
    compliance: dict[str, object] | None = None,
    review_rounds: list[object] | None = None,
    jobs_root: Path | None = None,
    linked_execution_run_id: str | None = None,
    provider_recommendation: dict[str, object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id="plan-123",
        status=status,
        compliance=compliance,
        gaps=[],
        review_rounds=review_rounds or [],
        doc_sync={"jobs_root": str(jobs_root)} if jobs_root else {},
        resume=SimpleNamespace(linked_execution_run_id=linked_execution_run_id),
        structured_brief=SimpleNamespace(provider_recommendation=provider_recommendation or {}),
    )


def test_changed_files_header_check_blocks_only_changed_source_header_fields(tmp_path) -> None:
    _write_module(
        tmp_path,
        "changed.py",
        "# DEPS: todo\n# RESPONSIBILITY: Provide changed behavior.\n# MODULE: decision_core\n# ---",
    )
    _write_module(
        tmp_path,
        "unchanged.py",
        "# DEPS: __future__\n# RESPONSIBILITY: 待补充\n# MODULE: decision_core\n# ---",
    )
    _write_required_docs(tmp_path)

    status = build_doc_sync_status_for_project(
        tmp_path,
        FileJobRuntime(root=tmp_path / "jobs"),
        changed_files=["src/agent_orchestrator/changed.py"],
    )

    assert status["header_contract_violations"] == [
        "header contract violation: src/agent_orchestrator/changed.py has placeholder `DEPS` value"
    ]
    assert status["header_contract_warnings"] == [
        "header contract warning: src/agent_orchestrator/unchanged.py has placeholder `RESPONSIBILITY` value"
    ]
    assert status["changed_files"] == ["src/agent_orchestrator/changed.py"]


def test_canonical_process_docs_include_module_manifest_and_root_map_entries(tmp_path) -> None:
    _write_module(
        tmp_path,
        "alpha.py",
        "# DEPS: __future__\n# RESPONSIBILITY: Provide alpha behavior.\n# MODULE: decision_core\n# ---",
    )
    (tmp_path / "docs" / "process").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "process" / "agent-orchestrator-implementation-process.md").write_text(
        "# Process\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "process" / "agent-team-operator-runbook.md").write_text(
        "# Runbook\n",
        encoding="utf-8",
    )

    bundle = canonical_process_documentation_bundle(tmp_path)

    assert "`alpha.py`: Example module." in bundle.module_manifest.bullets
    assert "file-header contract" in bundle.module_manifest.bullets
    assert "root map" in bundle.module_manifest.bullets
    assert "`src/agent_orchestrator/`: primary Python package" in bundle.root_map.bullets
    assert (
        "`docs/process/agent-orchestrator-implementation-process.md`: implementation supervision source of truth"
        in bundle.root_map.bullets
    )
    assert "`docs/process/agent-team-operator-runbook.md`: operator workflow recovery guide" in bundle.root_map.bullets


def test_session_guidance_prioritizes_compliance_blocker_commands() -> None:
    guidance = build_session_guidance(
        _session(
            status="approved_for_execution",
            compliance={"blocking_reasons": ["module manifest is stale"], "warnings": []},
        )
    )

    assert guidance.primary_action == "inspect_compliance"
    assert guidance.resume_action == "inspect_compliance"
    assert guidance.resume_reason == "compliance_blocking"
    assert guidance.block_source == "compliance"
    assert guidance.recommended_commands == ["python -m agent_orchestrator.cli team check-compliance plan-123"]
    assert guidance.recovery_actions == ["inspect_compliance"]


def test_session_guidance_retries_failed_delegated_claude_review_job(tmp_path) -> None:
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    job = runtime.start(
        JobRequest(
            task_id="review",
            provider="claude",
            kind="review",
            prompt="review",
            cwd=str(tmp_path),
        )
    )
    runtime.fail(job.id, summary="review failed", error="claude auth failed")
    session = _session(
        status="needs_revision",
        review_rounds=[
            SimpleNamespace(round_type="review", summary=f"Delegated review via claude job {job.id}."),
        ],
        jobs_root=runtime.root,
    )

    guidance = build_session_guidance(session)

    assert guidance.primary_action == "retry_review"
    assert guidance.resume_action == "retry_review"
    assert guidance.resume_reason == "failed_review_job"
    assert guidance.block_source == "delegated_job"
    assert guidance.block_detail == "failed_review_job"
    assert guidance.recommended_commands == [
        "python -m agent_orchestrator.cli team retry-review plan-123",
        "python -m agent_orchestrator.cli team inspect-blockers plan-123",
        'python -m agent_orchestrator.cli team revise plan-123 --summary "close required gaps"',
    ]
    assert guidance.recovery_actions == ["inspect_delegated_job", "retry_review", "revise_plan"]


def test_session_guidance_retries_failed_fallback_reviewer_job(tmp_path) -> None:
    runtime = FileJobRuntime(root=tmp_path / "jobs")
    job = runtime.start(
        JobRequest(
            task_id="review",
            provider="mock",
            kind="review",
            prompt="review",
            cwd=str(tmp_path),
        )
    )
    runtime.fail(job.id, summary="review failed", error="mock reviewer failed")
    session = _session(
        status="needs_revision",
        review_rounds=[
            SimpleNamespace(round_type="review", summary=f"Delegated review via mock job {job.id}."),
        ],
        jobs_root=runtime.root,
        provider_recommendation={
            "reviewer": "mock",
            "fallback_from": "claude",
            "fallback_reason": "reviewer_unavailable",
        },
    )

    guidance = build_session_guidance(session)

    assert guidance.primary_action == "retry_review"
    assert guidance.resume_action == "retry_review"
    assert guidance.resume_reason == "failed_review_job"
    assert guidance.recommended_commands[0] == "python -m agent_orchestrator.cli team retry-review plan-123"
    assert guidance.recovery_actions == ["inspect_delegated_job", "retry_review", "revise_plan"]


def test_session_guidance_inspects_execution_after_completion() -> None:
    guidance = build_session_guidance(
        _session(status="accepted", linked_execution_run_id="run-123")
    )

    assert guidance.primary_action == "inspect_execution"
    assert guidance.resume_action == "inspect_execution"
    assert guidance.resume_reason == "execution_completed"
    assert guidance.block_source is None
    assert guidance.recommended_commands == ["python -m agent_orchestrator.cli team inspect-execution plan-123"]
    assert guidance.recovery_actions == []

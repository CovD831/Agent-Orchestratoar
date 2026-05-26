from pathlib import Path


def test_context_map_doc_exists_and_mentions_codebase_map_style_orientation() -> None:
    text = Path("docs/process/context-map.md").read_text(encoding="utf-8")

    assert "CODEBASE_MAP-style orientation" in text
    assert "root map" in text
    assert "module manifest" in text
    assert "file-header contract" in text
    assert "cli_inherit" in text
    assert "direct_api" in text


def test_long_cycle_plan_declares_auto_continue_protocol() -> None:
    text = Path("docs/process/长周期主执行计划.md").read_text(encoding="utf-8")

    assert "验证通过后自动进入下一段" in text
    assert "普通进展汇报不构成停点" in text
    assert "不再按“小计划一轮轮确认”运行" in text


def test_process_doc_declares_long_plan_driven_execution() -> None:
    text = Path("docs/process/agent-orchestrator-implementation-process.md").read_text(encoding="utf-8")

    assert "主计划驱动" in text
    assert "不再把每次实现包装成新的独立小计划" in text
    assert "验证通过后自动进入下一段" in text


def test_readme_points_to_continuous_internal_default_workflow() -> None:
    text = Path("README.md").read_text(encoding="utf-8")

    assert "internal default" in text
    assert "长周期主执行计划" in text
    assert "验证通过后自动进入下一段" in text


def test_operator_runbook_doc_covers_happy_path_and_recovery() -> None:
    text = Path("docs/process/agent-team-operator-runbook.md").read_text(encoding="utf-8")

    assert "team start" in text
    assert "team status" in text
    assert "team next" in text
    assert "team roles" in text
    assert "team inspect-knowledge" in text
    assert "approval_state" in text
    assert "required outputs" in text
    assert "team revise" in text
    assert "team approve" in text
    assert "team execute" in text
    assert "retry-review" in text
    assert "retry-adversarial-review" in text
    assert "topology_reason" in text
    assert "fallback_reason" in text
    assert "fallback_detail" in text
    assert "场景 A" in text
    assert "场景 B" in text
    assert "场景 C" in text
    assert "不要直接编辑底层 JSON" in text


def test_readme_points_to_hook_installation_workflow() -> None:
    text = Path("README.md").read_text(encoding="utf-8")

    assert "install-hooks" in text
    assert "team check-compliance" in text


def test_readme_uses_health_subcommand_example() -> None:
    text = Path("README.md").read_text(encoding="utf-8")

    assert "python -m agent_orchestrator.cli health" in text
    assert "python -m agent_orchestrator.cli --health" not in text


def test_process_doc_reflects_basic_documentation_gate_progress() -> None:
    text = Path("docs/process/agent-orchestrator-implementation-process.md").read_text(encoding="utf-8")

    assert "`in_progress - basic gate active`" in text
    assert "`in_progress - basic refresh and compliance checks active`" in text
    assert "`in_progress - changed-file scoped pre-commit gate active`" in text
    assert "team check-compliance" in text
    assert "--changed-file" in text
    assert "Missing plan/checklist/review-round persistence is now blocked" in text
    assert "visible reviewer fallback policy" in text
    assert "fallback source, reason, detail, and preferred reviewer" in text
    assert "structured topology rationale" in text
    assert "operator-runbook signal compliance" in text
    assert "Operator runbook drift for topology and provider fallback signals is now blocked" in text
    assert "Checklist ownership is now explicit on persisted plan items" in text
    assert "cli_inherit" in text
    assert "cli_isolated" in text
    assert "direct_api" in text
    assert "No hook-based compliance checks are active." not in text


def test_hook_script_exists_and_runs_compliance_gate() -> None:
    text = Path("scripts/git-hooks/pre-commit").read_text(encoding="utf-8")

    assert "team check-compliance" in text
    assert "PYTHONPATH=src" in text
    assert "root-map.md" not in text
    assert "has_compliance_input" in text
    assert "exit 0" in text
    assert "managed hook marker missing" in text


def test_hook_script_scopes_changed_file_checks_to_compliance_inputs() -> None:
    text = Path("scripts/git-hooks/pre-commit").read_text(encoding="utf-8")

    assert "case \"$file\" in" in text
    assert "src/agent_orchestrator/*.py" in text
    assert "docs/process/*.md" in text
    assert "docs/architecture/*.md" in text
    assert "README.md" in text
    assert "*)" in text
    assert "continue" in text
    assert "git diff --cached --name-only -z" in text
    assert "changed_args+=(--changed-file=\"$file\")" in text
    assert '"${changed_args[@]}"' in text

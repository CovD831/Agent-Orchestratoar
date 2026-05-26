"""Compliance and session-guidance helpers for planning workflows."""

from __future__ import annotations

# DEPS: __future__, agent_orchestrator, ast, dataclasses, json, pathlib, shlex, typing
# RESPONSIBILITY: Centralize planning compliance checks and session guidance helpers.
# MODULE: decision_core
# ---

import ast
import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_orchestrator.jobs import FileJobRuntime, JobRuntime

if TYPE_CHECKING:
    from agent_orchestrator.planning import PlanReviewRound, PlanSession


@dataclass(slots=True, frozen=True)
class ProcessDocumentSpec:
    path: str
    title: str
    bullets: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.bullets, tuple):
            object.__setattr__(self, "bullets", tuple(self.bullets))

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "title": self.title,
            "bullets": list(self.bullets),
        }

    def render_markdown(self) -> str:
        lines = [f"# {self.title}", ""]
        lines.extend(f"- {bullet}" for bullet in self.bullets)
        lines.append("")
        return "\n".join(lines)

    @classmethod
    def from_markdown(cls, path: str, text: str) -> "ProcessDocumentSpec":
        raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not raw_lines:
            raise ValueError("document is empty")
        heading = raw_lines[0]
        if not heading.startswith("# "):
            raise ValueError("document must start with a markdown heading")
        bullets: list[str] = []
        for line in raw_lines[1:]:
            if not line.startswith("- "):
                raise ValueError("document must use bullet lines for its structure")
            bullets.append(line[2:].strip())
        if not bullets:
            raise ValueError("document must define at least one bullet")
        return cls(path=path, title=heading[2:].strip(), bullets=tuple(bullets))


@dataclass(slots=True, frozen=True)
class ProcessDocumentationBundle:
    root_map: ProcessDocumentSpec
    context_map: ProcessDocumentSpec
    module_manifest: ProcessDocumentSpec
    file_header_contract: ProcessDocumentSpec
    release_readiness: ProcessDocumentSpec

    def iter_specs(self) -> list[tuple[str, ProcessDocumentSpec]]:
        return [
            ("root_map", self.root_map),
            ("context_map", self.context_map),
            ("module_manifest", self.module_manifest),
            ("file_header_contract", self.file_header_contract),
            ("release_readiness", self.release_readiness),
        ]


@dataclass(frozen=True, slots=True)
class SessionGuidance:
    session_id: str
    primary_action: str
    primary_reason: str
    resume_action: str
    resume_reason: str
    block_source: str | None
    block_detail: str | None
    recommended_commands: list[str]
    recovery_actions: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "primary_action": self.primary_action,
            "primary_reason": self.primary_reason,
            "resume_action": self.resume_action,
            "resume_reason": self.resume_reason,
            "block_source": self.block_source,
            "block_detail": self.block_detail,
            "recommended_commands": list(self.recommended_commands),
            "recovery_actions": list(self.recovery_actions),
        }


BLOCK_SOURCES = {"compliance", "delegated_job", "execution_run", "review", "awaiting_human"}
HEADER_REQUIRED_FIELDS = ("DEPS", "RESPONSIBILITY", "MODULE")
HEADER_PLACEHOLDER_VALUES = {"待补充", "待确定", "todo", "tbd", "unknown"}


def canonical_process_documentation_bundle(project_root: Path) -> ProcessDocumentationBundle:
    module_entries = _collect_module_manifest_entries(project_root)
    module_manifest_bullets: tuple[str, ...] = ("file-header contract", "root map", "context map")
    if module_entries:
        module_manifest_bullets = (*module_manifest_bullets, *module_entries)
    root_map_entries = _collect_root_map_entries(project_root)
    return ProcessDocumentationBundle(
        root_map=ProcessDocumentSpec(
            path="docs/process/root-map.md",
            title="Root Map",
            bullets=("module manifests", "file-header contract", "compliance checks", "context map", *root_map_entries),
        ),
        context_map=ProcessDocumentSpec(
            path="docs/process/context-map.md",
            title="Context Map",
            bullets=(
                "CODEBASE_MAP-style orientation for the Agent Orchestrator repository",
                "root map",
                "module manifest",
                "file-header contract",
                "compliance checks",
                "provider runtime modes: cli_inherit, cli_isolated, direct_api",
                "direct API readiness uses masked env-key reporting only",
                "README.md",
                "docs/process/agent-orchestrator-implementation-process.md",
                "docs/process/agent-team-operator-runbook.md",
                "docs/process/长周期主执行计划.md",
                "docs/process/v1x-release-readiness.md",
                "src/agent_orchestrator/",
                "tests/",
            ),
        ),
        module_manifest=ProcessDocumentSpec(
            path="docs/process/module-manifest.md",
            title="Module Manifest",
            bullets=(*module_manifest_bullets, "release readiness"),
        ),
        file_header_contract=ProcessDocumentSpec(
            path="docs/process/file-header-contract.md",
            title="File Header Contract",
            bullets=(
                "required header fields: DEPS / RESPONSIBILITY / MODULE",
                "changed-file enforcement for source modules",
                "placeholder header values are not allowed in changed files",
                "module manifest linkage",
                "context map linkage",
            ),
        ),
        release_readiness=ProcessDocumentSpec(
            path="docs/process/v1x-release-readiness.md",
            title="v1.x Release Readiness",
            bullets=(
                "version sync lives in `pyproject.toml`",
                "`team setup` reports provider health, doc sync, compliance, and release readiness",
                "evidence output is local markdown under `docs/process/`",
                "full readiness still depends on targeted tests and final compliance",
                "this repository does not promise plugin-marketplace style distribution",
            ),
        ),
    )


def build_doc_sync_status_for_project(
    project_root: Path,
    runtime: JobRuntime,
    *,
    refresh_results: list[dict[str, object]] | None = None,
    changed_files: list[str] | None = None,
) -> dict[str, object]:
    required_docs = [
        "README.md",
        "docs/process/长周期主执行计划.md",
        "docs/process/agent-orchestrator-implementation-process.md",
        "docs/architecture/决策核心-执行拓扑-运行时分层说明.md",
        "docs/process/root-map.md",
        "docs/process/context-map.md",
        "docs/process/module-manifest.md",
        "docs/process/file-header-contract.md",
        "docs/process/v1x-release-readiness.md",
    ]
    missing = [relative_path for relative_path in required_docs if not (project_root / relative_path).exists()]
    jobs_root = str(getattr(runtime, "root", "")) if hasattr(runtime, "root") else ""
    header_contract_violations = scan_source_file_headers(project_root, changed_files=changed_files)

    document_statuses: dict[str, dict[str, object]] = {}
    stale_docs: list[dict[str, object]] = []
    bundle = canonical_process_documentation_bundle(project_root)
    for name, spec in bundle.iter_specs():
        path = project_root / spec.path
        expected = spec.to_dict()
        if not path.exists():
            status = {
                "name": name,
                "path": spec.path,
                "status": "missing",
                "expected": expected,
                "actual": None,
            }
            document_statuses[name] = status
            stale_docs.append(status)
            continue
        text = path.read_text(encoding="utf-8")
        try:
            actual_spec = ProcessDocumentSpec.from_markdown(spec.path, text)
        except ValueError as exc:
            status = {
                "name": name,
                "path": spec.path,
                "status": "stale",
                "expected": expected,
                "actual": None,
                "reason": str(exc),
            }
            document_statuses[name] = status
            stale_docs.append(status)
            continue
        if actual_spec.title != spec.title or actual_spec.bullets != spec.bullets:
            status = {
                "name": name,
                "path": spec.path,
                "status": "stale",
                "expected": expected,
                "actual": actual_spec.to_dict(),
                "reason": "document content does not match canonical structure",
            }
            document_statuses[name] = status
            stale_docs.append(status)
            continue
        document_statuses[name] = {
            "name": name,
            "path": spec.path,
            "status": "passed",
            "expected": expected,
            "actual": actual_spec.to_dict(),
        }

    changed_file_doc_sync_violations = _changed_file_doc_sync_violations(
        project_root,
        changed_files=changed_files,
        document_statuses=document_statuses,
    )
    hook_marker_warnings = _managed_hook_marker_warnings(project_root, changed_files=changed_files)

    payload: dict[str, object] = {
        "project_root": str(project_root),
        "jobs_root": jobs_root,
        "required_docs_checked": len(required_docs),
        "missing_docs": missing,
        "stale_docs": stale_docs,
        "header_contract_violations": header_contract_violations,
        "header_contract_warnings": _scan_unrelated_header_warnings(project_root, changed_files=changed_files),
        "hook_marker_warnings": hook_marker_warnings,
        "changed_file_doc_sync_violations": changed_file_doc_sync_violations,
        "documents": document_statuses,
    }
    if refresh_results is not None:
        payload["refresh_results"] = refresh_results
    if changed_files is not None:
        payload["changed_files"] = list(changed_files)
    return payload


def scan_source_file_headers(project_root: Path, *, changed_files: list[str] | None = None) -> list[str]:
    source_root = project_root / "src" / "agent_orchestrator"
    if not source_root.exists():
        return []

    selected_paths: set[Path] | None = None
    if changed_files:
        selected_paths = set()
        for item in changed_files:
            changed_path = project_root / item
            if changed_path.suffix == ".py" and changed_path.parent == source_root:
                selected_paths.add(changed_path)

    violations: list[str] = []
    for path in sorted(source_root.glob("*.py")):
        if selected_paths is not None and path not in selected_paths:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            violations.append(f"header contract violation: {path.relative_to(project_root)} is empty")
            continue
        docstring_end_index = _find_module_docstring_end(lines)
        if docstring_end_index is None:
            violations.append(
                f"header contract violation: {path.relative_to(project_root)} missing module docstring header"
            )
            continue
        nonempty_after_docstring = [line.strip() for line in lines[docstring_end_index + 1 :] if line.strip()]
        if path.name == "__init__.py":
            continue
        if not nonempty_after_docstring:
            violations.append(
                f"header contract violation: {path.relative_to(project_root)} missing required module manifest linkage"
            )
            continue
        if nonempty_after_docstring[0] != "from __future__ import annotations":
            violations.append(
                f"header contract violation: {path.relative_to(project_root)} missing `from __future__ import annotations`"
            )
            continue
        if selected_paths is not None:
            header_violations = _header_contract_field_violations(
                path.relative_to(project_root),
                lines,
                docstring_end_index,
            )
            violations.extend(header_violations)
    return violations


def repair_missing_source_headers(project_root: Path, *, changed_files: list[str] | None = None) -> dict[str, object]:
    """Safely insert missing standard header blocks for selected source files."""
    source_root = project_root / "src" / "agent_orchestrator"
    if not source_root.exists():
        return {"changed_files": [], "required_actions": [], "remaining_warnings": ["source root not found"]}

    candidates = _selected_source_paths(project_root, changed_files=changed_files)
    changed: list[str] = []
    required_actions: list[str] = []
    remaining_warnings: list[str] = []
    for path in candidates:
        relative = str(path.relative_to(project_root))
        lines = path.read_text(encoding="utf-8").splitlines()
        docstring_end_index = _find_module_docstring_end(lines)
        if docstring_end_index is None:
            required_actions.append(f"{relative}: add a module docstring before header repair can run")
            continue
        if _extract_header_fields(lines, docstring_end_index):
            remaining_warnings.append(f"{relative}: existing header fields were left unchanged")
            continue
        future_index = _future_annotations_index(lines, docstring_end_index)
        if future_index is None:
            required_actions.append(f"{relative}: add `from __future__ import annotations` before header repair can run")
            continue
        module = _infer_module_bucket(path, source_root)
        deps = _infer_header_deps("\n".join(lines))
        responsibility = _infer_responsibility(path)
        insert_at = future_index + 1
        while insert_at < len(lines) and not lines[insert_at].strip():
            insert_at += 1
        new_lines = [
            *lines[:insert_at],
            f"# DEPS: {deps}",
            f"# RESPONSIBILITY: {responsibility}",
            f"# MODULE: {module}",
            "# ---",
            "",
            *lines[insert_at:],
        ]
        path.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")
        changed.append(relative)

    return {
        "changed_files": changed,
        "required_actions": required_actions,
        "remaining_warnings": remaining_warnings,
    }


def _scan_unrelated_header_warnings(project_root: Path, *, changed_files: list[str] | None = None) -> list[str]:
    if not changed_files:
        return []

    source_root = project_root / "src" / "agent_orchestrator"
    if not source_root.exists():
        return []

    selected_paths = {
        project_root / item
        for item in changed_files
        if (project_root / item).suffix == ".py" and (project_root / item).parent == source_root
    }
    warnings: list[str] = []
    for path in sorted(source_root.glob("*.py")):
        if path.name == "__init__.py" or path in selected_paths:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        docstring_end_index = _find_module_docstring_end(lines)
        if docstring_end_index is None:
            continue
        warnings.extend(
            _header_contract_placeholder_warnings(
                path.relative_to(project_root),
                lines,
                docstring_end_index,
            )
        )
    return warnings


def _selected_source_paths(project_root: Path, *, changed_files: list[str] | None = None) -> list[Path]:
    source_root = project_root / "src" / "agent_orchestrator"
    if not source_root.exists():
        return []
    if changed_files:
        paths = [
            project_root / item
            for item in changed_files
            if (project_root / item).suffix == ".py"
            and (project_root / item).parent == source_root
            and (project_root / item).name != "__init__.py"
            and (project_root / item).exists()
        ]
        return sorted(paths)
    return sorted(path for path in source_root.glob("*.py") if path.name != "__init__.py")


def _changed_file_doc_sync_violations(
    project_root: Path,
    *,
    changed_files: list[str] | None,
    document_statuses: dict[str, dict[str, object]],
) -> list[str]:
    if not changed_files:
        return []

    changed_source_files = [
        item
        for item in changed_files
        if item.startswith("src/agent_orchestrator/")
        and item.endswith(".py")
        and Path(item).name != "__init__.py"
    ]
    if not changed_source_files:
        return []

    violations: list[str] = []
    module_manifest_status = document_statuses.get("module_manifest", {})
    if module_manifest_status.get("status") != "passed":
        for path in changed_source_files:
            violations.append(
                f"changed-file doc sync violation: {path} requires docs/process/module-manifest.md to be refreshed"
            )

    root_map_status = document_statuses.get("root_map", {})
    if root_map_status.get("status") != "passed":
        for path in changed_source_files:
            violations.append(
                f"changed-file doc sync violation: {path} requires docs/process/root-map.md to be refreshed"
            )

    manifest_path = project_root / "docs" / "process" / "module-manifest.md"
    if not manifest_path.exists():
        return sorted(dict.fromkeys(violations))
    try:
        manifest_spec = ProcessDocumentSpec.from_markdown(
            "docs/process/module-manifest.md",
            manifest_path.read_text(encoding="utf-8"),
        )
    except ValueError:
        return sorted(dict.fromkeys(violations))

    documented_modules = _manifest_entry_paths(manifest_spec)
    manifest_bullets = {bullet for bullet in manifest_spec.bullets if bullet.startswith("`") and "`:" in bullet}
    for relative_path in changed_source_files:
        filename = Path(relative_path).name
        if filename not in documented_modules:
            violations.append(
                f"changed-file doc sync violation: {relative_path} is missing from docs/process/module-manifest.md"
            )
            continue
        source_path = project_root / relative_path
        expected_summary = _extract_module_summary(source_path)
        expected_bullet = f"`{filename}`: {expected_summary}"
        if expected_bullet not in manifest_bullets:
            violations.append(
                f"changed-file doc sync violation: {relative_path} summary is stale in docs/process/module-manifest.md"
            )
    return sorted(dict.fromkeys(violations))


def _managed_hook_marker_warnings(project_root: Path, *, changed_files: list[str] | None = None) -> list[str]:
    if not changed_files or not _changed_files_include_compliance_inputs(changed_files):
        return []
    if not (project_root / "docs" / "process").exists():
        return []

    marker_path = project_root / ".agent_orchestrator" / "hooks.json"
    if not marker_path.exists():
        return ["managed hook warning: install-hooks has not been run for this repository"]

    warnings: list[str] = []
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["managed hook warning: .agent_orchestrator/hooks.json is unreadable; rerun install-hooks"]

    if not isinstance(marker, dict):
        return ["managed hook warning: .agent_orchestrator/hooks.json is malformed; rerun install-hooks"]

    if marker.get("managed_hooks_enabled") is not True:
        warnings.append("managed hook warning: managed_hooks_enabled is not true; rerun install-hooks")

    installed_hooks = marker.get("installed_hooks")
    if not isinstance(installed_hooks, list) or "pre-commit" not in {str(item) for item in installed_hooks}:
        warnings.append("managed hook warning: pre-commit is missing from hook marker; rerun install-hooks")

    source_hook = project_root / "scripts" / "git-hooks" / "pre-commit"
    installed_hook = project_root / ".git" / "hooks" / "pre-commit"
    if (project_root / ".git" / "hooks").exists():
        if not installed_hook.exists():
            warnings.append("managed hook warning: installed pre-commit hook is missing; rerun install-hooks")
        elif source_hook.exists() and installed_hook.read_text(encoding="utf-8") != source_hook.read_text(encoding="utf-8"):
            warnings.append("managed hook warning: installed pre-commit hook differs from scripts/git-hooks/pre-commit; rerun install-hooks")

    return warnings


def _changed_files_include_compliance_inputs(changed_files: list[str]) -> bool:
    return any(
        item == "README.md"
        or item.startswith("docs/process/")
        or item.startswith("docs/architecture/")
        or (item.startswith("src/agent_orchestrator/") and item.endswith(".py"))
        for item in changed_files
    )


def _header_contract_field_violations(path: Path, lines: list[str], docstring_end_index: int) -> list[str]:
    fields = _extract_header_fields(lines, docstring_end_index)
    violations: list[str] = []
    for field in HEADER_REQUIRED_FIELDS:
        value = fields.get(field)
        if value is None:
            violations.append(f"header contract violation: {path} missing `{field}` field")
            continue
        if _is_placeholder_header_value(value):
            violations.append(f"header contract violation: {path} has placeholder `{field}` value")
    if not violations:
        violations.extend(_header_dependency_violations(path, lines, fields))
    return violations


def _header_contract_placeholder_warnings(path: Path, lines: list[str], docstring_end_index: int) -> list[str]:
    fields = _extract_header_fields(lines, docstring_end_index)
    warnings: list[str] = []
    for field in HEADER_REQUIRED_FIELDS:
        value = fields.get(field)
        if value is not None and _is_placeholder_header_value(value):
            warnings.append(f"header contract warning: {path} has placeholder `{field}` value")
    return warnings


def _extract_header_fields(lines: list[str], docstring_end_index: int) -> dict[str, str]:
    fields: dict[str, str] = {}
    after_docstring = lines[docstring_end_index + 1 :]
    found_future = False
    for raw_line in after_docstring:
        line = raw_line.strip()
        if not line:
            continue
        if not found_future:
            if line == "from __future__ import annotations":
                found_future = True
            continue
        if not line.startswith("# "):
            if fields:
                break
            continue
        content = line[2:]
        if content == "---":
            break
        if ":" not in content:
            continue
        key, value = content.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


def _future_annotations_index(lines: list[str], docstring_end_index: int) -> int | None:
    for index, raw_line in enumerate(lines[docstring_end_index + 1 :], start=docstring_end_index + 1):
        line = raw_line.strip()
        if not line:
            continue
        if line == "from __future__ import annotations":
            return index
        return None
    return None


def _header_dependency_violations(path: Path, lines: list[str], fields: dict[str, str]) -> list[str]:
    declared_raw = fields.get("DEPS")
    if declared_raw is None or _is_placeholder_header_value(declared_raw):
        return []

    declared = {item.strip() for item in declared_raw.split(",") if item.strip()}
    if not declared:
        return []

    imported = _module_dependency_names("\n".join(lines))
    expected = {
        name
        for name in imported
        if name in {"__future__", "agent_orchestrator"}
    }

    missing = sorted(expected - declared)
    extra = sorted(name for name in declared - imported if name in {"__future__", "agent_orchestrator"})

    violations: list[str] = []
    if missing:
        violations.append(
            f"header contract violation: {path} missing dependency declaration(s): {', '.join(missing)}"
        )
    if extra:
        violations.append(
            f"header contract violation: {path} has stale dependency declaration(s): {', '.join(extra)}"
        )
    return violations


def _module_dependency_names(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    dependencies: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root:
                    dependencies.add(root)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level and not module:
                continue
            root = module.split(".", 1)[0]
            if root:
                dependencies.add(root)
    return dependencies


def _infer_header_deps(source: str) -> str:
    imported = _module_dependency_names(source)
    deps = [name for name in ("__future__", "agent_orchestrator") if name in imported]
    return ", ".join(deps) if deps else "none"


def _infer_module_bucket(path: Path, source_root: Path) -> str:
    name = path.name
    if name in {"cli.py", "ui_server.py", "ui_service.py", "events.py", "messages.py", "memory.py"}:
        return "interface"
    if name in {"jobs.py", "command.py", "tmux_runtime.py", "run_store.py"}:
        return "infrastructure"
    if path.parent == source_root:
        return "decision_core"
    return path.parent.name or "decision_core"


def _infer_responsibility(path: Path) -> str:
    stem = path.stem.replace("_", " ")
    return f"Provide {stem} module behavior."


def _is_placeholder_header_value(value: str) -> bool:
    lowered = value.strip().lower()
    return not lowered or lowered in HEADER_PLACEHOLDER_VALUES


def build_compliance_status_for_session(
    *,
    project_root: Path,
    doc_sync: dict[str, object] | None,
    session: PlanSession | None = None,
    run_store: Any | None = None,
    plans_root: Path | str | None = None,
    changed_files: list[str] | None = None,
) -> dict[str, object]:
    changed_file_scope = list(changed_files or [])
    session_scope = session is not None
    missing_docs = list(doc_sync.get("missing_docs", [])) if isinstance(doc_sync, dict) else []
    stale_docs = list(doc_sync.get("stale_docs", [])) if isinstance(doc_sync, dict) else []
    header_contract_violations = (
        list(doc_sync.get("header_contract_violations", []))
        if isinstance(doc_sync, dict)
        else []
    )
    changed_file_doc_sync_violations = (
        list(doc_sync.get("changed_file_doc_sync_violations", []))
        if isinstance(doc_sync, dict)
        else []
    )
    header_contract_warnings = (
        list(doc_sync.get("header_contract_warnings", []))
        if isinstance(doc_sync, dict)
        else []
    )
    hook_marker_warnings = (
        list(doc_sync.get("hook_marker_warnings", []))
        if isinstance(doc_sync, dict)
        else []
    )
    blocking_reasons: list[str] = []
    warnings: list[str] = [str(item) for item in [*header_contract_warnings, *hook_marker_warnings]]
    baseline_warnings: list[str] = []
    if missing_docs and (changed_file_scope or not session_scope):
        blocking_reasons.append("missing required docs: " + ", ".join(str(item) for item in missing_docs))
    elif missing_docs:
        baseline_warnings.append("missing required docs: " + ", ".join(str(item) for item in missing_docs))
    if stale_docs and (changed_file_scope or not session_scope):
        stale_names = [str(item.get("path", item.get("name", "unknown"))) for item in stale_docs if isinstance(item, dict)]
        blocking_reasons.append("stale document structure: " + ", ".join(stale_names))
    elif stale_docs:
        stale_names = [str(item.get("path", item.get("name", "unknown"))) for item in stale_docs if isinstance(item, dict)]
        baseline_warnings.append("stale document structure: " + ", ".join(stale_names))
    if header_contract_violations:
        blocking_reasons.extend(str(item) for item in header_contract_violations)
    if changed_file_doc_sync_violations:
        blocking_reasons.extend(str(item) for item in changed_file_doc_sync_violations)

    if not missing_docs and not stale_docs:
        readme_path = project_root / "README.md"
        readme_text = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""
        long_plan_path = project_root / "docs" / "process" / "长周期主执行计划.md"
        long_plan_text = long_plan_path.read_text(encoding="utf-8") if long_plan_path.exists() else ""
        impl_process_path = project_root / "docs" / "process" / "agent-orchestrator-implementation-process.md"
        impl_process_text = impl_process_path.read_text(encoding="utf-8") if impl_process_path.exists() else ""
        runbook_path = project_root / "docs" / "process" / "agent-team-operator-runbook.md"
        runbook_text = runbook_path.read_text(encoding="utf-8") if runbook_path.exists() else ""
        required_runbook_signals = ["topology_reason", "fallback_reason", "fallback_detail"]
        required_guidance_commands = [
            "team summary",
            "team next",
            "team runbook",
            "team resume",
            "team inspect-blockers",
            "team inspect-execution",
            "team retry-review",
            "team retry-adversarial-review",
            "team check-compliance",
        ]
        root_map_path = project_root / "docs" / "process" / "root-map.md"
        root_map_text = root_map_path.read_text(encoding="utf-8") if root_map_path.exists() else ""
        manifest_path = project_root / "docs" / "process" / "module-manifest.md"
        manifest_text = manifest_path.read_text(encoding="utf-8") if manifest_path.exists() else ""
        header_contract_path = project_root / "docs" / "process" / "file-header-contract.md"
        header_contract_text = header_contract_path.read_text(encoding="utf-8") if header_contract_path.exists() else ""
        baseline_reasons: list[str] = []
        if "agent-team-operator-runbook.md" not in readme_text:
            baseline_reasons.append("README missing operator runbook link")
        if "文档同步 / compliance / hook blocking" not in long_plan_text:
            baseline_reasons.append("long-cycle plan missing happy-path compliance clause")
        if "hook-based compliance checks" not in impl_process_text:
            baseline_reasons.append("implementation process doc missing compliance hook language")
        if any(signal not in runbook_text for signal in required_runbook_signals):
            baseline_reasons.append("operator runbook missing topology/fallback signals")
        if any(command not in runbook_text for command in required_guidance_commands):
            baseline_reasons.append("operator runbook missing canonical guidance commands")
        if "module manifests" not in root_map_text:
            baseline_reasons.append("root map missing module manifest linkage")
        if "file-header contract" not in manifest_text:
            baseline_reasons.append("module manifest missing file-header contract linkage")
        else:
            try:
                manifest_spec = ProcessDocumentSpec.from_markdown("docs/process/module-manifest.md", manifest_text)
            except ValueError:
                manifest_spec = None
            if manifest_spec is not None:
                documented_modules = _manifest_entry_paths(manifest_spec)
                actual_modules = {
                    path.name
                    for path in (project_root / "src" / "agent_orchestrator").glob("*.py")
                    if path.name != "__init__.py"
                }
                if documented_modules != actual_modules:
                    baseline_reasons.append(
                        "module manifest coverage mismatch: documented modules do not match source modules"
                    )
        if "required header fields: DEPS / RESPONSIBILITY / MODULE" not in header_contract_text:
            baseline_reasons.append("file-header contract missing required header fields")

        if changed_file_scope or not session_scope:
            blocking_reasons.extend(baseline_reasons)
        else:
            baseline_warnings.extend(baseline_reasons)

        hook_marker_path = project_root / ".agent_orchestrator" / "hooks.json"
        if changed_file_scope and (project_root / "docs" / "process").exists() and not hook_marker_path.exists():
            warnings.append("managed hook warning: install-hooks has not been run for this repository")

    warnings.extend(baseline_warnings)
    workflow_doc_issues = {
        "README missing operator runbook link",
        "long-cycle plan missing happy-path compliance clause",
        "implementation process doc missing compliance hook language",
    }
    runbook_signal_issue = "operator runbook missing topology/fallback signals"
    runbook_guidance_issue = "operator runbook missing canonical guidance commands"
    all_compliance_issues = [*blocking_reasons, *warnings]

    if session is not None and session.resume.linked_execution_run_id and run_store is not None:
        try:
            payload = run_store.read(session.resume.linked_execution_run_id)
        except Exception:
            payload = {}
        metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
        linked_session_id = metadata.get("plan_session_id")
        approved_plan = metadata.get("approved_plan", {})
        if linked_session_id != session.id:
            blocking_reasons.append("run provenance mismatch: linked run session id does not match current plan session")
        if isinstance(approved_plan, dict) and approved_plan.get("session_id") != session.id:
            blocking_reasons.append("run provenance mismatch: approved plan session id does not match current plan session")

    if session is not None and plans_root is not None:
        session_dir = Path(plans_root) / session.id
        if not (session_dir / "checklist.json").exists():
            blocking_reasons.append("missing plan artifact snapshot: checklist.json")
        if not (session_dir / "verdict.json").exists():
            blocking_reasons.append("missing plan artifact snapshot: verdict.json")
        rounds_dir = session_dir / "rounds"
        expected_round_files = [f"round-{index:03d}.json" for index, _ in enumerate(session.review_rounds, start=1)]
        if not rounds_dir.exists() or any(not (rounds_dir / name).exists() for name in expected_round_files):
            blocking_reasons.append("review round snapshots are incomplete")

    checked_files = _collect_checked_files(
        project_root=project_root,
        doc_sync=doc_sync,
        session=session,
        plans_root=plans_root,
    )
    recommended_commands = _compliance_recommended_commands(
        session,
        changed_files=changed_files,
        project_root=project_root,
        warnings=warnings,
    )
    required_actions = _compliance_required_actions(blocking_reasons, warnings)
    status = "blocked" if blocking_reasons else "warning" if warnings else "passed"

    return {
        "status": status,
        "blocking": bool(blocking_reasons),
        "scope": "session" if session is not None else "project",
        "checks": [
            {
                "name": "required_docs_present",
                "status": "failed" if missing_docs else "passed",
                "details": "missing required docs" if missing_docs else "required docs present",
            },
            {
                "name": "docs_reference_current_workflow",
                "status": (
                    "failed"
                    if any(reason in blocking_reasons for reason in workflow_doc_issues)
                    else "warning"
                    if any(reason in warnings for reason in workflow_doc_issues)
                    else "passed"
                ),
                "details": "workflow docs mention operator runbook and compliance gates",
            },
            {
                "name": "operator_runbook_signals_current",
                "status": (
                    "failed"
                    if runbook_signal_issue in blocking_reasons
                    else "warning"
                    if runbook_signal_issue in warnings
                    else "passed"
                ),
                "details": "operator runbook documents topology and provider fallback signals",
            },
            {
                "name": "operator_runbook_guidance_current",
                "status": (
                    "failed"
                    if runbook_guidance_issue in blocking_reasons
                    else "warning"
                    if runbook_guidance_issue in warnings
                    else "passed"
                ),
                "details": "operator runbook documents canonical session guidance commands",
            },
            {
                "name": "execution_provenance_matches_session",
                "status": "failed" if any("run provenance mismatch" in reason for reason in blocking_reasons) else "passed",
                "details": "linked execution run matches the current plan session",
            },
            {
                "name": "source_file_headers_match_contract",
                "status": "failed" if header_contract_violations else "passed",
                "details": "python source files expose the required module header contract",
            },
            {
                "name": "changed_files_keep_process_docs_in_sync",
                "status": "failed" if changed_file_doc_sync_violations else "passed",
                "details": "changed source files do not leave module manifest or root map behind",
            },
            {
                "name": "managed_git_hooks_declared",
                "status": "warning" if hook_marker_warnings else "passed",
                "details": "managed hook marker and installed pre-commit hook are current",
            },
        ],
        "blocking_reasons": blocking_reasons,
        "warnings": warnings,
        "baseline_warnings": baseline_warnings,
        "checked_files": checked_files,
        "required_actions": required_actions,
        "recommended_commands": recommended_commands,
        "changed_files": list(changed_files or []),
    }


def _collect_checked_files(
    *,
    project_root: Path,
    doc_sync: dict[str, object] | None,
    session: PlanSession | None,
    plans_root: Path | str | None,
) -> list[str]:
    checked: list[str] = []
    if isinstance(doc_sync, dict):
        checked.extend(str(item) for item in doc_sync.get("changed_files", []) if item)
        for relative_path in doc_sync.get("missing_docs", []):
            checked.append(str(relative_path))
        for item in doc_sync.get("documents", {}).values() if isinstance(doc_sync.get("documents"), dict) else []:
            if isinstance(item, dict):
                path = item.get("path")
                if path:
                    checked.append(str(path))
        header_paths = _paths_from_messages(doc_sync.get("header_contract_violations", []))
        warning_paths = _paths_from_messages(doc_sync.get("header_contract_warnings", []))
        checked.extend(header_paths)
        checked.extend(warning_paths)
        if doc_sync.get("hook_marker_warnings"):
            checked.extend([".agent_orchestrator/hooks.json", "scripts/git-hooks/pre-commit"])
        checked.extend(_paths_from_messages(doc_sync.get("hook_marker_warnings", [])))
        checked.extend(_paths_from_messages(doc_sync.get("changed_file_doc_sync_violations", [])))
    if session is not None and plans_root is not None:
        session_dir = Path(plans_root) / session.id
        checked.extend(
            [
                str(Path(plans_root) / session.id / "checklist.json"),
                str(Path(plans_root) / session.id / "verdict.json"),
            ]
        )
    return sorted(dict.fromkeys(checked))


def _paths_from_messages(messages: list[object]) -> list[str]:
    paths: list[str] = []
    for item in messages:
        text = str(item)
        marker = ": "
        if marker not in text:
            continue
        tail = text.split(marker, 1)[1]
        path = tail.split(" ", 1)[0]
        if "/" in path and (path.endswith(".py") or path.endswith(".md")):
            paths.append(path)
    return paths


def _compliance_required_actions(blocking_reasons: list[str], warnings: list[str]) -> list[str]:
    actions: list[str] = []
    if any("missing required docs" in reason or "stale document structure" in reason for reason in blocking_reasons):
        actions.append("restore_process_docs")
    if any(
        signal in reason
        for reason in blocking_reasons
        for signal in [
            "changed-file doc sync violation",
            "module manifest coverage mismatch",
            "root map missing module manifest linkage",
            "module manifest missing file-header contract linkage",
            "file-header contract missing required header fields",
            "operator runbook missing topology/fallback signals",
            "operator runbook missing canonical guidance commands",
        ]
    ):
        actions.append("sync_process_doc_contracts")
    if any("header contract violation" in reason for reason in blocking_reasons):
        actions.append("fix_changed_file_headers")
    if any("run provenance mismatch" in reason for reason in blocking_reasons):
        actions.append("repair_execution_provenance")
    if any("missing plan artifact snapshot" in reason or "review round snapshots are incomplete" in reason for reason in blocking_reasons):
        actions.append("restore_plan_artifacts")
    if any("managed hook warning" in warning for warning in warnings):
        actions.append("install_or_repair_managed_hooks")
    if warnings:
        actions.append("clean_up_non_blocking_header_warnings")
    return actions


def _compliance_recommended_commands(
    session: PlanSession | None,
    *,
    changed_files: list[str] | None,
    project_root: Path,
    warnings: list[str],
) -> list[str]:
    changed_file_flags = " ".join(f"--changed-file={shlex.quote(path)}" for path in changed_files or [])
    suffix = f" {changed_file_flags}" if changed_file_flags else ""
    if session is None:
        commands = [f"python -m agent_orchestrator.cli team check-compliance{suffix}"]
    else:
        commands = [
            f"python -m agent_orchestrator.cli team check-compliance {session.id}{suffix}",
            "python -m agent_orchestrator.cli team status",
            f"python -m agent_orchestrator.cli team summary {session.id}",
        ]
    if any("managed hook warning" in warning for warning in warnings):
        commands.append(f"python -m agent_orchestrator.cli install-hooks --root {shlex.quote(str(project_root))}")
    return commands


def execution_block_detail(session: PlanSession) -> str | None:
    if not session.compliance or not isinstance(session.compliance, dict):
        return None
    reasons = [str(item) for item in session.compliance.get("blocking_reasons", [])]
    if any("run provenance mismatch" in reason for reason in reasons):
        return "provenance_mismatch"
    if session.resume.linked_execution_run_id and session.status == "blocked":
        return "run_blocked"
    return None


def build_session_guidance(session: PlanSession) -> SessionGuidance:
    required_open = [gap for gap in session.gaps if gap.required and gap.status != "closed"]
    compliance_blocking_reasons = _compliance_blocking_reasons(session)
    compliance_warnings = _compliance_warnings(session)
    baseline_warnings = _baseline_compliance_warnings(session)
    delegated_jobs, delegated_job_failed, delegated_job_in_progress, delegated_job_provider = _collect_delegated_jobs(session)

    primary_action = "inspect_session"
    primary_reason = "inspect the current session state before continuing"
    resume_action = "inspect_session"
    resume_reason = "manual_inspection_required"
    block_source: str | None = None
    block_detail: str | None = None
    recovery_actions: list[str] = []

    if session.status == "executing":
        primary_action = "wait_for_execution"
        primary_reason = "execution is in progress; wait for completion or inspect the linked run"
        resume_action = "wait_for_execution"
        resume_reason = "execution_in_progress"
    elif session.status == "intake_chat":
        primary_action = "mark_draft_ready"
        primary_reason = "continue chatting with the planning lead until the first draft is ready"
        resume_action = "mark_draft_ready"
        resume_reason = "draft_not_confirmed"
        recovery_actions = ["lead_chat"]
    elif session.status == "draft_ready":
        primary_action = "submit_review"
        primary_reason = "the first draft is confirmed; submit it to adversarial review"
        resume_action = "submit_review"
        resume_reason = "draft_ready_for_review"
        recovery_actions = ["lead_chat"]
    elif session.status == "adversarial_review":
        primary_action = "inspect_delegated_job"
        primary_reason = "adversarial review is in progress; inspect review jobs before intervening"
        resume_action = "inspect_delegated_job"
        resume_reason = "adversarial_review_in_progress"
        block_source = "review"
        recovery_actions = ["inspect_delegated_job"]
    elif compliance_blocking_reasons:
        block_source = "compliance"
        primary_action = "inspect_compliance"
        primary_reason = "compliance is blocking the workflow; restore required docs before approval or execution"
        resume_action = "inspect_compliance"
        resume_reason = "compliance_blocking"
        recovery_actions = ["inspect_compliance"]
    elif compliance_warnings and not baseline_warnings:
        primary_action = "inspect_compliance"
        primary_reason = "non-blocking compliance warnings exist; review them before the next changed-file update"
        resume_action = "inspect_session"
        resume_reason = "compliance_warning_only"
        recovery_actions = ["inspect_compliance"]
    elif delegated_job_failed and _delegated_failure_supports_retry(session, delegated_job_provider):
        block_source = "delegated_job"
        if _has_failed_delegated_family(delegated_jobs, {"adversarial_review", "adversarial_review_retry"}):
            block_detail = "failed_adversarial_review_job"
            primary_action = "retry_adversarial_review"
            resume_action = "retry_adversarial_review"
            resume_reason = "failed_adversarial_review_job"
            recovery_actions = ["inspect_delegated_job", "retry_adversarial_review", "revise_plan"]
        else:
            block_detail = "failed_review_job"
            primary_action = "retry_review"
            resume_action = "retry_review"
            resume_reason = "failed_review_job"
            recovery_actions = ["inspect_delegated_job", "retry_review", "revise_plan"]
        if delegated_job_provider == "claude":
            primary_reason = "delegated job failed; inspect the failed Claude job before deciding whether to revise or retry"
        else:
            primary_reason = "delegated job failed; inspect the failed job before deciding whether to revise or retry"
    elif delegated_job_failed:
        block_source = "delegated_job"
        block_detail = "failed_delegated_job"
        primary_action = "inspect_delegated_job"
        primary_reason = (
            "delegated job failed and automatic retry is not currently supported; "
            "inspect the failed job, then revise the plan or escalate manually"
        )
        resume_action = "inspect_delegated_job"
        resume_reason = "failed_delegated_job"
        recovery_actions = ["inspect_delegated_job", "revise_plan"]
    elif delegated_job_in_progress:
        block_source = "delegated_job"
        block_detail = "delegated_job_in_progress"
        primary_action = "inspect_delegated_job"
        primary_reason = "delegated review job is still in progress; inspect the job before deciding whether to wait or intervene"
        resume_action = "inspect_delegated_job"
        resume_reason = "delegated_job_in_progress"
        recovery_actions = ["inspect_delegated_job"]
    elif session.status == "needs_revision" and required_open:
        block_source = "review"
        primary_action = "revise"
        primary_reason = f"{len(required_open)} required gaps are still open; revise the plan before approval"
        resume_action = "revise"
        resume_reason = "required_gaps_open"
    elif session.status == "needs_revision":
        primary_action = "approve"
        primary_reason = "all required gaps are closed; approval is now allowed"
        resume_action = "approve"
        resume_reason = "required_gaps_closed"
    elif session.status == "awaiting_human_confirmation":
        if required_open:
            block_source = "review"
            primary_action = "revise"
            primary_reason = f"{len(required_open)} required review gap(s) need human supplement before approval"
            resume_action = "revise"
            resume_reason = "human_supplement_required"
            recovery_actions = ["lead_chat", "revise_plan"]
        else:
            primary_action = "approve"
            primary_reason = "adversarial review is complete; human approval is now required before execution"
            resume_action = "approve"
            resume_reason = "human_confirmation_required"
            recovery_actions = ["lead_chat"]
    elif session.status == "approved_for_execution":
        primary_action = "execute"
        primary_reason = "plan is approved; execution is the next valid action"
        resume_action = "execute"
        resume_reason = "approved_plan_ready"
    elif session.status in {"accepted", "needs_followup"}:
        primary_action = "inspect_execution"
        primary_reason = "execution completed; inspect the linked run and any follow-up guidance"
        resume_action = "inspect_execution"
        resume_reason = "execution_completed"
    elif session.status == "awaiting_human":
        block_source = "awaiting_human"
        primary_action = "human_decision"
        primary_reason = "escalate to human decision; human confirmation is required before the workflow can continue"
        resume_action = "human_decision"
        resume_reason = "human_confirmation_required"
        recovery_actions = ["human_decision"]
    elif session.status == "blocked":
        primary_action = "inspect_blockers"
        resume_action = "inspect_blockers"
        resume_reason = "review_blocked"
        recovery_actions = ["inspect_blockers"]
        if session.resume.linked_execution_run_id and not required_open:
            block_source = "execution_run"
            block_detail = execution_block_detail(session) or "run_blocked"
            primary_reason = (
                "execution ended in a blocked state; inspect the linked run before changing the plan "
                "or re-running execution"
            )
            recovery_actions = ["inspect_blockers", "inspect_execution"]
            if block_detail == "provenance_mismatch":
                recovery_actions.append("inspect_compliance")
        else:
            block_source = "review"
            primary_reason = "the workflow is blocked; inspect blocking review findings"

    commands = _guidance_commands(session.id, primary_action, resume_action, recovery_actions)
    return SessionGuidance(
        session_id=session.id,
        primary_action=primary_action,
        primary_reason=primary_reason,
        resume_action=resume_action,
        resume_reason=resume_reason,
        block_source=block_source,
        block_detail=block_detail,
        recommended_commands=commands,
        recovery_actions=recovery_actions,
    )


def _find_module_docstring_end(lines: list[str]) -> int | None:
    if not lines:
        return None
    first = lines[0].strip()
    if not first.startswith('"""'):
        return None
    if first.count('"""') >= 2 and first != '"""':
        return 0
    for index, line in enumerate(lines[1:], start=1):
        if '"""' in line:
            return index
    return None


def _collect_module_manifest_entries(project_root: Path) -> tuple[str, ...]:
    source_root = project_root / "src" / "agent_orchestrator"
    if not source_root.exists():
        return ()

    entries: list[str] = []
    for path in sorted(source_root.glob("*.py")):
        if path.name == "__init__.py":
            continue
        summary = _extract_module_summary(path)
        entries.append(f"`{path.name}`: {summary}")
    return tuple(entries)


def _collect_root_map_entries(project_root: Path) -> tuple[str, ...]:
    entries: list[str] = []
    package_root = project_root / "src" / "agent_orchestrator"
    if package_root.exists():
        entries.append("`src/agent_orchestrator/`: primary Python package")

    docs_root = project_root / "docs" / "process"
    if docs_root.exists():
        impl_process = docs_root / "agent-orchestrator-implementation-process.md"
        runbook = docs_root / "agent-team-operator-runbook.md"
        if impl_process.exists():
            entries.append("`docs/process/agent-orchestrator-implementation-process.md`: implementation supervision source of truth")
        if runbook.exists():
            entries.append("`docs/process/agent-team-operator-runbook.md`: operator workflow recovery guide")
    return tuple(entries)


def _extract_module_summary(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    end_index = _find_module_docstring_end(lines)
    if end_index is None:
        return "Missing module docstring."
    docstring_lines = lines[: end_index + 1]
    text = "\n".join(docstring_lines).strip()
    if text.startswith('"""'):
        text = text[3:]
    if text.endswith('"""'):
        text = text[:-3]
    cleaned = " ".join(line.strip() for line in text.splitlines()).strip()
    return cleaned or "Undocumented module."


def _manifest_entry_paths(spec: ProcessDocumentSpec) -> set[str]:
    module_paths: set[str] = set()
    for bullet in spec.bullets:
        stripped = str(bullet)
        if not stripped.startswith("`"):
            continue
        if "`:" not in stripped:
            continue
        module_paths.add(stripped.split("`:", 1)[0].strip("`"))
    return module_paths


def _guidance_commands(
    session_id: str,
    primary_action: str,
    resume_action: str,
    recovery_actions: list[str],
) -> list[str]:
    actions = [primary_action]
    if resume_action not in actions:
        actions.append(resume_action)
    for action in recovery_actions:
        if action not in actions:
            actions.append(action)
    commands: list[str] = []
    for action in actions:
        command = _resume_guidance_command(session_id, action)
        if command not in commands:
            commands.append(command)
    return commands


def _compliance_blocking_reasons(session: PlanSession) -> list[str]:
    if not isinstance(session.compliance, dict):
        return []
    return [str(item) for item in session.compliance.get("blocking_reasons", [])]


def _compliance_warnings(session: PlanSession) -> list[str]:
    if not isinstance(session.compliance, dict):
        return []
    return [str(item) for item in session.compliance.get("warnings", [])]


def _baseline_compliance_warnings(session: PlanSession) -> list[str]:
    if not isinstance(session.compliance, dict):
        return []
    return [str(item) for item in session.compliance.get("baseline_warnings", [])]


def _collect_delegated_jobs(session: PlanSession) -> tuple[list[dict[str, object]], bool, bool, str | None]:
    delegated_jobs: list[dict[str, object]] = []
    delegated_job_failed = False
    delegated_job_in_progress = False
    delegated_job_provider = None
    latest_round_by_family: dict[str, PlanReviewRound] = {}
    for round_ in session.review_rounds:
        family = _delegated_round_family(round_.round_type)
        if family:
            latest_round_by_family[family] = round_

    for round_ in session.review_rounds:
        summary = round_.summary
        if " job " not in summary:
            continue
        job_status = "completed"
        job_summary = summary
        job_error = None
        job_id = summary.split("job ")[-1].rstrip(".")
        runtime_status = _read_delegated_job_status(session, job_id)
        if runtime_status:
            job_status = runtime_status.status
            job_summary = runtime_status.summary or summary
            job_error = runtime_status.error
            provider = runtime_status.provider
            model = runtime_status.model
            metadata = runtime_status.metadata
            if runtime_status.status == "failed" and latest_round_by_family.get(_delegated_round_family(round_.round_type) or "") is round_:
                delegated_job_failed = True
            if runtime_status.status in {"pending", "running"} and latest_round_by_family.get(_delegated_round_family(round_.round_type) or "") is round_:
                delegated_job_in_progress = True
        else:
            provider = "claude" if "claude" in summary else "mock"
            model = None
            metadata = {}
        if job_status == "failed" and latest_round_by_family.get(_delegated_round_family(round_.round_type) or "") is round_ and delegated_job_provider is None:
            delegated_job_provider = provider
        delegated_jobs.append(
            {
                "round_type": round_.round_type,
                "provider": provider,
                "job_id": job_id,
                "status": job_status,
                "summary": job_summary,
                "error": job_error,
                "model": model,
                "metadata": metadata,
            }
        )
    return delegated_jobs, delegated_job_failed, delegated_job_in_progress, delegated_job_provider


def _has_failed_delegated_family(delegated_jobs: list[dict[str, object]], round_types: set[str]) -> bool:
    return any(
        str(job.get("round_type")) in round_types and str(job.get("status")) == "failed"
        for job in delegated_jobs
    )


def _delegated_failure_supports_retry(session: PlanSession, delegated_job_provider: str | None) -> bool:
    if delegated_job_provider == "claude":
        return True
    structured_brief = getattr(session, "structured_brief", None)
    recommendation = getattr(structured_brief, "provider_recommendation", {})
    if not isinstance(recommendation, dict):
        return False
    reviewer = recommendation.get("reviewer")
    adversarial_reviewer = recommendation.get("adversarial_reviewer")
    fallback_from = recommendation.get("fallback_from") or recommendation.get("adversarial_fallback_from")
    return bool(
        delegated_job_provider
        and delegated_job_provider in {reviewer, adversarial_reviewer}
        and fallback_from
        and fallback_from != delegated_job_provider
    )


def _read_delegated_job_status(session: PlanSession, job_id: str):
    jobs_root = session.doc_sync.get("jobs_root") if session.doc_sync else None
    if not jobs_root:
        return None
    try:
        runtime = FileJobRuntime(root=jobs_root)
        return runtime.status(job_id)
    except Exception:
        return None


def _delegated_round_family(round_type: str) -> str | None:
    if round_type in {"review", "review_retry"}:
        return "review"
    if round_type in {"adversarial_review", "adversarial_review_retry"}:
        return "adversarial_review"
    return None


def _resume_guidance_command(session_id: str, action: str) -> str:
    if action == "mark_draft_ready":
        return f"python -m agent_orchestrator.cli team draft-ready {session_id}"
    if action == "submit_review":
        return f"python -m agent_orchestrator.cli team submit-review {session_id}"
    if action == "lead_chat":
        return f"python -m agent_orchestrator.cli team chat {session_id} --message \"clarify the plan\""
    if action == "retry_review":
        return f"python -m agent_orchestrator.cli team retry-review {session_id}"
    if action == "retry_adversarial_review":
        return f"python -m agent_orchestrator.cli team retry-adversarial-review {session_id}"
    if action in {"revise", "revise_plan"}:
        return f"python -m agent_orchestrator.cli team revise {session_id} --summary \"close required gaps\""
    if action == "approve":
        return f"python -m agent_orchestrator.cli team approve {session_id}"
    if action == "execute":
        return f"python -m agent_orchestrator.cli team execute {session_id} --mode success_first"
    if action == "inspect_execution":
        return f"python -m agent_orchestrator.cli team inspect-execution {session_id}"
    if action == "inspect_blockers":
        return f"python -m agent_orchestrator.cli team inspect-blockers {session_id}"
    if action == "inspect_compliance":
        return f"python -m agent_orchestrator.cli team check-compliance {session_id}"
    if action == "human_decision":
        return f"python -m agent_orchestrator.cli team summary {session_id}"
    if action == "wait_for_execution":
        return f"python -m agent_orchestrator.cli team status {session_id}"
    if action == "inspect_delegated_job":
        return f"python -m agent_orchestrator.cli team inspect-blockers {session_id}"
    if action == "revise":
        return f"python -m agent_orchestrator.cli team next {session_id}"
    return f"python -m agent_orchestrator.cli team summary {session_id}"


def compliance_blocking_reasons(session: PlanSession) -> list[str]:
    return _compliance_blocking_reasons(session)


def compliance_warnings(session: PlanSession) -> list[str]:
    return _compliance_warnings(session)


def collect_delegated_jobs(session: PlanSession) -> tuple[list[dict[str, object]], bool, str | None]:
    jobs, failed, _in_progress, provider = _collect_delegated_jobs(session)
    return jobs, failed, provider


def has_failed_delegated_family(delegated_jobs: list[dict[str, object]], round_types: set[str]) -> bool:
    return _has_failed_delegated_family(delegated_jobs, round_types)


def read_delegated_job_status(session: PlanSession, job_id: str):
    return _read_delegated_job_status(session, job_id)


def delegated_round_family(round_type: str) -> str | None:
    return _delegated_round_family(round_type)


def resume_guidance_command(session_id: str, action: str) -> str:
    return _resume_guidance_command(session_id, action)


def extract_job_id(summary: str) -> str | None:
    if " job " not in summary:
        return None
    return summary.split("job ")[-1].rstrip(".")


def latest_round(rounds: list[PlanReviewRound], round_family: str) -> PlanReviewRound | None:
    latest = None
    for round_ in rounds:
        if _delegated_round_family(round_.round_type) == round_family:
            latest = round_
    return latest


def checklist_item_completed(checklist: list[Any], label: str) -> bool:
    return any(getattr(item, "label", None) == label and bool(getattr(item, "completed", False)) for item in checklist)

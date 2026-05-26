# v1.x Reference Upgrade Master Plan

## Purpose

This plan turns targeted lessons from local reference projects into staged upgrades for Agent Orchestrator.

The goal is not to become a bridge product, session manager, or plugin marketplace. The goal is to strengthen the existing product shape:

- Planning Governance Layer
- Execution Strategy Layer
- Documentation / Evidence / Console operations

All implementation work remains mapped to the repository's three Chinese implementation layers:

- `决策核心层`
- `执行拓扑层`
- `Provider / Runtime 层`

## Execution Protocol

- Each implementation stage must start by updating or appending a stage plan in this document.
- During a stage, run only targeted tests for the touched subsystem.
- When targeted tests pass, continue directly to the next stage without waiting for additional confirmation.
- Run the full test suite and full compliance gate only at the final convergence stage.
- Do not pull additional external repositories unless a stage explicitly needs source-level comparison beyond the already cloned local references.
- Preserve current public CLI behavior unless a stage explicitly documents the new interface.

## Reference Project Advantage Matrix

| Reference project | Strength to borrow | Local product landing zone | Layer mapping | Boundary |
| --- | --- | --- | --- | --- |
| `research_repos/codex-orchestrator` | Background tmux jobs, `status/capture/send/watch/attach`, durable job metadata, output capture, codebase map injection | Job runtime ergonomics, operator console job cards, terminal refs, log excerpts, context-map prompt support | 执行拓扑层 + Provider / Runtime 层 | Borrow lifecycle and observability ideas, not a full session-manager product |
| `research_repos/codex-plugin-cc` | Plugin bundle structure, install/update/uninstall scripts, semantic-version alignment, honest limitation reporting | Packaging docs, setup diagnostics, version/readiness checks, release readiness checklist | Provider / Runtime 层 | Borrow distribution discipline, not host-specific plugin assumptions |
| `research_repos/cc-plugin-codex` | Reverse companion plugin, Codex-native skill bundle, install verification, explicit unavailable hook limitation | Cross-provider readiness, setup command language, limitation sections in docs | Provider / Runtime 层 | Borrow clarity around degraded capability, not runtime-specific hooks |
| `research_repos/wanman` | Per-agent worktrees, isolated `$HOME`, JSON-RPC supervisor, long-lived matrix lifecycle | CLI runtime isolation, runtime-home metadata, future supervisor boundaries | Provider / Runtime 层 | Borrow isolation discipline, not the full matrix product |
| `research_repos/slark` | CLI bridge plus SDK/API bypass, per-project storage, workflow/task surfaces | `cli_inherit` / `cli_isolated` / `direct_api` runtime modes and role defaults | 执行拓扑层 + Provider / Runtime 层 | Borrow runtime choice, not the full app shell |
| Superpowers / AgentSys pattern | Skills, commands, role discipline, explicit agent operating rules | Role prompt discipline and process-doc compliance checks | 决策核心层 | Borrow reusable discipline, not host-specific command syntax |
| Task Master pattern | PRD-to-task breakdown, dependencies, next executable task | Checklist dependency/status/next-item visibility inside PlanSession | 决策核心层 + 执行拓扑层 | Borrow task structure, not a second task database |
| Ralph / Swarm / OpenSwarm patterns | Fresh-session loops, runtime decoupling, structured logs, unified approval signals | Fresh/resume execution guidance, cost/usage placeholders, approval/intervention observability | 执行拓扑层 + Provider / Runtime 层 | Borrow safeguards and observability, not a standalone dashboard |
| OpenAI `codex-plugin-cc` pattern represented by local plugin docs | Productized `review`, `adversarial-review`, `rescue`, `status`, `result`, `cancel`, `setup` verbs | Standard action taxonomy for team sessions, direct job commands, review/rescue gates | 决策核心层 + 执行拓扑层 | Borrow action grammar and UX, keep local governance-first semantics |
| Current Agent Orchestrator | Persisted plan sessions, adversarial gap closure, approved-plan execution contract, compliance and evidence reports | Unique product core that reference projects do not replace | 决策核心层 | Keep as differentiator and avoid reducing the project to runtime wrappers |

## Frozen Product Boundaries

In scope:

- Stronger job lifecycle observability and recovery signals.
- Standard review/rescue/setup/status/result/cancel action semantics.
- Context maps and documentation sync that help agents resume quickly.
- Packaging and setup diagnostics that honestly report unavailable providers or hooks.
- Evidence reports that show governance and execution benefits.
- Governed provider runtime modes: `cli_inherit`, `cli_isolated`, and `direct_api`.
- Masked API-key readiness for direct API paths without persisting secrets.

Out of scope:

- Replacing tmux, Codex CLI, Claude Code, or provider-native session systems.
- Becoming a standalone plugin marketplace.
- Making the UI the primary product surface; CLI remains first-class.
- Pulling broad external dependencies to imitate reference projects.
- Building a full direct-API tool loop or patch-application engine in this track.

## Staged Upgrade Track

### Stage 0: Reference Alignment And Baseline Freeze

Goal:

- Establish this matrix and freeze boundaries before feature work.
- Confirm current targeted test baseline.

Implementation changes:

- Add this plan as the reference upgrade master plan.
- Link the plan from process/roadmap docs where appropriate.
- Record that local references are sufficient unless a later phase requires deeper comparison.

Targeted test:

- `pytest tests/test_jobs.py tests/test_tmux_runtime.py tests/test_ui_service.py tests/test_ui_server.py tests/test_evidence.py tests/test_cli.py -q`

Pass criteria:

- Targeted baseline passes.
- Plan clearly maps borrowed ideas to local layers and boundaries.

### Stage 1: Job Operations And Observability

Goal:

- Borrow the useful job lifecycle ergonomics from `codex-orchestrator` without becoming a full session manager.

Implementation changes:

- Standardize job metadata for terminal refs, attach availability, last seen time, and log excerpts.
- Ensure send/cancel operations expose accepted, unsupported, missing, unavailable, and terminal outcomes.
- Surface those fields consistently in CLI and Console job cards.

Targeted test:

- `pytest tests/test_jobs.py tests/test_tmux_runtime.py tests/test_ui_service.py tests/test_ui_server.py tests/test_cli.py -q`

Pass criteria:

- Job commands and UI payloads expose stable lifecycle/operation signals.
- Mock/file defaults remain stable.

### Stage 2: Review / Rescue / Setup Action Grammar

Goal:

- Turn reference plugin verbs into a local, governance-first action grammar.

Implementation changes:

- Normalize operator-facing actions around review, adversarial review, rescue, status, result, cancel, and setup/readiness.
- Keep approved-plan and required-gap gates authoritative.
- Add setup/readiness output that reports provider limitations honestly.

Targeted test:

- `pytest tests/test_actions.py tests/test_team.py tests/test_cli.py tests/test_command.py -q`

Pass criteria:

- Action availability remains status-aware.
- Readiness/setup output distinguishes available, fallback, unsupported, and unavailable states.

### Stage 3: Context Map And Documentation Recovery

Goal:

- Borrow `CODEBASE_MAP`-style context recovery while preserving existing root-map/module-manifest/header contracts.

Implementation changes:

- Make canonical docs explain which context artifact to use for agent orientation.
- Add or refresh a concise codebase navigation artifact if existing root/module docs are insufficient.
- Keep document refresh and compliance commands as the source of truth.

Targeted test:

- `pytest tests/test_planning_support.py tests/test_docs_process.py tests/test_cli.py -q`

Pass criteria:

- Canonical docs refresh cleanly.
- Compliance can detect stale or missing context docs without noisy broad rewrites.

### Stage 4: Packaging, Setup, And Version Discipline

Goal:

- Borrow plugin-repo install/update/version discipline without turning this repository into a plugin product.

Implementation changes:

- Document local install/update/setup expectations.
- Add setup diagnostics or readiness summaries for provider/runtime dependencies if not already covered.
- Add a release-readiness checklist covering version sync, tests, evidence, and compliance.
- Keep the CLI honest about what it can install, update, and verify locally.

Targeted test:

- `pytest tests/test_cli.py tests/test_command.py tests/test_docs_process.py -q`

Pass criteria:

- Setup/readiness documentation matches CLI behavior.
- Version/release checklist is explicit and honest about limitations.
### Stage 5: Evidence And Product Explanation

Goal:

- Prove why governance-first orchestration is useful compared with fixed-template workflows.

Implementation changes:

- Strengthen evidence cases and trend reports around planning quality, rescue quality, and runtime limitations.
- Update README/runbook with the reference-informed workflow story.

Targeted test:

- `pytest tests/test_evidence.py tests/test_cli.py tests/test_docs_process.py -q`

Pass criteria:

- Evidence reports remain schema-compatible.
- Docs explain advantages and limitations without overstating provider/runtime completeness.

### Stage 6: Final Convergence Gate

Goal:

- Verify the full repository and synchronize process docs.

Final tests:

- `pytest`
- `PYTHONPATH=src python -m agent_orchestrator.cli team check-compliance`
- `git status --short`

Pass criteria:

- Full tests pass.
- Compliance passes.
- Working tree only contains intended staged upgrade changes.

## Provider Runtime Isolation Track

This track upgrades Provider / Runtime behavior while preserving the local-first CLI default.

Execution protocol:

- Each stage starts by appending or refreshing its stage note in this document.
- During a stage, run only the targeted tests listed for that stage.
- If targeted tests pass, continue into the next stage without waiting for confirmation.
- Run full `pytest`, `PYTHONPATH=src python -m agent_orchestrator.cli team check-compliance`, and `git status --short` only at final convergence.

Runtime mode contract:

- `cli_inherit`: default mode; reuse the user's local Codex / Claude Code install, auth, config, rules, and project trust behavior.
- `cli_isolated`: run CLI jobs with a repository-owned runtime home so inherited global rules and shell profile state are visible and bounded.
- `direct_api`: call provider APIs through environment-provided keys for low-side-effect planning/review/summarization roles; this does not provide a local tool loop.

### Isolation Stage 0: Baseline And Stage Plan Refresh

Goal:

- Record this runtime-mode plan and expand the reference matrix before code changes.

Targeted test:

- `pytest tests/test_docs_process.py tests/test_command.py -q`

### Isolation Stage 1: Provider Runtime Mode Contract

Goal:

- Add runtime-mode fields to agent profiles, job requests, job metadata, and readiness output while keeping default CLI behavior unchanged.

Targeted test:

- `pytest tests/test_command.py tests/test_jobs.py tests/test_cli.py -q`

### Isolation Stage 2: CLI Environment Isolation

Goal:

- Implement `cli_isolated` by creating per-job runtime homes and recording effective environment metadata.

Targeted test:

- `pytest tests/test_command.py tests/test_jobs.py tests/test_team.py -q`

### Isolation Stage 3: Direct API Runtime Foundation

Goal:

- Add a fakeable direct API runtime path with masked API-key readiness and no secret persistence.

Targeted test:

- `pytest tests/test_command.py tests/test_jobs.py tests/test_planning_support.py -q`

### Isolation Stage 4: Policy Routing And Role Defaults

Goal:

- Prefer direct API for low-side-effect governance roles and keep CLI as the default implementation/rescue worker.

Targeted test:

- `pytest tests/test_planning_support.py tests/test_team.py tests/test_cli_presenters.py tests/test_cli.py -q`

### Isolation Stage 5: Reference-Informed Workflow Upgrades

Goal:

- Add task dependency/next-item visibility, role prompt discipline, fresh/resume guidance, and approval/cost observability without adding a second product surface.

Targeted test:

- `pytest tests/test_planning_support.py tests/test_docs_process.py tests/test_evidence.py tests/test_ui_service.py -q`

### Isolation Stage 6: Documentation, Evidence, And Operator Surfaces

Goal:

- Document runtime modes and expose readiness/evidence/operator signals for CLI inheritance, CLI isolation, direct API auth, and provider fallback.

Targeted test:

- `pytest tests/test_docs_process.py tests/test_evidence.py tests/test_cli.py tests/test_cli_presenters.py -q`

## Reference-Informed Product Upgrade Track

This track turns the reference-project lessons into product capabilities while keeping Agent Orchestrator centered on planning governance, execution strategy, and provider/runtime boundaries.

Execution protocol:

- Each stage starts by refreshing this stage plan.
- During a stage, run only the targeted tests listed for that stage.
- If targeted tests pass, continue into the next stage without waiting for confirmation.
- Run full `pytest`, `PYTHONPATH=src python -m agent_orchestrator.cli team check-compliance`, and `git status --short` only at final convergence.

Reference landing zones:

- Task Master: task dependency visibility and next executable work inside PlanSession/WorkGraph, not a second task database.
- Superpowers / AgentSys: role contracts, command discipline, and compliance-visible role boundaries.
- Ralph: fresh/resume execution context policy and stop conditions around implementation/review/rescue loops.
- wanman / slark: local runtime/workspace policy, lightweight threads, and knowledge artifacts without SQLite or a supervisor.
- OpenSwarm / Claude Swarm: approval, intervention, usage/cost placeholders, and observability, not a mission-control clone.

### Reference Stage 0: Baseline Repair And Plan Refresh

Goal:

- Make tests acknowledge the current `intake_chat -> draft-ready -> submit-review -> approve -> execute` workflow.

Targeted test:

- `pytest tests/test_actions.py tests/test_cli.py tests/test_team.py tests/test_work_graph.py tests/test_evidence.py tests/test_orchestrator.py -q`

### Reference Stage 1: Task Pool And Next Executable Work

Goal:

- Add task dependency, blocked reason, validation, and next executable task surfaces through existing PlanSession/WorkGraph storage.

Targeted test:

- `pytest tests/test_work_graph.py tests/test_team.py tests/test_cli.py -q`

### Reference Stage 2: Role Contracts And Skill Discipline

Goal:

- Add role contract display and compliance checks for role discipline and command validity.

Targeted test:

- `pytest tests/test_actions.py tests/test_planning_support.py tests/test_docs_process.py tests/test_cli.py -q`

### Reference Stage 3: Fresh/Resume Execution Policy

Goal:

- Record fresh/resume execution context policy in team execution, worker/rescue jobs, runbook, and evidence.

Targeted test:

- `pytest tests/test_jobs.py tests/test_command.py tests/test_team.py tests/test_cli_presenters.py -q`

### Reference Stage 4: Knowledge Artifacts And Threaded Handoffs

Goal:

- Persist lightweight decisions, lessons, and workflow notes as JSONL artifacts and expose message thread visibility.

Targeted test:

- `pytest tests/test_messages.py tests/test_memory.py tests/test_team.py tests/test_ui_service.py -q`

### Reference Stage 5: Approval / Observability / Evidence

Goal:

- Surface unified approval state, human intervention reason, job/runtime health, and usage/cost placeholders.

Targeted test:

- `pytest tests/test_evidence.py tests/test_ui_service.py tests/test_ui_server.py tests/test_cli.py -q`

### Reference Stage 6: Documentation And Operator Workflow

Goal:

- Synchronize README, runbook, architecture docs, context map, and refresh/compliance behavior with the reference-informed capabilities.

Targeted test:

- `pytest tests/test_docs_process.py tests/test_planning_support.py tests/test_cli.py -q`

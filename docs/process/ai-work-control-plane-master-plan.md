# AI Work Control Plane Master Plan

## Purpose

This plan shifts Agent Orchestrator from an orchestration-centered product to an **AI Work Control Plane** for long-cycle local agent work.

The goal is not to discard orchestration. Explicit agent planning, review, rescue, jobs, and provider runtimes remain necessary in the short term. The shift is that orchestration becomes an execution capability under a higher control plane:

原有编排不舍弃；它在短期仍是必需执行层，长期则沉到 AI Work Control Plane 下方，成为可审计、可恢复、可替换的 runtime 能力。

短期靠显式编排解决真实工作；中期用 control plane 管住编排；长期允许编排逐步被模型内化，但状态、证据、审批、记忆和恢复仍留在系统外部。

```text
WorkspaceState -> ContextPacket -> StrategyDecision -> ExecutionTopologySnapshot -> ApprovalItem -> EvidenceBundle -> MemoryRecord
```

## Product Boundary

In scope:

- Durable workspace state over plan sessions, runs, jobs, evidence, approvals, provider health, dirty files, and memory.
- Context compression that gives models minimum sufficient context without choosing strategy.
- Deterministic strategy and topology artifacts that record why work should move next.
- Human approval items as first-class state, especially for blocked sessions, compliance gates, provider fallback, rescue, and reroute decisions.
- Evidence bundles for targeted tests, full tests, compliance, setup doctor, and evidence report status.
- Memory records with provenance, freshness, confidence, and optional external cache status.

Out of scope for this track:

- Replacing existing `team execute`, job runtime, provider adapters, or recovery gates.
- Building a human-org-chart UI around CEO / Leader / Employee roleplay.
- Building a React Flow editor before schemas stabilize.
- Making SDK-first provider runtime the default.
- Treating explore_cache as required infrastructure.

## AI-Native Role Model

Roles are artifact transformers, not human company titles:

- `state_keeper`: workspace stores -> `WorkspaceStateSnapshot`
- `context_compressor`: docs / memory / changed files -> `ContextPacket`
- `strategist`: context and state -> `StrategyDecision`
- `topology_compiler`: strategy / plan / work graph -> `ExecutionTopologySnapshot`
- `approval_gate`: blocked or risky state -> `ApprovalItem`
- `evidence_recorder`: local gates -> `EvidenceBundle`
- `memory_curator`: evidence and outcomes -> `MemoryRecord`

## Delivery Track

### Completed Baseline: Phase 0-5 Result

Result: Phase 0-5 are the current AI Work Control Plane baseline. CodeWhale/reference-informed work is closed, the product is reframed, the artifact pipeline is implemented, CLI surfaces are exposed, AI-native artifact transformer roles are registered, and final convergence gates passed for the baseline.

### Phase 0: CodeWhale Closure

Close the existing CodeWhale-inspired work already present in the dirty tree: worker handoff contract, gate evidence, task timeline, setup doctor JSON, docs context, handoff packets, and diagnostics.

Result: closed as part of the baseline; targeted CLI/team/planning/docs/evidence tests passed.

Targeted test:

- `pytest tests/test_cli.py tests/test_team.py tests/test_planning_support.py tests/test_docs_process.py tests/test_evidence.py -q`

### Phase 1: Product Reframe

Update README, roadmap, architecture docs, runbook, context map, release readiness docs, and ADRs so the canonical product story is Work Control Plane first and orchestration runtime second.

Result: completed as part of the baseline; README, roadmap, architecture/process docs, runbook, context map, release readiness, and ADR 0004 now describe AI Work Control Plane as the upper product layer.

Targeted test:

- `pytest tests/test_docs_process.py tests/test_planning_support.py -q`

### Phase 2: Artifact Pipeline

Add `control_plane.py` with workspace state, context packet, strategy decision, topology snapshot, approval queue, evidence bundle, and memory provenance builders.

Result: completed as part of the baseline; `src/agent_orchestrator/control_plane.py` builds the v1 artifacts from existing file stores.

Targeted test:

- `pytest tests/test_control_plane.py tests/test_memory.py -q`

### Phase 3: CLI Surfaces

Expose CLI-first operator commands:

- `team workspace-status`
- `team context-packet`
- `team topology inspect`
- `team approvals list`
- `team approvals resolve`
- `team evidence-gates`

Result: completed as part of the baseline; JSON outputs are parseable and pretty outputs remain human-readable.

Targeted test:

- `pytest tests/test_cli.py tests/test_cli_presenters.py -q`

### Phase 4: Role Discipline

Add AI-native artifact transformer roles to `team roles` and compliance-visible runbook requirements.

Result: completed as part of the baseline; roles are artifact transformers rather than human company titles.

Targeted test:

- `pytest tests/test_actions.py tests/test_docs_process.py tests/test_team.py -q`

### Phase 5: Final Convergence

Refresh evidence reports and run final gates:

- `pytest`
- `env PYTHONPATH=src python -m agent_orchestrator.cli team check-compliance`
- `git status --short`

Result: completed for the baseline; the next track is **Contract Hardening + Dogfood**.

## Phase 6+ Track: Contract Hardening + Dogfood

Phase 6+ turns the baseline artifact pipeline into a durable control-plane protocol:

- Artifact contracts are documented and pinned by tests.
- Workspace index records lifecycle references for recent control-plane artifacts.
- `StrategyDecision` appears in operator summary, next, and runbook surfaces.
- Approval governance uses stable reason codes.
- Evidence bundles recommend memory writes without requiring external cache.
- UI remains read-only and consumes the stable JSON schema.
- A dogfood scenario fixes the complete artifact chain as a minimum acceptance line.

The dogfood acceptance scenario is:

```text
PlanSession -> WorkspaceState -> ContextPacket -> StrategyDecision -> ExecutionTopologySnapshot -> ApprovalItem/EvidenceBundle -> MemoryRecord
```

### Phase 6+ Results

- Phase 0 Baseline Seal: completed; Phase 0-5 are documented as the current baseline.
- Phase 1 Artifact Contract Hardening: completed; artifact contracts and golden fixtures pin stable v1 fields.
- Phase 2 Artifact Lifecycle + Workspace Index: completed; workspace index records recent artifact refs while remaining backward-compatible.
- Phase 3 StrategyDecision Operator Workflow: completed; `team summary`, `team next`, and `team runbook` surface strategy decisions.
- Phase 4 Approval Governance Reason Codes: completed; approval items carry stable reason codes and hydrate legacy payloads.
- Phase 5 Evidence -> Memory Policy: completed; evidence bundles recommend durable memory writes without auto-syncing external cache.
- Phase 6 Read-Only Operator UI Surfaces: completed; UI consumes workspace, strategy, topology, approval, and evidence summaries without mutation.
- Phase 7 Dogfood Scenario: completed; tests pin the full control-plane chain as a regression baseline.
- Phase 8 Final Convergence: run full tests and compliance at track closeout.

## Continuous Hardening Track

The next track keeps the same product direction and turns the baseline into the default internal workflow. Each phase starts with a short phase plan in `docs/process/`, runs only targeted tests during implementation, and automatically advances after those tests pass. Full `pytest` and `team check-compliance` are reserved for final convergence.

## Operations Track

The next line after continuous hardening is the **AI Work Control Plane Operations Track**. It turns the artifact pipeline into the default operator work surface:

```text
PlanSession -> WorkspaceState -> ContextPacket -> StrategyDecision
  -> ExecutionTopologySnapshot -> ApprovalInbox -> RunLedger
  -> EvidenceBundle -> MemoryPromotion
```

The track is reference-informed by `docs/research/control-plane-reference-rescreen.md`: HiveWard contributes workspace/program, approval inbox, blueprint, run ledger, and runtime-boundary language; wanman contributes supervisor/store isolation boundaries; slark contributes workflow state and lessons/decisions promotion; CodeWhale contributes doctor/resume/MCP validation patterns; codex-orchestrator and plugin repos contribute job observability and review/rescue command grammar; Eigent contributes dogfood evidence and HITL/tool inventory cases.

The first implementation focus is **Workspace / Program Index v2 + Approval Inbox + Run Ledger**. Explicit orchestration remains the lower execution capability; the control plane owns the durable state, recovery, evidence, approval, memory, and runtime-health surface.

Operations Track dogfood evidence is recorded in `docs/process/ai-work-control-plane-operations-dogfood-evidence.md`. The pinned chain is `PlanSession -> Workspace / Program Index v2 -> ContextPacket -> StrategyDecision -> Topology Blueprint Snapshot -> Approval Inbox -> Run Ledger -> EvidenceBundle -> Memory Candidate`.

## Live Recovery Track

The next line after Operations Track is the **AI Work Control Plane Live Recovery Track**. It turns the operator-readable control-plane surface into an operator-recoverable surface:

`Workspace / Program Index v2 -> Run Ledger -> Recovery Timeline -> Runtime Event Stream -> Recovery Recommendation -> Operator Resume Command -> Evidence-backed Memory Promotion`.

The first implementation focus is richer live recovery telemetry. Provider/runtime bridge fidelity and broader real-task dogfood coverage are supporting gaps, but this track does not build a full provider bridge, a React Flow editor, or a direct-API patch engine.

Live Recovery dogfood evidence is recorded in `docs/process/ai-work-control-plane-live-recovery-dogfood-evidence.md`.

## Runtime Bridge Fidelity Track

The next line after Live Recovery is the **AI Work Control Plane Runtime Bridge Fidelity Track**. It turns recovery-readable runtime telemetry into session-fidelity artifacts:

`JobRecord -> ProviderSessionSnapshot -> RuntimeOperationReceipt -> RuntimeEventStream -> RecoveryRecommendation -> WorkspaceStatus / EvidenceBundle / UI`.

The implementation focus is provider/runtime fidelity for existing local command-runtime jobs: session liveness, operation receipts, attachability, continuation support, degraded reasons, and recovery-safe next commands. This track still does not build a full provider bridge, persistent session manager, React Flow editor, direct-API patch engine, or provider ping-pong loop.

Runtime Bridge dogfood evidence is recorded in `docs/process/ai-work-control-plane-runtime-bridge-dogfood-evidence.md`.

## Real-Task Dogfood Evidence Track

The next line after Runtime Bridge Fidelity is the **AI Work Control Plane Real-Task Dogfood Evidence Track**. It proves the frozen baseline against a broader local task matrix:

`RealTaskCase -> PlanSession -> Workspace / Program Index -> Recovery Recommendation -> Runtime Fidelity Summary -> EvidenceBundle -> Postmortem Signals -> Evidence Trend`.

This track keeps the product direction fixed. It expands evidence cases, report metrics, trend metrics, and postmortem/cost-latency readiness without building a full provider bridge or persistent session manager.

The committed case matrix is `docs/process/evidence-cases.json`. The generated evidence artifacts are `docs/process/v1x-evidence-report.md`, `docs/process/v1x-evidence-trend.md`, and `.agent_orchestrator/evidence/real-tasks.json`.

## Frozen Control-Plane Baseline

Current baseline: the product center has moved to **AI Work Control Plane**. Explicit `agent team` orchestration, provider runtimes, command jobs, direct API calls, and UI surfaces remain implementation capabilities underneath the control plane.

The stable operator chain is now:

```text
Workspace / Program Index v2
  -> ContextPacket
  -> StrategyDecision
  -> Topology Blueprint
  -> Approval Inbox
  -> Run Ledger
  -> Recovery Timeline
  -> Runtime Event Stream
  -> Provider Session Snapshot
  -> EvidenceBundle / Memory Candidate
```

The next product work should not be another reframe. It should be broader real-task dogfood and evidence: use this control plane on more local tasks, record where recovery/runtime fidelity actually helps, and only then deepen provider-specific bridge behavior.

Current real-task dogfood baseline: the committed evidence matrix now covers standard implementation, follow-up recovery, high-risk migration, parallel validation, UI workflow, compliance blocking, runtime fidelity, and interruption recovery. Evidence reports include recovery coverage, runtime fidelity coverage, compliance blocking coverage, postmortem readiness, and cost/latency readiness.

## Provider Runtime Bridge Evaluation Track

The next line after Runtime Measurement RC packaging is the **AI Work Control Plane Provider Runtime Bridge Evaluation Track**. It evaluates real provider runtime bridge capability before any full provider bridge implementation:

```text
Provider CLI / Runtime Evidence
  -> Capability Matrix
  -> Ownership Boundary
  -> ProviderRuntimeAdapter Contract Draft
  -> Pilot Candidate Selection
```

This track builds on the completed Runtime Bridge Fidelity and Runtime Measurement RC work. It does not claim persistent provider session ownership, token/cost measurement, provider-native send/cancel guarantees, plugin marketplace packaging, or a direct-API patch engine.

Track plan:

- `docs/process/ai-work-control-plane-provider-runtime-bridge-evaluation-plan.md`

Phase records:

- `docs/process/ai-work-control-plane-provider-runtime-bridge-evaluation-phase-0-boundary.md`
- `docs/process/ai-work-control-plane-provider-runtime-bridge-evaluation-phase-1-capability-matrix.md`
- `docs/process/ai-work-control-plane-provider-runtime-bridge-evaluation-phase-2-ownership-boundary.md`
- `docs/process/ai-work-control-plane-provider-runtime-bridge-evaluation-phase-3-adapter-contract.md`
- `docs/process/ai-work-control-plane-provider-runtime-bridge-evaluation-phase-4-pilot-selection.md`

## Codex Runtime Pilot Track

The next line after Provider Runtime Bridge Evaluation is the **AI Work Control Plane Codex Runtime Pilot**. It implements the first narrow real-provider adapter path without claiming a full provider bridge:

```text
codex exec --json
  -> JSONL / final-message capture
  -> ProviderSessionRef
  -> RuntimeMeasurement
  -> ProviderSessionSnapshot
  -> WorkspaceStatus / EvidenceBundle
```

Track plan:

- `docs/process/ai-work-control-plane-codex-runtime-pilot-plan.md`

Phase records:

- `docs/process/ai-work-control-plane-codex-runtime-pilot-phase-1-3-json-path.md`

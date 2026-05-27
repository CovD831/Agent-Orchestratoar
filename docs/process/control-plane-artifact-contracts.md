# Control Plane Artifact Contracts

## Purpose

These contracts pin the minimum stable shape for AI Work Control Plane artifacts. Producers may add fields, but they must keep the `format` value and documented stable fields compatible for v1 consumers.

## Compatibility Rules

- Every machine-facing artifact includes a top-level `format` string.
- Pretty CLI output is human-readable only; JSON output is the automation contract.
- Unknown fields must be ignored by consumers.
- Missing optional fields must hydrate to safe empty values.
- Legacy memory and approval payloads remain readable.
- External `explore_cache` status is optional state, not a failure.

## WorkspaceStateSnapshot

- Format: `agent_orchestrator.workspace_state.v1`
- Producer: `team workspace-status`
- Consumers: operator summary surfaces, UI, lifecycle index
- Lifecycle: generated from file stores and persisted to `.agent_orchestrator/workspace/index.json`
- Stable fields: `project_root`, `plans`, `runs`, `jobs`, `evidence`, `approvals`, `provider_health`, `dirty_state`, `memory_digest`, `external_cache`, `created_at`

## WorkspaceIndex

- Format: `agent_orchestrator.workspace_index.v1`
- Producer: workspace, context, topology, and evidence builders
- Consumers: operator surfaces that need the latest artifact lifecycle references
- Lifecycle: persisted to `.agent_orchestrator/workspace/index.json`
- Stable fields: `workspace_state`, `artifacts`, `updated_at`
- Rule: artifact refs contain `format`, `digest`, `created_at`, `recorded_at`, `status`, and `summary`.

## ContextPacket

- Format: `agent_orchestrator.context_packet.v1`
- Producer: `team context-packet`
- Consumers: strategy/topology surfaces and model handoff
- Lifecycle: generated on demand; records source artifacts and stale warnings
- Stable fields: `query`, `changed_files`, `docs_context`, `memory_records`, `source_artifacts`, `stale_warnings`, `token_budget_summary`, `external_cache`, `created_at`
- Rule: it compresses context but does not choose strategy.

## StrategyDecision

- Format: `agent_orchestrator.strategy_decision.v1`
- Producer: control-plane topology builder and operator workflow helpers
- Consumers: `team summary`, `team next`, `team runbook`, topology snapshot, UI
- Lifecycle: generated deterministically from session state
- Stable fields: `session_id`, `goal`, `next_goal`, `status`, `selected_topology`, `selected_provider_runtime`, `rationale`, `tradeoffs`, `risks`, `validation_plan`, `executes`, `created_at`
- Rule: `executes` must remain `false`.

## ExecutionTopologySnapshot

- Format: `agent_orchestrator.execution_topology_snapshot.v1`
- Producer: `team topology inspect`
- Consumers: operator console and UI
- Lifecycle: read-only snapshot; never mutates execution engine state
- Stable fields: `session_id`, `fixed_node_types`, `nodes`, `edges`, `strategy_decision`, `execution_contract`, `approval_queue`, `evidence_bundle`, `read_only`, `created_at`

## ApprovalItem

- Format: `agent_orchestrator.approval_item.v1`
- Producer: approval queue generator and approval resolver
- Consumers: `team approvals list`, `team approvals resolve`, evidence/memory policy
- Lifecycle: generated from blocked/risky state; resolution appends an event and memory record
- Stable fields: `id`, `status`, `reason_code`, `reason`, `scope`, `scope_id`, `recommended_action`, `session_id`, `run_id`, `job_id`, `work_unit_id`, `evidence_refs`, `created_at`, `resolved_at`, `resolution_reason`, `actor`
- Rule: resolving an approval records a decision only; execution gates remain authoritative.

## EvidenceBundle

- Format: `agent_orchestrator.evidence_bundle.v1`
- Producer: `team evidence-gates`
- Consumers: operator console, memory recommendation, release gates
- Lifecycle: generated on demand from local evidence, compliance, and gate state
- Stable fields: `status`, `gate_evidence`, `evidence_state`, `recovery_refs`, `compliance`, `memory_recommendation`, `created_at`

## RecoveryTimeline

- Format: `agent_orchestrator.recovery_timeline.v1`
- Producer: control-plane recovery builder and workspace status
- Consumers: `team summary`, `team next`, `team runbook`, workspace status, UI, evidence policy
- Lifecycle: generated read-only from PlanSession, Run Ledger, Approval Inbox, EvidenceBundle, provider fallback, and compliance state
- Stable fields: `project_root`, `status_catalog`, `entries`, `summary`, `source_refs`, `read_only`, `created_at`
- Rule: this artifact recommends recovery context only; it does not execute.

## RuntimeEventStream

- Format: `agent_orchestrator.runtime_event_stream.v1`
- Producer: control-plane runtime event builder and workspace status
- Consumers: recovery timeline, recovery recommendation, evidence policy, UI
- Lifecycle: generated read-only from existing plan, run, job, approval, and provider/runtime metadata
- Stable fields: `project_root`, `events`, `summary`, `provider_session_snapshots`, `operation_receipts`, `mutation_policy`, `usage_cost`, `read_only`, `created_at`
- Rule: direct API and runtime events remain records-only; execution stays under the approved-plan gate.

## ProviderSessionSnapshot

- Format: `agent_orchestrator.provider_session_snapshot.v1`
- Producer: `team runtime inspect` and runtime event builder
- Consumers: workspace status, evidence policy, UI, recovery recommendation
- Lifecycle: generated read-only from job records and available runtime metadata
- Stable fields: `job_id`, `task_id`, `provider`, `kind`, `status`, `phase`, `runtime_mode`, `session_id`, `thread_id`, `provider_session_ref`, `pid`, `command`, `home_isolation`, `liveness`, `operation_support`, `operation_receipts`, `last_operation_receipt`, `recommended_recovery_command`, `artifact_refs`, `read_only`, `created_at`
- Rule: a snapshot reports fidelity; it does not claim ownership of a persistent provider session.

## ProviderSessionRef

- Format: `agent_orchestrator.provider_session_ref.v1`
- Producer: provider runtime adapters when a provider-owned reference is observed
- Consumers: provider session snapshot, runtime event stream, future provider pilots
- Lifecycle: stored inside job parsed payloads and surfaced read-only through provider session snapshots
- Stable fields: `job_id`, `provider`, `runtime_id`, `session_id`, `thread_id`, `cwd`, `pid`, `command`, `provider_owned`, `continuation_guarantee`, `created_at`
- Rule: a ref points at provider/runtime state; it does not transfer ownership of that state to Agent Orchestrator.

## RuntimeOperationReceipt

- Format: `agent_orchestrator.runtime_operation_receipt.v1`
- Producer: job runtime send/cancel/terminal operation handling
- Consumers: provider session snapshot, runtime event stream, UI
- Lifecycle: appended to job parsed payloads while preserving existing `operation`, `follow_up`, and `cancel` fields
- Stable fields: `id`, `job_id`, `provider`, `runtime_mode`, `session_id`, `thread_id`, `action`, `status`, `reason`, `detail`, `terminal_state`, `records_only`, `updated_at`
- Rule: receipts are evidence of operator/runtime interaction only; they do not bypass execution gates.

## RecoveryRecommendation

- Format: `agent_orchestrator.recovery_recommendation.v1`
- Producer: `team next --format json` and workspace status
- Consumers: operator CLI, UI, recovery docs
- Lifecycle: generated read-only from session status, recovery timeline, runtime events, approvals, and evidence
- Stable fields: `session_id`, `current_status`, `current_blocking_reason`, `safest_next_operator_command`, `required_approval_or_evidence`, `recoverable_artifact_refs`, `may_resume_execution`, `human_decision_required`, `compliance_must_be_fixed_first`, `read_only`, `mutation_policy`, `created_at`
- Rule: recommendations never resolve approvals or execute recovery.

## MemoryRecord

- Format rule: memory records are line-oriented records and use stable fields instead of a top-level artifact format.
- Producer: local memory store and control-plane resolution/evidence policies
- Consumers: context packet and operator knowledge surfaces
- Lifecycle: append-only local baseline; external cache is optional
- Stable fields: `id`, `namespace`, `session_id`, `role`, `provider`, `record_type`, `summary`, `payload`, `provenance`, `freshness`, `confidence`, `external_cache_status`, `created_at`
- Rule: `provenance.source_artifacts` should identify the source evidence bundle, approval id, session id, or run id when available.

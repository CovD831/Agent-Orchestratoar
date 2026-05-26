# Agent Orchestrator Product Process

## Header
- Current Dual-Layer Stage: `Stage 2 - Planning Governance Skeleton`
- Planning Governance Progress: `decision-core-first happy path established; recovery and handoff hardening in progress`
- Execution Strategy Progress: `iteration 4 in progress`
- Total Product Progress: `decision-core-first happy path established with planning skeleton advancing`
- Current Product Gap: the repository now has a basic planning governance loop, persisted plan sessions, dual-model review rounds, decision verdicts, approved-plan-driven execution provenance, execution gating, visible reviewer fallback policy, structured topology rationale, scoped changed-file compliance hooks, operator-runbook signal compliance, a structured compliance contract with changed-file header enforcement, and explicit Provider / Runtime modes for `cli_inherit`, `cli_isolated`, and `direct_api`, but still lacks fully hardened recovery semantics, richer topology policy breadth, broader documentation coverage, stronger narrow-scope hook enforcement for the internal-default workflow, and deeper direct-API tool-loop support

## Purpose Of This Document

This is the **single source of truth** for product progress supervision.

It should answer two questions at all times:

1. `Where are we in the dual-layer plan?`
2. `Are current changes still moving the project toward the intended final product shape?`

Update this file at the end of every implementation iteration. Do not maintain a second competing progress narrative elsewhere.

## How To Use This Document

- Keep the header current after every iteration.
- Record accepted direction only; do not use this file for speculative side ideas.
- Every verified iteration must include evidence and drift notes.
- If an iteration improves only one layer while the product gap remains dominated by the other, record that drift explicitly.
- Treat providers, bridges, runtimes, and job backends as plugin boundaries unless an iteration explicitly says otherwise.

## 中文分层对齐说明

从现在开始，过程监督时默认按以下三层判断改动归属：

- `决策核心层`
- `执行拓扑层`
- `Provider / Runtime 层`

对应中文基准说明见：

- [决策核心-执行拓扑-运行时分层说明](/Users/abab/Desktop/Agent-Orchestratoar/docs/architecture/决策核心-执行拓扑-运行时分层说明.md)
- [长周期主执行计划](/Users/abab/Desktop/Agent-Orchestratoar/docs/process/长周期主执行计划.md)
- [v1.x Reference Upgrade Master Plan](/Users/abab/Desktop/Agent-Orchestratoar/docs/process/v1x-reference-upgrade-master-plan.md)

特别说明：

- `agent team` 默认视为执行拓扑层
- `claude / codex / command runtime` 默认视为 Provider / Runtime 层
- `cli_inherit / cli_isolated / direct_api` 默认视为 Provider / Runtime 层
- `PlanSession / RoundController / gap closure / approved-plan gate` 默认视为决策核心层

执行方式补充：

- 后续默认按“长周期主执行计划”持续推进
- 后续实现采用“主计划驱动”，不再把每次实现包装成新的独立小计划
- 每个实现段验证通过后自动进入下一段，普通进展汇报不构成停点
- 除非发生高风险方向变化，否则不再为每个小阶段重新起一轮大计划

## Final Product Shape

The intended v1 product is:

- a local-first CLI orchestration system
- optimized for the author's real workflows before broader packaging
- built around `Planning Governance Layer + Execution Strategy Layer`
- capable of accepting a task plus optional strategy constraints
- capable of running a rule-driven planning governance loop before execution
- capable of producing persisted plans, review history, checklists, approved execution artifacts, routed execution decisions, and synchronized documentation updates

The product is not:

- a bridge product
- a tmux/session manager
- a provider-specific orchestration shell
- a runtime-complete agent platform

## Product Layers

### Planning Governance Layer
- Goal:
  make planning a first-class, reviewable, resumable, and enforceable product workflow before code execution begins
- Completion Criteria:
  plan sessions, review rounds, adversarial review, decision verdicts, checklist persistence, and resume state exist and gate execution
- Status:
  `in_progress`

### Execution Strategy Layer
- Goal:
  convert approved plans into explainable, policy-driven execution through replaceable plugins
- Completion Criteria:
  strategy signals, decision artifacts, plugin boundaries, and guardrails remain explicit and testable
- Status:
  `in_progress`

### Documentation And Compliance Layer
- Goal:
  force documentation, file-header contracts, and code structure to remain synchronized through checks and hooks
- Completion Criteria:
  root map, module manifests, file headers, loopback checks, and hook-based blocking are all active
- Status:
  `in_progress - basic gate active`

## Product Stages

### Stage 1: Product Backbone Rewrite
- Goal:
  replace the previous strategy-only product narrative with a dual-layer system narrative
- Completion Criteria:
  README, roadmap, and process all describe the same dual-layer system with one source of truth
- Status:
  `in_progress`

### Stage 2: Planning Governance Skeleton
- Goal:
  create the minimal plan-loop system that persists plans and blocks premature execution
- Completion Criteria:
  plan artifacts, review rounds, dual-model verdicts, gap closure logic, checklists, and resume support exist
- Status:
  `in_progress`

### Stage 3: Fractal Documentation And Hard Sync
- Goal:
  make root map, module manifests, and file header contracts enforceable and automatically refreshed
- Completion Criteria:
  documentation mismatch is detectable and task completion updates the global context map
- Status:
  `in_progress - basic refresh and compliance checks active`

### Stage 4: Hook Enforcement And End-to-End Convergence
- Goal:
  connect plan governance, execution strategy, and documentation synchronization into one enforceable workflow
- Completion Criteria:
  hook checks block violations and the full task lifecycle runs through all layers coherently
- Status:
  `in_progress - changed-file scoped pre-commit gate active`

## Execution Strategy Progress Log

### Iteration 1
- Goal:
  stabilize the strategy-control contract around `mode`, `agent_enabled`, `depth`, `provider_flow`, and depth-first failure escalation
- Status:
  `verified`
- Evidence:
  strategy-control surfaces stabilized in policy, failure, orchestrator, and CLI paths
- Risks / Drift:
  historical expectations now need to account for sequential upgrades

### Iteration 2
- Goal:
  make `provider_flow` influence execution decisions without tying the strategy layer to one runtime shape
- Status:
  `verified`
- Evidence:
  provider hints and provider evidence appear in run artifacts and tests
- Risks / Drift:
  command runtime still shaped too much of the narrative

### Iteration 3
- Goal:
  prove that the strategy layer can drive mixed execution plugins while keeping runtime behavior secondary
- Status:
  `verified`
- Evidence:
  mixed provider command-backed execution works in one orchestration run
- Risks / Drift:
  this strengthened execution evidence more than product backbone clarity

### Iteration 4
- Goal:
  define and present the execution strategy decision contract
- Status:
  `verified with approved-plan handoff convergence still being hardened`
- Evidence:
  decision signals and decision artifacts now exist on runs and attempts; CLI summaries surface decision output; tests cover contract round-trip, approved-plan-linked provenance, and auto-mode signal carryover
- Risks / Drift:
  direct `run` still coexists with the `team` path, so the main remaining risk is allowing execution ergonomics to outgrow approved-plan-first governance
- Open Questions:
  how much further direct-run and team-run artifact convergence is needed before the internal-default loop feels complete

## Planning Governance Gap List

- No configurable multi-role review policy exists beyond the fixed dual-model template.
- Decision verdicts now record reviewer fallback source, reason, detail, and preferred reviewer; broader multi-provider fallback policy is still incomplete.
- Checklist ownership is now explicit on persisted plan items, but richer per-round transition policy is still incomplete.
- Recovery semantics are now session-visible through `team summary` / `team next` / `team runbook`, but deeper interruption-aware round recovery is still incomplete.

## Documentation Sync Gap List

- Basic root map, module manifest, and file-header contract documents exist under `docs/process/`.
- Basic documentation refresh exists through the team documentation sync path.
- Basic hook-based compliance checks can detect process-document drift through `team check-compliance`.
- Operator runbook drift for topology and provider fallback signals is now blocked by compliance.
- Compliance output now exposes structured `warnings`, `checked_files`, `required_actions`, and `recommended_commands`, while keeping hard header enforcement scoped to changed files.
- Richer code/header comparison coverage is still missing.
- Automatic global map refresh is now tied to key team workflow transitions, but broader task-completion refresh semantics are still incomplete.

## Hook Enforcement Gap List

- A repository-managed `pre-commit` hook source exists and runs `team check-compliance`.
- The hook passes staged compliance-relevant files through `--changed-file` so source/header checks can stay scoped while still blocking drift.
- Hook installation is still opt-in through `install-hooks`; stronger always-on enforcement is still incomplete.
- Blocking coverage exists for basic process-document drift, but comprehensive doc/code mismatch coverage is still missing.
- No blocking rule exists for stale file-header dependencies.
- Missing plan/checklist/review-round persistence is now blocked for session-aware compliance checks.

## Risk Register

- Planning governance may sprawl unless round rules and exit conditions stay explicit.
- Existing execution work may overfit raw requirements before approved-plan inputs exist.
- Documentation enforcement may become noisy if it is introduced before formats are stable.
- Hook checks may be too brittle if rolled out before false-positive handling is understood.

## Technical Debt Register

- Direct `run` entrypoints still coexist with the decision-core-first `team` happy path, but they now persist an approved-plan-style execution contract for convergence.
- Product docs have now been upgraded to dual-layer language; topology/provider policy is still partly heuristic.
- Decision artifacts exist, and team/direct-run execution contracts now share a core schema, but broader interoperability cleanup is still needed.
- Roadmap/process/README alignment must continue to describe the current state as hardening and scope control, not as missing foundational planning-governance primitives.

## Keep / Adjust Signals

### Signals That Support Continuing
- persisted plan sessions reduce ambiguity before execution begins
- adversarial review meaningfully improves execution plans
- execution strategy remains explainable when driven by approved plan artifacts
- doc/code sync checks catch real drift without overwhelming noise

### Signals That Support Further Shrinkage
- most product value still comes from runtime-specific tricks rather than planning governance or strategy
- plan review loops become too expensive relative to their benefit
- documentation enforcement becomes mostly performative and not operationally useful
- hooks create more friction than trust

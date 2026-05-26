# HiveWard / codewale 调研笔记

调研日期：2026-05-26  
本地位置：`/Users/abab/Desktop/Agent-Orchestratoar/research_repos/`

## 拉取结果

| 项目 | GitHub | 本地目录 | 当前结论 |
| --- | --- | --- | --- |
| HiveWard | `Chaunyzhang/HiveWard` | `research_repos/HiveWard` | 高相关；它把 Claude Code / Codex / OpenClaw 放在“业务蓝图 + 公司组织 + 审批运行”的上层控制面里，和本仓库的长周期、多 agent、可审计执行方向高度重合。 |
| codewale 候选 1 | `singhk1988/codewale` | `research_repos/codewale` | 空仓库，无可分析代码。 |
| codewale 候选 2 | `jeelptl2005/codewale` | `research_repos/codewale-jeelptl2005` | Flask 编程学习网站，包含登录、课程、测试、证书、资料上传等；和 Agent Orchestrator 的方向基本不相关。 |

## HiveWard 一句话判断

HiveWard 更像“Agent Company 的可视化控制台”：先创建公司，再有 CEO / Leader 组织层，Leader 绑定蓝图，蓝图节点绑定 OpenClaw / Codex / Claude 等 harness，运行时通过节点状态、上游输入、Manager 决策、人工审批、历史记录来推进。

这和本仓库不是完全同一个切面：

- 本仓库当前核心优势在 **决策核心层**：计划治理、双模型审查、执行合约、策略解释、合规门禁。
- HiveWard 当前核心优势在 **执行拓扑 / 产品控制台层**：公司工作区、角色组织、蓝图画布、运行监控、审批入口、多 harness adapter。

因此最值得借鉴的不是直接迁移它的实现，而是把它作为本仓库未来 UI / 拓扑层 / 多 provider 编排层的一个参照物。

## HiveWard 架构要点

### 1. 工作区产品模型非常清晰

它不是从“开一次 chat”开始，而是从 Company 开始：

```text
Company
  -> CEO
    -> Leader(s)
      -> Blueprint(s)
        -> Harness Agent nodes
          -> Run / Approval / History
```

对应代码：

- `packages/shared/src/company.ts`：CompanyProfile、CompanyOverview、默认 company。
- `packages/shared/src/roles.ts`：CEO / Leader、role capabilities、driver binding、architecture view。
- `apps/api/src/store/fileHivewardStore.ts`：文件式 store index，把 companies、blueprints、runs、dashboards、roles、inbox、chat sessions 放进同一工作区索引。

可借鉴点：本仓库现在已有 `PlanSession` / team workflow / execution artifacts，但“项目现场”的产品对象仍偏 CLI 和 session。可以考虑引入一个比 session 更高层的 **Workspace / Program / Company** 概念，承载长期目标、角色、计划组、运行历史、审批队列和指标。

### 2. Blueprint schema 覆盖了执行拓扑的核心节点

`packages/shared/src/blueprint.ts` 里定义了：

- `agent`
- `manager`
- `manager_slot`
- `loop`
- `condition`
- `summary`
- `note`
- `group`

其中 agent 节点直接有：runtime、prompt、skillIds、modelId、permissionProfile、workingDirectory、timeoutMs、outputSchema、approval、send、tools。

可借鉴点：本仓库已有 ExecutionContract / work units / JobRuntime，但还没有一个稳定的“可视化/可导入导出的拓扑 DSL”。HiveWard 的节点类型可以变成我们的一个参考：

- 短期：给 `ExecutionContract` 增加可选 `topology_nodes` / `topology_edges` 只读快照，先不做画布。
- 中期：把 work units 映射成 `agent / review / rescue / summary / condition` 节点。
- 长期：允许用户保存“执行模板”，类似 HiveWard 的 blueprint package。

### 3. Manager + Slot 模型值得重点吸收

HiveWard 的 Manager 节点可以通过端口/slot 调度下游执行线：

- `manager.portCount`
- `manager.maxHandoffs`
- `manager_slot.executionMode`
- `manager_slot.parallelLaneCount`
- manager 可以是规则驱动，也可以是 agent-driven，通过 JSON 决策 `status / nextSlot / reason` 选择下一个 slot。

这和本仓库的 `topology_reason`、work-unit decomposition、review/rescue/reroute 很贴。建议借鉴为本仓库的 **执行拓扑层语义**：

```text
PlanSession approved
  -> ExecutionContract
    -> Manager policy node
      -> Slot: implementation lane(s)
      -> Slot: review lane
      -> Slot: rescue lane
      -> Slot: summary/evidence lane
```

好处：把“为什么并行 / 为什么审查 / 为什么救援 / 为什么停止”从散落的 policy 字段，进一步升级成可解释的拓扑轨迹。

### 4. 人工审批被做成运行时状态，而不是外部备注

HiveWard 的 run status 里有 `waiting_approval`。Agent 节点可配置 `approval.enabled`，输出进入审批队列；用户可以 approve / reject / reply / select approval reply，审批后继续调度。

可借鉴点：本仓库当前有 plan approve、review verdict、compliance gate，但 execution 中的人工介入还可以更一等公民：

- work unit 可以进入 `waiting_user_decision`。
- rescue / reroute / destructive action 前可以产生审批 item。
- 审批 item 应绑定 run、work unit、provider job、上游证据和推荐动作。

### 5. Adapter 层直接接 Claude SDK / Codex SDK

HiveWard 的 `packages/adapter/src/sdk-runtime/` 做了 provider runtime：

- `AgentSdkRuntimeRouter` 路由 `claude` / `codex`。
- `CodexAgentSdkRuntime` 用 `@openai/codex-sdk` startThread / run / runStreamed。
- task registry 控制最大并发、取消、超时。
- prompt envelope 会稳定 stringify 上游输入、redact secrets、注入 output schema。

可借鉴点：本仓库当前定位是 provider/runtime 插件边界，默认 `cli_inherit` 更贴近真实本地工作；HiveWard 的 SDK runtime 对我们最有价值的是：

- 把 prompt envelope 标准化为 provider 无关输入。
- 把 outputSchema 校验放在 adapter 边界。
- task registry 并发 / timeout / cancel 可以作为 JobRuntime 的一个实现参考。
- secret redaction 应进入所有 provider prompt envelope，而不是只靠调用约定。

不建议直接替换为 SDK-first。对本仓库而言，CLI 原生权限、全局配置、桌面工作流仍然很重要；SDK runtime 更适合作为 `direct_api` / controlled worker 的另一种 backend。

### 6. 文件式 store 的产品闭环快，但后期会有边界压力

HiveWard 的 store 用一个 index 加 blueprints/runs 目录做持久化，并用 operationQueue 串行化文件写入。这对 beta 产品很实用：部署简单、状态可观察、容易迁移。

本仓库已经有 `.agent_orchestrator/` 的 plans / jobs / evidence / events / messages。可以借鉴它的“index + archives”形态，补一个 `workspace-index.json` 或 `program-index.json`，统一索引：

- active plan sessions
- approved plans
- execution runs
- jobs
- evidence reports
- pending approvals
- provider health snapshots

## codewale 判断

`singhk1988/codewale` 是空仓库。`jeelptl2005/codewale` 是一个 Flask 教学网站：

- `app.py`：Flask、PyMySQL、bcrypt、SMTP、session、课程进度、quiz/final test/certificate。
- `templates/`：页面模板。
- `static/uploads/`：课程 PDF。
- `data/languages/python.json`：课程内容数据。

它对本仓库的 agent orchestration 没有直接借鉴价值。唯一很弱的参考是“课程进度 / final test / certificate”的学习产品闭环，但和当前路线不匹配，建议不投入。

## 对 Agent Orchestrator 的建议路线

### 立即可借鉴，低风险

1. **补一份 topology DSL 设计稿**  
   以本仓库现有 ExecutionContract 为核心，借 HiveWard 的 `agent / manager / slot / condition / summary / approval` 概念，定义我们的只读拓扑快照。不急着做 UI。

2. **把人工审批建模进 execution run**  
   当前 plan approve 是治理层审批；还需要 execution-level approval item，尤其是 rescue、reroute、破坏性命令、跨 provider fallback。

3. **标准化 provider prompt envelope**  
   加上 stable stringify、secret redaction、schema hints、上游结果摘要预算。HiveWard 这里做得很清楚。

4. **补 workspace/program index**  
   把 `.agent_orchestrator/` 下分散 artifact 的“当前现场”统一起来，让 CLI / 未来 UI 都能读同一个入口。

### 中期可借鉴，中等风险

5. **Manager / Slot execution topology**  
   把 `speed_first` 的并行、`success_first` 的审查、`cost_first` 的收敛，映射成拓扑而不是只映射成 policy profile。这样 runbook 和 UI 能解释得更直观。

6. **Blueprint import/export 类似能力**  
   本仓库可以叫 `execution template` 或 `program template`，先支持 JSON export/import，再考虑图形化编辑。

7. **Role directory**  
   把当前 team workflow 里的 planner / reviewer / implementer / rescuer / summarizer 显式建成角色目录，绑定 provider/runtime 偏好、权限 profile、工作目录和职责说明。

### 暂不建议直接跟进

8. **完整 React Flow 画布**  
   视觉画布很诱人，但会显著吞噬实现预算。更适合等 CLI schema 和 runtime trace 稳定后再做。

9. **SDK-first provider runtime**  
   HiveWard 的 SDK runtime 对 SaaS/control-plane 友好；本仓库更强调本地真实 Codex/Claude 行为，短期仍应保留 CLI-first，并把 SDK 作为插件实现之一。

10. **OpenClaw 深集成**  
   除非用户明确转向 OpenClaw，否则不要把路线绑到 OpenClaw gateway。保持 provider/runtime 插件边界更安全。

## 最重要的结论

HiveWard 验证了一个关键判断：你的方向不是“再封装一个 agent CLI”，而是要做 **长周期 agent work 的上层组织、治理、调度、审批和复盘系统**。它在产品表达上已经把“公司 / CEO / Leader / 蓝图 / 审批 / 历史”讲得很顺；本仓库则在“计划治理 / 审查 / 执行策略 / 合规证据”上更深。

最佳路线不是复制 HiveWard，而是：

```text
Agent Orchestrator 决策核心
  + HiveWard 式组织/蓝图/审批产品语言
  + 本地 CLI-first provider/runtime 插件
  = 更适合个人真实工作的 long-cycle agent company/workbench
```

如果要排一个下一步，我建议先做：

> `ExecutionTopologySnapshot` + `PendingApprovalItem` + `WorkspaceIndex` 三件套。

它们能吸收 HiveWard 的核心优点，又不会让我们过早陷入前端画布工程。

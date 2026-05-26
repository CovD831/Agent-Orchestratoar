const state = {
  selectedSessionId: null,
  selectedJobId: null,
  selectedPlanNodeId: null,
  selectedPlanAgentIds: [],
  globalStream: null,
  sessionStream: null,
  streamRefreshTimer: null,
  agentConfig: null,
  selectedAgentRole: "planner",
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || response.statusText);
  }
  return response.json();
}

function badge(status, id = "") {
  const idAttr = id ? ` id="${escapeHtml(id)}"` : "";
  return `<span${idAttr} class="badge ${String(status || "idle")}">${statusLabel(status)}</span>`;
}

function renderSessions(payload) {
  const sessions = payload.sessions || [];
  $("sessions").innerHTML = sessions.length
    ? sessions.map((session) => `
      <button class="session-item ${session.id === state.selectedSessionId ? "active" : ""}" data-session="${session.id}">
        <strong>${escapeHtml(session.goal || session.requirement || session.id)}</strong>
        <span class="summary">${escapeHtml(session.id)} · ${escapeHtml(statusLabel(session.status))}</span>
        <span class="summary">下一步：${escapeHtml(actionLabel(session.primary_action || "inspect_session"))}</span>
      </button>
    `).join("")
    : `<div class="session-item"><strong>暂无会话</strong><span class="summary">启动一个团队任务后，这里会出现监控对象。</span></div>`;
  document.querySelectorAll("[data-session]").forEach((node) => {
    node.addEventListener("click", () => selectSession(node.dataset.session));
  });
  if (!state.selectedSessionId && sessions[0]) {
    selectSession(sessions[0].id);
  }
}

function renderSession(payload) {
  const session = payload.session || {};
  const summary = session.status_summary || {};
  const brief = session.structured_brief || {};
  $("next-title").textContent = actionLabel(payload.next_action?.primary_action || "inspect_session");
  $("next-reason").textContent = payload.next_action?.primary_reason || "";
  $("action-buttons").innerHTML = actionButtons(payload.next_action?.primary_action);
  $("governance-summary").innerHTML = renderGovernanceSummary(payload.governance_summary || {});
  $("session-title").textContent = brief.goal || session.requirement || session.id || "未选择会话";
  $("session-status").outerHTML = badge(session.status, "session-status");
  $("session-meta").innerHTML = [
    ["会话", session.id],
    ["阶段", phaseLabel(summary.phase || session.resume?.current_phase)],
    ["拓扑", topologyLabel(summary.selected_topology)],
    ["运行时", compactProvider(session.decision_verdict?.selected_provider_runtime || {})],
  ].map(([label, value]) => `<div class="meta"><span>${label}</span>${escapeHtml(value || "未知")}</div>`).join("");
  $("runbook").innerHTML = (payload.runbook || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  $("decision").textContent = JSON.stringify({
    decision_verdict: session.decision_verdict,
    gaps: session.gaps,
    linked_execution: payload.linked_execution,
  }, null, 2);
  $("evidence-summary").innerHTML = renderEvidenceSummary(payload.evidence_summary || {});
  $("operator-summary").innerHTML = renderOperatorSummary(payload.operator_summary || {});
  renderPlanTree(payload.plan_tree);
  $("agent-cards").innerHTML = (payload.agent_cards || []).length
    ? renderRoleGroups(payload)
    : renderEmptyAgentCard();
  bindActionButtons();
  bindPlanNodes(payload);
}

function actionButtons(action) {
  const map = {
    lead_chat: "继续沟通",
    mark_draft_ready: "确认初稿",
    submit_review: "提交审查",
    approve: "批准计划",
    execute: "开始执行",
    retry_review: "重试审核",
    retry_adversarial_review: "重试对抗审核",
    inspect_execution: "查看执行",
    revise: "修订计划",
    inspect_session: "刷新状态",
  };
  const label = map[action] || "刷新状态";
  return `<button class="primary" data-action="${action || "inspect_session"}">${label}</button>`;
}

function bindActionButtons() {
  document.querySelectorAll("[data-action]").forEach((node) => {
    node.addEventListener("click", async () => {
      await runAction(node.dataset.action);
    });
  });
}

async function runAction(action) {
  if (!state.selectedSessionId) return;
  const base = `/api/sessions/${state.selectedSessionId}`;
  if (action === "approve") await api(`${base}/approve`, { method: "POST" });
  else if (action === "execute") await api(`${base}/execute`, { method: "POST", body: JSON.stringify({ mode: "success_first" }) });
  else if (action === "mark_draft_ready") await api(`${base}/draft-ready`, { method: "POST" });
  else if (action === "submit_review") await api(`${base}/submit-review`, { method: "POST" });
  else if (action === "lead_chat") {
    const message = window.prompt("发给计划主控的消息");
    if (!message) return;
    await api(`${base}/chat`, { method: "POST", body: JSON.stringify({ message }) });
  }
  else if (action === "retry_review") await api(`${base}/retry-review`, { method: "POST" });
  else if (action === "retry_adversarial_review") await api(`${base}/retry-adversarial-review`, { method: "POST" });
  else if (action === "revise") {
    const summary = window.prompt("请输入修订摘要");
    if (!summary) return;
    await api(`${base}/revise`, { method: "POST", body: JSON.stringify({ summary, closed_gap_ids: [] }) });
  }
  await selectSession(state.selectedSessionId);
  await refreshSessions();
}

function renderAgentCard(card) {
  const status = String(card.status || "unknown");
  const provider = String(card.provider || "agent");
  const model = card.model ? ` · ${card.model}` : "";
  const kind = String(card.role_label || card.kind || "work");
  const initials = provider.slice(0, 1).toUpperCase();
  return `
    <div class="agent-card ${escapeHtml(status)} ${isCardDimmed(card) ? "dimmed" : ""}" data-agent-card="${escapeHtml(card.id || "")}">
      <div class="agent-main">
        <div class="agent-avatar">${escapeHtml(initials)}</div>
        <div class="agent-title">
          <strong>${escapeHtml(kind)}</strong>
          <span class="summary">${escapeHtml(providerLabel(provider))}${escapeHtml(model)} · ${escapeHtml(kindLabel(card.kind))}</span>
        </div>
      </div>
      <div class="agent-task">${escapeHtml(card.current_action || card.summary || card.error || "暂无活动摘要")}</div>
      <div class="agent-footer">
        ${badge(status)}
        <span>${escapeHtml(card.id || "无任务 ID")}</span>
      </div>
    </div>
  `;
}

function renderPlanTree(tree) {
  if (!tree || !Array.isArray(tree.children)) {
    $("plan-tree").innerHTML = "";
    return;
  }
  const nodes = [tree, ...tree.children].slice(0, 8);
  $("plan-tree").innerHTML = nodes.map((node) => `
    <button class="plan-node ${escapeHtml(node.state || "planned")} ${state.selectedPlanNodeId === node.id ? "active" : ""}" data-plan-node="${escapeHtml(node.id)}">
      <span class="eyebrow">${escapeHtml(nodeKindLabel(node.kind))}</span>
      <strong>${escapeHtml(node.label || node.id)}</strong>
      <small>${escapeHtml(statusLabel(node.status || node.state))}</small>
    </button>
  `).join("");
}

function bindPlanNodes(payload) {
  document.querySelectorAll("[data-plan-node]").forEach((node) => {
    node.addEventListener("click", () => {
      const planNode = findPlanNode(payload.plan_tree, node.dataset.planNode);
      state.selectedPlanNodeId = planNode?.id || null;
      state.selectedPlanAgentIds = planNode?.related_agent_ids || [];
      renderSession(payload);
    });
  });
}

function findPlanNode(root, id) {
  if (!root || !id) return null;
  if (root.id === id) return root;
  for (const child of root.children || []) {
    const found = findPlanNode(child, id);
    if (found) return found;
  }
  return null;
}

function isCardDimmed(card) {
  if (!state.selectedPlanAgentIds.length) return false;
  return !state.selectedPlanAgentIds.includes(card.id);
}

function renderRoleGroups(payload) {
  const groups = payload.role_groups || [];
  if (!groups.length) {
    return (payload.agent_cards || []).map(renderAgentCard).join("");
  }
  return groups.map((group) => {
    const cards = group.cards || [];
    return `
      <section class="role-group">
        <div class="role-head">
          <strong>${escapeHtml(group.layer_label || layerLabel(group.layer))}</strong>
          <span>${escapeHtml(cards.length)} 个</span>
        </div>
        <div class="role-cards">
          ${cards.length ? cards.map(renderAgentCard).join("") : renderLayerEmptyCard(group)}
        </div>
      </section>
    `;
  }).join("");
}

function renderLayerEmptyCard(group) {
  return `
    <div class="agent-card idle">
      <div class="agent-main">
        <div class="agent-avatar">${escapeHtml((group.layer_label || "层").slice(0, 1))}</div>
        <div class="agent-title">
          <strong>${escapeHtml(group.layer_label || layerLabel(group.layer))}</strong>
          <span class="summary">暂无活动</span>
        </div>
      </div>
      <div class="agent-task">该层级当前没有可见 agent/job。</div>
      <div class="agent-footer">${badge("idle")}<span>${escapeHtml(group.layer || "layer")}</span></div>
    </div>
  `;
}

function renderJobs(payload) {
  const jobs = payload.jobs || [];
  $("jobs").innerHTML = jobs.length
    ? jobs.map((job) => `
      <button class="job-item ${job.id === state.selectedJobId ? "active" : ""}" data-job="${job.id}">
        <div class="card-head"><strong>${escapeHtml(job.provider)} · ${escapeHtml(job.kind)}</strong>${badge(job.status)}</div>
        <span class="summary">${escapeHtml(job.output_preview || job.summary || job.id)}</span>
        <span class="summary">pid:${escapeHtml(job.pid || "-")} · exit:${escapeHtml(job.exit_code ?? "-")} · ${job.terminal_ref ? escapeHtml(job.terminal_ref) : job.log_available ? "有日志" : "无日志"}</span>
        <span class="summary">${escapeHtml(job.last_log_excerpt || job.last_seen_at || "")}</span>
      </button>
    `).join("")
    : `<div class="job-item"><strong>暂无最近任务</strong><span class="summary">任务活动会显示在这里。</span></div>`;
  document.querySelectorAll("[data-job]").forEach((node) => {
    node.addEventListener("click", () => selectJob(node.dataset.job));
  });
}

async function selectSession(id) {
  state.selectedSessionId = id;
  const payload = await api(`/api/sessions/${id}`);
  renderSession(payload);
  await refreshSessions(false);
  connectSessionStream(id);
}

async function selectJob(id) {
  state.selectedJobId = id;
  const [detail, payload] = await Promise.all([
    api(`/api/jobs/${id}`),
    api(`/api/jobs/${id}/log`),
  ]);
  $("log-title").textContent = `任务日志 · ${id}`;
  $("log").textContent = payload.log || "这个任务暂无日志。";
  $("log").scrollTop = $("log").scrollHeight;
  $("job-actions").innerHTML = `
    <button data-job-command="send" ${isTerminalJob(detail) ? "" : "disabled"}>发送</button>
    <button data-job-command="cancel" ${isTerminalJob(detail) ? "" : "disabled"}>取消</button>
    <span class="summary">${escapeHtml(detail.terminal_ref || detail.phase || "无终端")}</span>
    <span class="summary">${escapeHtml(operationLabel(detail.operation))}</span>
  `;
  bindJobCommands();
  document.querySelectorAll("[data-job]").forEach((node) => {
    node.classList.toggle("active", node.dataset.job === id);
  });
}

async function refreshSessions(autoselect = true) {
  const payload = await api("/api/sessions");
  renderSessions(payload);
  if (autoselect && state.selectedSessionId) {
    await selectSession(state.selectedSessionId);
  }
}

async function refreshJobs() {
  renderJobs(await api("/api/jobs"));
}

async function refreshAgentConfig() {
  if (!$("agent-config")) return;
  state.agentConfig = await api("/api/agent-config");
  renderAgentConfig();
}

function renderAgentConfig() {
  if (!$("agent-config")) return;
  const profiles = state.agentConfig?.profiles || {};
  const roles = Object.keys(profiles);
  const selected = profiles[state.selectedAgentRole] || profiles[roles[0]] || {};
  if (!profiles[state.selectedAgentRole] && roles[0]) state.selectedAgentRole = roles[0];
  $("agent-config").innerHTML = `
    <label class="field">
      <span>角色</span>
      <select id="agent-role">
        ${roles.map((role) => `<option value="${escapeHtml(role)}" ${role === state.selectedAgentRole ? "selected" : ""}>${escapeHtml(agentRoleLabel(role))}</option>`).join("")}
      </select>
    </label>
    <div class="agent-config-grid">
      <label class="field">
        <span>Provider</span>
        <select id="agent-provider">
          ${["codex", "claude", "mock"].map((provider) => `<option value="${provider}" ${provider === selected.provider ? "selected" : ""}>${providerLabel(provider)}</option>`).join("")}
        </select>
      </label>
      <label class="field">
        <span>Model</span>
        <input id="agent-model" value="${escapeHtml(selected.model || "")}" placeholder="sonnet / opus / gpt-5.4" />
      </label>
    </div>
    <label class="field">
      <span>Prompt Template</span>
      <textarea id="agent-prompt" rows="4">${escapeHtml(selected.prompt_template || "{default_prompt}")}</textarea>
    </label>
    <button class="primary" type="submit">保存配置</button>
  `;
  $("agent-role").addEventListener("change", () => {
    state.selectedAgentRole = $("agent-role").value;
    renderAgentConfig();
  });
}

async function saveAgentConfig(event) {
  event.preventDefault();
  if (!state.agentConfig?.profiles) return;
  const role = state.selectedAgentRole;
  const current = state.agentConfig.profiles[role] || {};
  state.agentConfig.profiles[role] = {
    ...current,
    role,
    provider: $("agent-provider").value,
    model: $("agent-model").value.trim() || null,
    prompt_template: $("agent-prompt").value.trim() || "{default_prompt}",
  };
  state.agentConfig = await api("/api/agent-config", {
    method: "POST",
    body: JSON.stringify(state.agentConfig),
  });
  renderAgentConfig();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderEmptyAgentCard() {
  return renderRoleGroups({
    role_groups: [
      { layer: "decision", layer_label: "决策层", cards: [] },
      { layer: "execution", layer_label: "执行层", cards: [] },
      {
        layer: "review",
        layer_label: "审核层",
        cards: [
          {
            role_label: "暂无可见 Agent",
            provider: "agent",
            kind: "idle",
            status: "idle",
            current_action: "启动或选择一个团队会话。Agent 的工作会以独立格子显示在这里。",
            id: "grid-ready",
          },
        ],
      },
      { layer: "rescue", layer_label: "救援层", cards: [] },
      { layer: "runtime", layer_label: "运行时层", cards: [] },
    ],
  });
}

function compactProvider(value) {
  if (!value || typeof value !== "object") return "";
  return Object.entries(value)
    .map(([key, item]) => `${key}:${item}`)
    .join(" · ");
}

function renderGovernanceSummary(summary) {
  const provider = compactProvider(summary.selected_provider_runtime || {});
  const message = governanceMessage(summary);
  return [
    ["门禁", gateLabel(summary.gate_status)],
    ["审查", reviewIntensityLabel(summary.review_intensity)],
    ["合规", complianceLabel(summary.compliance_status)],
    ["命令", `${summary.recommended_command_count || 0} 条`],
  ].map(([label, value]) => `
    <div class="governance-item"><span>${escapeHtml(label)}</span>${escapeHtml(value || "未知")}</div>
  `).join("") + `<div class="governance-message">${escapeHtml(message || provider || "等待治理信号")}</div>`;
}

function renderEvidenceSummary(summary) {
  return [
    ["审核轮", summary.review_round_count || 0],
    ["缺口", summary.gap_count || 0],
    ["委派", summary.delegated_job_count || 0],
    ["Provider", (summary.providers || []).join(" / ") || "无"],
  ].map(([label, value]) => `
    <div class="evidence-item"><span>${escapeHtml(label)}</span>${escapeHtml(value)}</div>
  `).join("");
}

function renderOperatorSummary(summary) {
  const provenance = summary.execution_provenance || {};
  const reviewPolicy = summary.review_policy || {};
  const fallback = summary.fallback_snapshot || {};
  const compliance = summary.compliance_snapshot || {};
  const graph = summary.work_graph_summary || {};
  const messages = summary.message_timeline || [];
  const events = summary.event_timeline || [];
  return `
    <div class="operator-grid">
      <div class="operator-item"><span>执行</span>${escapeHtml(provenance.linked_run_status || provenance.approved_plan_goal || "未执行")}</div>
      <div class="operator-item"><span>审查策略</span>${escapeHtml(reviewPolicy.policy_name || "未知")}</div>
      <div class="operator-item"><span>Fallback</span>${escapeHtml(fallback.recovery_provider_fallback_reason || compactProvider(fallback.provider_runtime || {}) || "无")}</div>
      <div class="operator-item"><span>合规</span>${escapeHtml(complianceLabel(compliance.status))}</div>
      <div class="operator-item"><span>工作图</span>${escapeHtml(`${graph.node_count || 0} 节点 / ${graph.edge_count || 0} 边`)}</div>
      <div class="operator-item"><span>消息</span>${escapeHtml(`${messages.length} 条`)}</div>
    </div>
    <div class="operator-timeline">
      ${events.slice(0, 3).map((event) => `<span>${escapeHtml(event.message || event.type || "event")}</span>`).join("")}
      ${messages.slice(0, 3).map((message) => `<span>${escapeHtml(message.content || message.message_type || "message")}</span>`).join("")}
    </div>
  `;
}

function statusLabel(status) {
  const map = {
    idle: "空闲",
    intake_chat: "沟通中",
    draft_ready: "初稿已确认",
    adversarial_review: "对抗审查",
    awaiting_human_confirmation: "等待确认",
    drafting: "起草中",
    in_review: "审核中",
    needs_revision: "需修订",
    approved_for_execution: "已批准",
    executing: "执行中",
    accepted: "已验收",
    needs_followup: "需跟进",
    blocked: "已阻塞",
    awaiting_human: "等待人工",
    running: "运行中",
    working: "工作中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
    done: "完成",
    unknown: "未知",
  };
  return map[String(status || "idle")] || String(status || "空闲");
}

function actionLabel(action) {
  const map = {
    lead_chat: "继续沟通",
    mark_draft_ready: "确认初稿",
    submit_review: "提交审查",
    inspect_delegated_job: "查看委派任务",
    inspect_compliance: "查看合规状态",
    revise: "修订计划",
    approve: "批准计划",
    execute: "开始执行",
    inspect_execution: "查看执行结果",
    retry_review: "重试审核",
    retry_adversarial_review: "重试对抗审核",
    human_decision: "等待人工决策",
    inspect_session: "查看会话",
  };
  return map[String(action || "inspect_session")] || String(action || "查看会话");
}

function phaseLabel(phase) {
  return statusLabel(phase);
}

function topologyLabel(topology) {
  const map = {
    team: "团队",
    solo: "单 Agent",
    team_with_adversarial_review: "团队 + 对抗审核",
    cluster: "集群",
  };
  return map[String(topology || "")] || topology || "";
}

function layerLabel(layer) {
  const map = {
    decision: "决策层",
    execution: "执行层",
    review: "审核层",
    rescue: "救援层",
    runtime: "运行时层",
  };
  return map[String(layer || "")] || layer || "";
}

function kindLabel(kind) {
  const map = {
    session_lead: "主控",
    authoring: "起草",
    review: "审核",
    review_retry: "审核重试",
    adversarial_review: "对抗审核",
    adversarial_review_retry: "对抗重试",
    implementation: "实现",
    runtime: "运行时",
    delegated: "委派",
  };
  return map[String(kind || "")] || kind || "任务";
}

function nodeKindLabel(kind) {
  const map = {
    session: "会话",
    subtask: "子任务",
    gap: "缺口",
    review_round: "审核轮",
    execution_run: "执行",
  };
  return map[String(kind || "")] || kind || "节点";
}

function providerLabel(provider) {
  const map = {
    claude: "Claude",
    codex: "Codex",
    mock: "Mock",
  };
  return map[String(provider || "")] || provider || "Agent";
}

function agentRoleLabel(role) {
  const map = {
    planner: "计划主控",
    plan_reviewer: "计划审核",
    adversarial_reviewer: "对抗审核",
    worker: "执行 Agent",
    execution_reviewer: "执行审核",
    rescue: "救援 Agent",
    ideation_proponent: "正方构想",
    ideation_skeptic: "反方质询",
  };
  return map[String(role || "")] || role || "Agent";
}

function gateLabel(gate) {
  const map = {
    open: "开放",
    approved: "已批准",
    blocked: "阻塞",
    needs_revision: "需修订",
    completed: "完成",
  };
  return map[String(gate || "")] || gate || "未知";
}

function reviewIntensityLabel(value) {
  const map = {
    standard: "标准",
    reviewed: "已审核",
    strict: "严格",
  };
  return map[String(value || "")] || value || "未知";
}

function complianceLabel(value) {
  const map = {
    passed: "通过",
    warning: "警告",
    blocked: "阻塞",
    unknown: "未知",
  };
  return map[String(value || "unknown")] || value || "未知";
}

function governanceMessage(summary) {
  if (summary.blocking_reasons && summary.blocking_reasons.length) {
    return `阻塞：${summary.blocking_reasons[0]}`;
  }
  if (summary.recovery_actions && summary.recovery_actions.length) {
    return `恢复路径：${summary.recovery_actions.join(" -> ")}`;
  }
  if (summary.primary_reason) {
    return summary.primary_reason;
  }
  if (summary.recovery_provider_fallback_reason) {
    return `Provider fallback：${summary.recovery_provider_fallback_reason}`;
  }
  return "";
}

function isTerminalJob(job) {
  return !["completed", "failed", "cancelled"].includes(String(job.status || ""));
}

function operationLabel(operation) {
  if (!operation || typeof operation !== "object") return "操作状态：未执行";
  return `操作状态：${operation.status || "unknown"} · ${operation.detail || operation.reason || ""}`;
}

function bindJobCommands() {
  document.querySelectorAll("[data-job-command]").forEach((node) => {
    node.addEventListener("click", async () => {
      if (!state.selectedJobId) return;
      if (node.dataset.jobCommand === "send") {
        const message = window.prompt("发送给任务的消息");
        if (!message) return;
        await api(`/api/jobs/${state.selectedJobId}/send`, { method: "POST", body: JSON.stringify({ message }) });
      } else if (node.dataset.jobCommand === "cancel") {
        await api(`/api/jobs/${state.selectedJobId}/cancel`, { method: "POST" });
      }
      await selectJob(state.selectedJobId);
      await refreshJobs();
    });
  });
}

function scheduleStreamRefresh(scope = "all") {
  if (state.streamRefreshTimer) return;
  state.streamRefreshTimer = window.setTimeout(async () => {
    state.streamRefreshTimer = null;
    if (scope === "sessions" || scope === "all") {
      await refreshSessions(false);
      if (state.selectedSessionId) await selectSession(state.selectedSessionId);
    }
    if (scope === "jobs" || scope === "all") {
      await refreshJobs();
      if (state.selectedJobId) await selectJob(state.selectedJobId);
    }
  }, 250);
}

function connectStreams() {
  if (!window.EventSource) return;
  connectGlobalStream();
  if (state.selectedSessionId) connectSessionStream(state.selectedSessionId);
}

function connectGlobalStream() {
  if (state.globalStream) state.globalStream.close();
  state.globalStream = new EventSource("/api/stream");
  state.globalStream.addEventListener("orchestration_event", () => scheduleStreamRefresh("sessions"));
  state.globalStream.addEventListener("team_message", () => scheduleStreamRefresh("sessions"));
  state.globalStream.addEventListener("job_update", () => scheduleStreamRefresh("jobs"));
  state.globalStream.onerror = () => {
    state.globalStream.close();
    state.globalStream = null;
  };
}

function connectSessionStream(id) {
  if (!window.EventSource || !id) return;
  if (state.sessionStream) state.sessionStream.close();
  state.sessionStream = new EventSource(`/api/sessions/${encodeURIComponent(id)}/stream`);
  state.sessionStream.addEventListener("orchestration_event", () => scheduleStreamRefresh("sessions"));
  state.sessionStream.addEventListener("team_message", () => scheduleStreamRefresh("sessions"));
  state.sessionStream.addEventListener("job_update", () => scheduleStreamRefresh("jobs"));
  state.sessionStream.onerror = () => {
    state.sessionStream.close();
    state.sessionStream = null;
  };
}

$("refresh").addEventListener("click", async () => {
  await refreshSessions();
  await refreshJobs();
  await refreshAgentConfig();
});

if ($("agent-config")) {
  $("agent-config").addEventListener("submit", saveAgentConfig);
}

$("new-session").addEventListener("submit", async (event) => {
  event.preventDefault();
  const requirement = $("requirement").value.trim();
  if (!requirement) return;
  const session = await api("/api/sessions", { method: "POST", body: JSON.stringify({ requirement }) });
  $("requirement").value = "";
  state.selectedSessionId = session.id;
  await refreshSessions();
});

refreshSessions();
refreshJobs();
refreshAgentConfig();
connectStreams();
setInterval(() => {
  refreshSessions(false);
  refreshJobs();
  if (state.selectedSessionId) selectSession(state.selectedSessionId);
  if (state.selectedJobId) selectJob(state.selectedJobId);
}, 5000);

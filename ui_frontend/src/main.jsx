import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const api = async (path, options = {}) => {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
};

const labels = {
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
  pending: "排队中",
  completed: "完成",
  failed: "失败",
  cancelled: "取消",
  codex: "Codex",
  claude: "Claude",
  mock: "Mock",
  decision_core: "Decision Core",
  parallel: "并行拓扑",
  serial: "串行拓扑",
  hybrid: "混合拓扑",
};

const navItems = [
  { id: "command", label: "作战室", detail: "当前现场" },
  { id: "governance", label: "计划治理", detail: "审查 / 批准" },
  { id: "runtime", label: "执行监控", detail: "Agent / Job" },
  { id: "evidence", label: "证据与历史", detail: "日志 / 合规" },
];

const statusLabel = (value) => labels[value] || value || "未知";
const terminalStatuses = new Set(["completed", "failed", "cancelled"]);

function App() {
  const [sessions, setSessions] = useState([]);
  const [selectedSessionId, setSelectedSessionId] = useState(null);
  const [sessionPayload, setSessionPayload] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [selectedJobId, setSelectedJobId] = useState(null);
  const [jobLog, setJobLog] = useState("选择一个 Provider Job 查看日志。");
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [inspectorTab, setInspectorTab] = useState("overview");
  const [activeNav, setActiveNav] = useState("command");

  const session = sessionPayload?.session || {};
  const brief = session.structured_brief || {};
  const nextAction = sessionPayload?.next_action || {};
  const actions = sessionPayload?.actions || [];
  const selectedJob = useMemo(() => jobs.find((job) => job.id === selectedJobId), [jobs, selectedJobId]);
  const sessionTitle = brief.goal || session.requirement || selectedSessionId || "描述你想让团队完成的事";
  const transcript = useMemo(() => buildTranscript(sessionPayload), [sessionPayload]);
  const activeJobs = useMemo(
    () => jobs.filter((job) => ["running", "working", "pending"].includes(String(job.status))).slice(0, 6),
    [jobs],
  );
  const enabledActions = actions.filter((action) => action.enabled);
  const metrics = useMemo(() => buildWorkbenchMetrics(sessions, jobs, sessionPayload), [sessions, jobs, sessionPayload]);
  const pendingItems = useMemo(() => buildPendingItems(sessionPayload, jobs), [sessionPayload, jobs]);

  const refreshSessions = async (autoselect = true) => {
    const payload = await api("/api/sessions");
    const nextSessions = payload.sessions || [];
    setSessions(nextSessions);
    if (autoselect && !selectedSessionId && nextSessions[0]) setSelectedSessionId(nextSessions[0].id);
  };

  const refreshJobs = async () => {
    const payload = await api("/api/jobs");
    setJobs(payload.jobs || []);
  };

  const refreshSelectedSession = async () => {
    if (!selectedSessionId) return;
    setSessionPayload(await api(`/api/sessions/${selectedSessionId}`));
  };

  useEffect(() => {
    Promise.all([refreshSessions(), refreshJobs()]).catch((err) => setError(String(err)));
  }, []);

  useEffect(() => {
    if (!selectedSessionId) {
      setSessionPayload(null);
      return;
    }
    api(`/api/sessions/${selectedSessionId}`).then(setSessionPayload).catch((err) => setError(String(err)));
  }, [selectedSessionId]);

  useEffect(() => {
    if (!selectedJobId) return;
    Promise.all([api(`/api/jobs/${selectedJobId}`), api(`/api/jobs/${selectedJobId}/log`)])
      .then(([detail, log]) => {
        setJobs((current) => current.map((job) => (job.id === selectedJobId ? { ...job, ...detail } : job)));
        setJobLog(log.log || "这个 Provider Job 暂无日志。");
      })
      .catch((err) => setError(String(err)));
  }, [selectedJobId]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      refreshSessions(false).catch(() => {});
      refreshJobs().catch(() => {});
      if (selectedSessionId) api(`/api/sessions/${selectedSessionId}`).then(setSessionPayload).catch(() => {});
    }, 5000);
    return () => window.clearInterval(timer);
  }, [selectedSessionId]);

  const submitDraft = async (event) => {
    event.preventDefault();
    const text = draft.trim();
    if (!text || busy) return;
    setBusy(true);
    setError("");
    try {
      if (!selectedSessionId) {
        const created = await api("/api/sessions", { method: "POST", body: JSON.stringify({ requirement: text }) });
        setSelectedSessionId(created.id);
        setActiveNav("governance");
        setDraft("");
        await refreshSessions(false);
      } else if (canRevise(sessionPayload)) {
        await api(`/api/sessions/${selectedSessionId}/revise`, {
          method: "POST",
          body: JSON.stringify({ summary: text, closed_gap_ids: openGapIds(session) }),
        });
        setDraft("");
        await Promise.all([refreshSelectedSession(), refreshSessions(false)]);
      } else if (canLeadChat(sessionPayload)) {
        await api(`/api/sessions/${selectedSessionId}/chat`, {
          method: "POST",
          body: JSON.stringify({ message: text }),
        });
        setDraft("");
        await Promise.all([refreshSelectedSession(), refreshSessions(false)]);
      } else if (selectedJobId && isLiveJob(selectedJob)) {
        await api(`/api/jobs/${selectedJobId}/terminal/input`, { method: "POST", body: JSON.stringify({ message: text }) });
        setDraft("");
        setJobLog((current) => `${current}\n\n> ${text}`);
      } else {
        setError("当前会话已经进入只读检查阶段。可以执行主操作，或选择一个运行中的 Provider Job 后发送输入。");
      }
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  };

  const runAction = async (actionId) => {
    if (!selectedSessionId || busy) return;
    const base = `/api/sessions/${selectedSessionId}`;
    setBusy(true);
    setError("");
    try {
      if (actionId === "execute") await api(`${base}/execute`, { method: "POST", body: JSON.stringify({ mode: "success_first" }) });
      else if (actionId === "approve") await api(`${base}/approve`, { method: "POST" });
      else if (actionId === "mark_draft_ready") await api(`${base}/draft-ready`, { method: "POST" });
      else if (actionId === "submit_review") await api(`${base}/submit-review`, { method: "POST" });
      else if (actionId === "retry_review") await api(`${base}/retry-review`, { method: "POST" });
      else if (actionId === "retry_adversarial_review") await api(`${base}/retry-adversarial-review`, { method: "POST" });
      else if (actionId === "resume") await api(`${base}/resume`, { method: "POST", body: JSON.stringify({ apply: true }) });
      await Promise.all([refreshSelectedSession(), refreshSessions(false), refreshJobs()]);
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  };

  const sendJobMessage = async () => {
    const text = draft.trim() || window.prompt("发送给当前 Provider Job 的输入");
    if (!selectedJobId || !text) return;
    await api(`/api/jobs/${selectedJobId}/terminal/input`, { method: "POST", body: JSON.stringify({ message: text }) });
    setDraft("");
    setJobLog((current) => `${current}\n\n> ${text}`);
  };

  const chooseSession = (id) => {
    setSelectedSessionId(id);
    setActiveNav(id ? "governance" : "command");
  };

  return (
    <main className="workbench-shell">
      <aside className="command-rail">
        <BrandHeader onRefresh={() => Promise.all([refreshSessions(false), refreshJobs()])} />
        <nav className="product-nav" aria-label="Agent Orchestrator sections">
          {navItems.map((item) => (
            <button key={item.id} className={activeNav === item.id ? "active" : ""} onClick={() => setActiveNav(item.id)}>
              <span>{item.label}</span>
              <small>{item.detail}</small>
            </button>
          ))}
        </nav>
        <SessionRail sessions={sessions} selectedSessionId={selectedSessionId} onSelect={chooseSession} />
      </aside>

      <section className="mission-deck">
        <MissionHeader
          selectedSessionId={selectedSessionId}
          session={session}
          sessionTitle={sessionTitle}
          nextAction={nextAction}
          metrics={metrics}
        />
        {!selectedSessionId ? (
          <WorkbenchHome metrics={metrics} sessions={sessions} jobs={jobs} pendingItems={pendingItems} setActiveNav={setActiveNav} />
        ) : (
          <MissionWorkspace
            payload={sessionPayload}
            jobs={jobs}
            transcript={transcript}
            enabledActions={enabledActions}
            busy={busy}
            runAction={runAction}
            activeNav={activeNav}
            setActiveNav={setActiveNav}
          />
        )}
        <Composer
          draft={draft}
          setDraft={setDraft}
          submitDraft={submitDraft}
          busy={busy}
          selectedSessionId={selectedSessionId}
          sessionPayload={sessionPayload}
          selectedJob={selectedJob}
          enabledActions={enabledActions}
          runAction={runAction}
        />
      </section>

      <aside className="inspector-panel">
        <InspectorTabs active={inspectorTab} onChange={setInspectorTab} />
        {inspectorTab === "overview" ? (
          <OverviewInspector payload={sessionPayload} nextAction={nextAction} metrics={metrics} pendingItems={pendingItems} />
        ) : inspectorTab === "agents" ? (
          <AgentInspector payload={sessionPayload} activeJobs={activeJobs} />
        ) : inspectorTab === "jobs" ? (
          <JobInspector jobs={jobs} selectedJob={selectedJob} selectedJobId={selectedJobId} setSelectedJobId={setSelectedJobId} jobLog={jobLog} sendJobMessage={sendJobMessage} />
        ) : (
          <EvidenceInspector payload={sessionPayload} jobs={jobs} sessions={sessions} />
        )}
      </aside>

      {error ? <div className="error-toast">{error}</div> : null}
    </main>
  );
}

function BrandHeader({ onRefresh }) {
  return (
    <header className="brand-block">
      <div className="brand-mark">AO</div>
      <div>
        <strong>Agent Orchestrator</strong>
        <span>治理型 Agent Workbench</span>
      </div>
      <button className="icon-button" onClick={onRefresh} title="刷新现场">↻</button>
    </header>
  );
}

function SessionRail({ sessions, selectedSessionId, onSelect }) {
  return (
    <section className="session-rail-block">
      <button className={!selectedSessionId ? "new-session active" : "new-session"} onClick={() => onSelect(null)}>
        <span>＋ 新任务</span>
        <small>从计划治理开始</small>
      </button>
      <div className="rail-section-title">
        <span>历史现场</span>
        <b>{sessions.length}</b>
      </div>
      <div className="session-list">
        {sessions.length ? sessions.map((item) => (
          <button key={item.id} className={item.id === selectedSessionId ? "session-card active" : "session-card"} onClick={() => onSelect(item.id)}>
            <strong>{item.goal || item.requirement || item.id}</strong>
            <span>{statusLabel(item.status)} · {item.phase || item.primary_action || "governance"}</span>
          </button>
        )) : <p className="empty-copy">还没有历史会话。</p>}
      </div>
    </section>
  );
}

function MissionHeader({ selectedSessionId, session, sessionTitle, nextAction, metrics }) {
  return (
    <header className="mission-header">
      <div className="mission-title">
        <span className="eyebrow">{selectedSessionId ? "当前治理现场" : "作战室首页"}</span>
        <h1>{sessionTitle}</h1>
        <p>{selectedSessionId ? nextAction.primary_reason || "计划、执行和证据状态会在这里汇合。" : "从任务到计划审查、执行策略、Provider Runtime 和证据闭环的一体化工作台。"}</p>
      </div>
      <div className="mission-status-card">
        <span className={`status-badge ${session.status || "idle"}`}>{statusLabel(session.status || "idle")}</span>
        <strong>{metrics.activeJobs}</strong>
        <small>Active provider jobs</small>
      </div>
    </header>
  );
}

function WorkbenchHome({ metrics, sessions, jobs, pendingItems, setActiveNav }) {
  const recent = sessions.slice(0, 4);
  return (
    <section className="deck-scroll">
      <div className="home-hero panel-glow">
        <span className="eyebrow">Decision Core → Execution Topology → Provider Runtime</span>
        <h2>把长周期 Agent 工作组织成可审查、可执行、可复盘的现场。</h2>
        <p>这里不是普通聊天入口，而是你的计划治理和执行策略控制台。先把任务变成通过审查的计划，再交给可替换的 Codex / Claude / Mock runtime 执行。</p>
      </div>
      <MetricGrid metrics={metrics} />
      <div className="home-grid">
        <section className="panel-card">
          <div className="panel-head"><h2>当前待办</h2><span>{pendingItems.length}</span></div>
          <PendingList items={pendingItems} />
        </section>
        <section className="panel-card">
          <div className="panel-head"><h2>最近现场</h2><button onClick={() => setActiveNav("governance")}>查看治理</button></div>
          <div className="compact-list">
            {recent.length ? recent.map((session) => (
              <div key={session.id} className="compact-row">
                <strong>{session.goal || session.requirement || session.id}</strong>
                <span>{statusLabel(session.status)} · {session.phase || "plan"}</span>
              </div>
            )) : <p className="empty-copy">开始一个新任务后，现场会出现在这里。</p>}
          </div>
        </section>
        <section className="panel-card wide">
          <div className="panel-head"><h2>Provider Runtime</h2><button onClick={() => setActiveNav("runtime")}>查看执行</button></div>
          <ProviderJobStrip jobs={jobs} />
        </section>
      </div>
    </section>
  );
}

function MetricGrid({ metrics }) {
  return (
    <div className="metric-grid">
      <MetricCard label="Sessions" value={metrics.sessions} detail="长期计划现场" />
      <MetricCard label="Governance" value={metrics.governanceOpen} detail="待确认/修订" tone="approval" />
      <MetricCard label="Runtime" value={metrics.activeJobs} detail="运行中 jobs" tone="execute" />
      <MetricCard label="Evidence" value={metrics.completedJobs} detail="已完成 jobs" tone="evidence" />
    </div>
  );
}

function MetricCard({ label, value, detail, tone = "default" }) {
  return (
    <section className={`metric-card ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </section>
  );
}

function MissionWorkspace({ payload, jobs, transcript, enabledActions, busy, runAction, activeNav, setActiveNav }) {
  return (
    <section className="deck-scroll">
      <ActionDock enabledActions={enabledActions} busy={busy} runAction={runAction} />
      <GovernanceBoard payload={payload} />
      <TopologyPreview payload={payload} />
      {activeNav === "runtime" ? <RuntimeBoard payload={payload} jobs={jobs} /> : null}
      {activeNav === "evidence" ? <EvidenceBoard payload={payload} jobs={jobs} /> : null}
      <GovernanceStream transcript={transcript} setActiveNav={setActiveNav} />
    </section>
  );
}

function ActionDock({ enabledActions, busy, runAction }) {
  return (
    <section className="action-dock panel-glow">
      <div>
        <span className="eyebrow">Next operator action</span>
        <h2>{enabledActions[0]?.label || "等待下一步"}</h2>
        <p>{enabledActions.length ? "只显示当前阶段可执行动作，避免把计划治理和执行策略混在一起。" : "计划准备好后，批准、审查或执行动作会在这里出现。"}</p>
      </div>
      <div className="action-buttons">
        {enabledActions.length ? enabledActions.slice(0, 5).map((action) => (
          <button key={action.id} className={action.id === "execute" || action.id === "approve" ? "primary" : "secondary"} type="button" onClick={() => runAction(action.id)} disabled={busy || action.id === "lead_chat" || action.id === "revise"}>
            {action.label}
          </button>
        )) : <span className="muted-pill">暂无可执行动作</span>}
      </div>
    </section>
  );
}

function Composer({ draft, setDraft, submitDraft, busy, selectedSessionId, sessionPayload, selectedJob, enabledActions, runAction }) {
  return (
    <form className="composer-wrap" onSubmit={submitDraft}>
      <div className="composer">
        <textarea
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) submitDraft(event);
          }}
          placeholder={composerPlaceholder(selectedSessionId, sessionPayload, selectedJob)}
        />
        <button className="primary" disabled={busy || !draft.trim()} type="submit">{selectedSessionId ? "发送" : "启动治理"}</button>
      </div>
      <div className="quick-actions">
        {enabledActions.length ? enabledActions.slice(0, 4).map((action) => (
          <button key={action.id} type="button" onClick={() => runAction(action.id)} disabled={busy || action.id === "lead_chat" || action.id === "revise"}>
            {action.label}
          </button>
        )) : <span>计划确认后，主要操作会出现在这里。</span>}
      </div>
    </form>
  );
}

function InspectorTabs({ active, onChange }) {
  const tabs = [
    ["overview", "总览"],
    ["agents", "Agent"],
    ["jobs", "Jobs"],
    ["evidence", "证据"],
  ];
  return (
    <div className="inspector-tabs">
      {tabs.map(([id, label]) => <button key={id} className={active === id ? "active" : ""} onClick={() => onChange(id)}>{label}</button>)}
    </div>
  );
}

function OverviewInspector({ payload, nextAction, metrics, pendingItems }) {
  const session = payload?.session || {};
  const brief = session.structured_brief || {};
  return (
    <div className="inspector-scroll">
      <section className="panel-card highlight">
        <span className="eyebrow">下一步</span>
        <h2>{nextAction.primary_label || "等待计划"}</h2>
        <p>{nextAction.primary_reason || "创建会话后，这里会显示当前建议动作。"}</p>
      </section>
      <section className="panel-card">
        <h2>治理轮廓</h2>
        <div className="fact-grid">
          <Fact label="目标" value={brief.goal || session.requirement || "未开始"} />
          <Fact label="拓扑" value={statusLabel(brief.topology_recommendation?.topology || session.status_summary?.selected_topology)} />
          <Fact label="子任务" value={`${(brief.subtasks || []).length} 个`} />
          <Fact label="缺口" value={`${(session.gaps || []).filter((gap) => gap.status !== "closed").length} 个`} />
        </div>
      </section>
      <section className="panel-card">
        <div className="panel-head"><h2>当前待办</h2><span>{pendingItems.length}</span></div>
        <PendingList items={pendingItems} />
      </section>
      <section className="panel-card">
        <h2>现场指标</h2>
        <div className="mini-metrics">
          <span>Sessions <b>{metrics.sessions}</b></span>
          <span>Running <b>{metrics.activeJobs}</b></span>
          <span>Done <b>{metrics.completedJobs}</b></span>
        </div>
      </section>
    </div>
  );
}

function AgentInspector({ payload, activeJobs }) {
  const groups = payload?.role_groups || [];
  const cards = groups.flatMap((group) => group.cards || []);
  const normalizedCards = cards.length ? cards : fallbackRoleCards(payload);
  return (
    <div className="inspector-scroll">
      <section className="panel-card">
        <h2>Agent Roles</h2>
        <div className="agent-list">
          {normalizedCards.length ? normalizedCards.map((card) => (
            <div key={card.id || `${card.role}-${card.kind}`} className="agent-row">
              <span className={`dot ${card.status || "idle"}`} />
              <div>
                <strong>{card.role_label || card.kind || "Agent"}</strong>
                <p>{card.latest_message_summary || card.current_action || card.summary || "暂无活动"}</p>
              </div>
            </div>
          )) : <p className="empty-copy">暂无可见 agent 活动。</p>}
        </div>
      </section>
      <section className="panel-card">
        <h2>Active Provider Jobs</h2>
        <div className="agent-list">
          {activeJobs.length ? activeJobs.map((job) => (
            <div key={job.id} className="agent-row">
              <span className={`dot ${job.status || "idle"}`} />
              <div>
                <strong>{statusLabel(job.provider)} · {job.kind}</strong>
                <p>{job.last_log_excerpt || job.output_preview || job.summary || job.terminal_ref || job.id}</p>
              </div>
            </div>
          )) : <p className="empty-copy">没有运行中的 job。</p>}
        </div>
      </section>
    </div>
  );
}

function JobInspector({ jobs, selectedJob, selectedJobId, setSelectedJobId, jobLog, sendJobMessage }) {
  return (
    <div className="inspector-scroll">
      <section className="panel-card">
        <h2>Provider Jobs</h2>
        <div className="job-list">
          {jobs.length ? jobs.slice(0, 14).map((job) => (
            <button key={job.id} className={job.id === selectedJobId ? "active" : ""} onClick={() => setSelectedJobId(job.id)}>
              <strong>{statusLabel(job.provider)} · {job.kind}</strong>
              <span>{statusLabel(job.status)} · {job.terminal_ref || job.cwd || job.id}</span>
            </button>
          )) : <p className="empty-copy">暂无任务。</p>}
        </div>
      </section>
      <section className="panel-card log-panel">
        <div className="panel-head">
          <h2>{selectedJob ? `${statusLabel(selectedJob.provider)} · ${selectedJob.kind}` : "任务日志"}</h2>
          <button disabled={!selectedJob || !isLiveJob(selectedJob)} onClick={sendJobMessage}>发送输入</button>
        </div>
        <pre className="log">{jobLog}</pre>
      </section>
    </div>
  );
}

function EvidenceInspector({ payload, jobs, sessions }) {
  const events = payload?.events || [];
  const completed = jobs.filter((job) => terminalStatuses.has(String(job.status))).slice(0, 5);
  return (
    <div className="inspector-scroll">
      <section className="panel-card">
        <h2>Evidence Trail</h2>
        <div className="compact-list">
          {events.length ? [...events].reverse().slice(0, 8).map((event) => (
            <div key={event.id || event.message} className="compact-row">
              <strong>{event.type || "event"}</strong>
              <span>{event.message || "状态已更新"}</span>
            </div>
          )) : <p className="empty-copy">选择会话后显示事件和证据线索。</p>}
        </div>
      </section>
      <section className="panel-card">
        <h2>Completed Jobs</h2>
        <div className="compact-list">
          {completed.length ? completed.map((job) => (
            <div key={job.id} className="compact-row">
              <strong>{statusLabel(job.provider)} · {job.kind}</strong>
              <span>{statusLabel(job.status)} · {job.summary || job.output_preview || job.id}</span>
            </div>
          )) : <p className="empty-copy">还没有完成的 provider job。</p>}
        </div>
      </section>
      <section className="panel-card">
        <h2>History Scope</h2>
        <p className="soft-copy">当前工作台可见 {sessions.length} 个长期计划现场。完整证据仍以 CLI artifact、events、jobs、compliance gate 为准。</p>
      </section>
    </div>
  );
}

function TopologyPreview({ payload }) {
  const session = payload?.session || {};
  const brief = session.structured_brief || {};
  const stages = buildTopologyStages(payload);
  return (
    <section className="topology-card">
      <div className="panel-head">
        <div>
          <span className="eyebrow">Execution Topology Preview</span>
          <h2>{statusLabel(brief.topology_recommendation?.topology || session.status_summary?.selected_topology || "decision_core")}</h2>
        </div>
        <span className={`status-badge ${session.status || "idle"}`}>{statusLabel(session.status || "idle")}</span>
      </div>
      <div className="topology-lanes">
        {stages.map((stage, index) => (
          <div key={stage.id} className={`topology-node ${stage.state}`}>
            <span>{index + 1}</span>
            <strong>{stage.label}</strong>
            <small>{stage.detail}</small>
          </div>
        ))}
      </div>
    </section>
  );
}


function GovernanceBoard({ payload }) {
  const session = payload?.session || {};
  const brief = session.structured_brief || {};
  const gaps = session.gaps || [];
  const openGaps = gaps.filter((gap) => gap.status !== "closed");
  const recommendation = brief.topology_recommendation || {};
  const reviewSignals = buildReviewSignals(payload);
  return (
    <section className="governance-board">
      <div className="governance-main panel-card">
        <span className="eyebrow">Planning Governance</span>
        <h2>{brief.goal || session.requirement || "等待计划目标"}</h2>
        <p>{brief.context || "计划主控会先澄清需求、生成执行合同，再把审查和批准状态沉淀为可恢复的现场。"}</p>
        <div className="governance-facts">
          <Fact label="推荐拓扑" value={statusLabel(recommendation.topology || session.status_summary?.selected_topology || "decision_core")} />
          <Fact label="执行策略" value={session.execution_mode || session.status_summary?.mode || "success_first"} />
          <Fact label="缺口状态" value={openGaps.length ? `${openGaps.length} 个待处理` : "已收敛"} />
        </div>
      </div>
      <div className="governance-side panel-card">
        <div className="panel-head"><h2>Review Signals</h2><span>{reviewSignals.length}</span></div>
        <div className="signal-list">
          {reviewSignals.map((signal) => (
            <div key={signal.id} className={`signal-card ${signal.state}`}>
              <strong>{signal.label}</strong>
              <span>{signal.detail}</span>
            </div>
          ))}
        </div>
      </div>
      <div className="governance-gaps panel-card">
        <div className="panel-head"><h2>Gaps / Constraints</h2><span>{openGaps.length}</span></div>
        <div className="compact-list">
          {openGaps.length ? openGaps.slice(0, 6).map((gap) => (
            <div key={gap.id} className="compact-row warn">
              <strong>{gap.title || gap.id}</strong>
              <span>{gap.description || gap.reason || "计划执行前需要收敛"}</span>
            </div>
          )) : <p className="empty-copy">没有开放缺口。计划可进入批准或执行路径。</p>}
        </div>
      </div>
    </section>
  );
}

function RuntimeBoard({ payload, jobs }) {
  const nodes = flattenPlan(payload?.plan_tree).slice(0, 8);
  const active = jobs.filter((job) => ["running", "working", "pending"].includes(String(job.status)));
  const failed = jobs.filter((job) => String(job.status) === "failed");
  const terminal = jobs.filter((job) => terminalStatuses.has(String(job.status)));
  return (
    <section className="runtime-board">
      <div className="runtime-summary panel-card">
        <div className="panel-head"><h2>Provider Runtime Control</h2><span>{jobs.length}</span></div>
        <div className="runtime-metrics">
          <MetricCard label="Active" value={active.length} detail="运行 / 排队" tone="execute" />
          <MetricCard label="Failed" value={failed.length} detail="需要 inspect / rescue" tone="approval" />
          <MetricCard label="Terminal" value={terminal.length} detail="可进入证据复盘" tone="evidence" />
        </div>
        <ProviderJobStrip jobs={active.length ? active : jobs} />
      </div>
      <div className="panel-card">
        <div className="panel-head"><h2>Work Units</h2><span>{nodes.length}</span></div>
        <div className="work-unit-grid">
          {nodes.length ? nodes.map((node) => (
            <div key={node.id} className={`work-unit ${node.state || "planned"}`}>
              <span>{node.kind || "unit"}</span>
              <strong>{node.label || node.id}</strong>
              <small>{statusLabel(node.status || node.state)}</small>
            </div>
          )) : <p className="empty-copy">计划生成后会在这里展开 work units。</p>}
        </div>
      </div>
    </section>
  );
}

function EvidenceBoard({ payload, jobs }) {
  const events = payload?.events || [];
  const session = payload?.session || {};
  const pending = buildPendingItems(payload, jobs);
  return (
    <section className="evidence-board">
      <div className="panel-card decision-inbox">
        <div className="panel-head"><h2>Decision Inbox</h2><span>{pending.length}</span></div>
        <PendingList items={pending} />
      </div>
      <div className="panel-card compliance-card">
        <span className="eyebrow">Compliance Gate</span>
        <h2>计划收尾才跑完整验证</h2>
        <p>阶段中只做 targeted build / smoke；总计划收尾执行 pytest 与 team check-compliance，保持长周期工作不断流。</p>
        <code>PYTHONPATH=src python -m agent_orchestrator.cli team check-compliance</code>
      </div>
      <div className="panel-card evidence-events">
        <div className="panel-head"><h2>Evidence Events</h2><span>{events.length}</span></div>
        <div className="timeline-list">
          {events.length ? [...events].reverse().slice(0, 8).map((event) => (
            <div key={event.id || event.message} className="timeline-item system">
              <span>{event.type || "event"}</span>
              <p>{event.message || "状态已更新"}</p>
            </div>
          )) : <p className="empty-copy">暂无事件。执行与审查开始后，这里会形成证据线。</p>}
        </div>
      </div>
      <div className="panel-card">
        <h2>Current Session Archive Key</h2>
        <p className="soft-copy">{session.id || "选择 session 后显示 plan / run / evidence 关联键。"}</p>
      </div>
    </section>
  );
}

function GovernanceStream({ transcript, setActiveNav }) {
  return (
    <section className="panel-card stream-card">
      <div className="panel-head">
        <div><span className="eyebrow">Governance Stream</span><h2>计划对话与审查记录</h2></div>
        <button onClick={() => setActiveNav("governance")}>聚焦治理</button>
      </div>
      <div className="timeline-list">
        {transcript.map((item) => <MessageBubble key={item.id} item={item} />)}
      </div>
    </section>
  );
}

function MessageBubble({ item }) {
  return (
    <article className={`timeline-item ${item.side}`}>
      <div className="message-meta">
        <strong>{item.role}</strong>
        <span>{item.type}</span>
      </div>
      <p>{item.content}</p>
    </article>
  );
}

function PendingList({ items }) {
  return (
    <div className="compact-list">
      {items.length ? items.map((item) => (
        <div key={item.id} className={`compact-row ${item.tone || ""}`}>
          <strong>{item.title}</strong>
          <span>{item.detail}</span>
        </div>
      )) : <p className="empty-copy">暂无人工待办。系统会在需要批准、修订或恢复时把事项推到这里。</p>}
    </div>
  );
}

function ProviderJobStrip({ jobs }) {
  const visible = jobs.slice(0, 6);
  return (
    <div className="provider-strip">
      {visible.length ? visible.map((job) => (
        <div key={job.id} className="provider-card">
          <span className={`dot ${job.status || "idle"}`} />
          <strong>{statusLabel(job.provider)} · {job.kind}</strong>
          <small>{statusLabel(job.status)} · {job.terminal_ref || job.id}</small>
        </div>
      )) : <p className="empty-copy">暂无 provider job。执行计划后，这里会显示 Codex / Claude / Mock runtime 活动。</p>}
    </div>
  );
}

function Fact({ label, value }) {
  return <div className="fact"><span>{label}</span><strong>{value}</strong></div>;
}

function buildTranscript(payload) {
  if (!payload) return [];
  const session = payload.session || {};
  const brief = session.structured_brief || {};
  const messages = payload.messages?.items || [];
  const events = payload.events || [];
  const items = [
    { id: "user-requirement", side: "user", role: "你", type: "需求", content: session.requirement || "新任务" },
    { id: "lead-plan", side: "agent", role: "计划主控", type: "第一版计划", content: formatPlanBrief(brief, session) },
  ];
  for (const message of [...messages].reverse()) {
    items.push({
      id: message.id || `${message.from_role}-${message.content}`,
      side: roleSide(message.from_role || message.to_role),
      role: roleLabel(message.from_role || message.to_role),
      type: messageTypeLabel(message.message_type),
      content: message.content || JSON.stringify(message.payload || {}),
    });
  }
  for (const event of [...events].reverse().slice(-5)) {
    items.push({ id: event.id || event.message, side: "system", role: "系统", type: event.type || "event", content: event.message || "状态已更新" });
  }
  return items.filter((item) => item.content);
}

function formatPlanBrief(brief, session) {
  const subtasks = Array.isArray(brief.subtasks) ? brief.subtasks : [];
  const lines = [];
  lines.push(brief.goal || session.requirement || "等待计划目标");
  if (brief.context) lines.push(`背景：${brief.context}`);
  if (subtasks.length) lines.push(`子任务：${subtasks.map((item, index) => `${index + 1}. ${item.title || item.id}`).join("；")}`);
  const gaps = session.gaps || [];
  if (gaps.length) lines.push(`待确认缺口：${gaps.map((gap) => gap.title || gap.id).join("；")}`);
  return lines.join("\n");
}

function buildWorkbenchMetrics(sessions, jobs, payload) {
  const session = payload?.session || {};
  const openGaps = (session.gaps || []).filter((gap) => gap.status !== "closed").length;
  const governanceOpen = sessions.filter((item) => ["needs_revision", "awaiting_human_confirmation", "draft_ready", "in_review"].includes(String(item.status))).length + openGaps;
  return {
    sessions: sessions.length,
    governanceOpen,
    activeJobs: jobs.filter((job) => ["running", "working", "pending"].includes(String(job.status))).length,
    completedJobs: jobs.filter((job) => String(job.status) === "completed").length,
  };
}

function buildPendingItems(payload, jobs) {
  const session = payload?.session || {};
  const actions = payload?.actions || [];
  const items = [];
  for (const action of actions.filter((action) => action.enabled).slice(0, 4)) {
    items.push({ id: `action-${action.id}`, title: action.label || action.id, detail: "当前 session 可执行动作", tone: action.id === "approve" || action.id === "execute" ? "approval" : "" });
  }
  for (const gap of (session.gaps || []).filter((gap) => gap.status !== "closed").slice(0, 3)) {
    items.push({ id: `gap-${gap.id}`, title: gap.title || gap.id, detail: "计划治理缺口需要处理", tone: "warn" });
  }
  const failedJobs = jobs.filter((job) => String(job.status) === "failed").slice(0, 2);
  for (const job of failedJobs) items.push({ id: `job-${job.id}`, title: `${statusLabel(job.provider)} ${job.kind}`, detail: "Provider job 失败，可能需要 rescue / inspect", tone: "bad" });
  return items;
}


function buildReviewSignals(payload) {
  const session = payload?.session || {};
  const status = String(session.status || "idle");
  const messages = payload?.messages?.items || [];
  const hasReviewer = messages.some((message) => ["reviewer", "lead"].includes(String(message.from_role || message.to_role)));
  const hasAdversarial = messages.some((message) => ["adversarial_reviewer", "skeptic"].includes(String(message.from_role || message.to_role)));
  return [
    { id: "draft", label: "Plan Draft", detail: status === "idle" ? "等待任务输入" : "计划主控已建立现场", state: status === "idle" ? "pending" : "done" },
    { id: "review", label: "Reviewer", detail: hasReviewer || ["in_review", "adversarial_review", "approved_for_execution", "executing", "accepted"].includes(status) ? "审查信号已进入记录" : "等待审查", state: hasReviewer ? "done" : ["in_review", "adversarial_review"].includes(status) ? "active" : "pending" },
    { id: "adversarial", label: "Adversarial", detail: hasAdversarial ? "对抗审查已记录" : "需要时进入反方质询", state: hasAdversarial ? "done" : status === "adversarial_review" ? "active" : "pending" },
    { id: "approval", label: "Human Gate", detail: ["approved_for_execution", "executing", "accepted"].includes(status) ? "执行边界已批准" : "等待人工确认", state: ["approved_for_execution", "executing", "accepted"].includes(status) ? "done" : ["awaiting_human_confirmation", "draft_ready"].includes(status) ? "active" : "pending" },
  ];
}

function buildTopologyStages(payload) {
  const session = payload?.session || {};
  const status = String(session.status || "idle");
  const reviewDone = ["approved_for_execution", "executing", "accepted", "needs_followup"].includes(status);
  const executing = status === "executing";
  const accepted = status === "accepted";
  return [
    { id: "plan", label: "Plan", detail: "任务进入计划治理", state: status === "idle" ? "pending" : "done" },
    { id: "review", label: "Review", detail: "双模型/对抗审查", state: reviewDone ? "done" : ["in_review", "adversarial_review", "needs_revision"].includes(status) ? "active" : "pending" },
    { id: "approval", label: "Approval", detail: "人工确认执行边界", state: reviewDone ? "done" : ["awaiting_human_confirmation", "draft_ready"].includes(status) ? "active" : "pending" },
    { id: "execute", label: "Execute", detail: "Provider Runtime 执行", state: accepted ? "done" : executing ? "active" : "pending" },
    { id: "evidence", label: "Evidence", detail: "事件、日志、合规证据", state: accepted ? "done" : "pending" },
  ];
}

function fallbackRoleCards(payload) {
  if (!payload) return [];
  const session = payload.session || {};
  return [
    { id: "lead", role_label: "计划主控", status: session.status || "idle", summary: "生成计划、收敛缺口、准备执行合约" },
    { id: "reviewer", role_label: "计划审核", status: ["in_review", "adversarial_review"].includes(String(session.status)) ? "running" : "idle", summary: "检查计划是否可执行、可验证" },
    { id: "worker", role_label: "执行 Agent", status: session.status === "executing" ? "running" : "idle", summary: "等待批准后的 provider runtime 执行" },
  ];
}

function flattenPlan(tree) {
  if (!tree) return [];
  return [tree, ...(tree.children || [])];
}

function roleLabel(role) {
  const map = {
    lead: "计划主控",
    reviewer: "计划审核",
    adversarial_reviewer: "对抗审核",
    worker: "执行 Agent",
    rescuer: "救援 Agent",
    summarizer: "证据汇总",
    proponent: "正方构想",
    skeptic: "反方质询",
  };
  return map[role] || role || "Agent";
}

function roleSide(role) {
  if (role === "adversarial_reviewer" || role === "skeptic") return "reviewer";
  if (role === "worker" || role === "rescuer") return "worker";
  return "agent";
}

function messageTypeLabel(type) {
  const map = { review_request: "请求", review_result: "审查", handoff: "交接", note: "消息" };
  return map[type] || type || "消息";
}

function composerPlaceholder(selectedSessionId, payload, selectedJob) {
  if (!selectedSessionId) return "输入任务目标：先进入计划治理，再让执行策略选择拓扑和 Provider Runtime...";
  if (canRevise(payload)) return "回复计划主控：补充约束、确认缺口，或说明要怎样修订第一版计划...";
  if (canLeadChat(payload)) return "继续和计划主控聊：补充约束、调整范围，确认第一版计划前都可以反复说明...";
  if (selectedJob && isLiveJob(selectedJob)) return "向当前运行中的 Provider Job 发送输入...";
  return "当前阶段以查看和执行动作为主。选择运行中的 Provider Job 后可发送输入。";
}

function canRevise(payload) {
  const session = payload?.session || {};
  return ["needs_revision", "awaiting_human_confirmation"].includes(String(session.status)) && openGapIds(session).length > 0;
}

function canLeadChat(payload) {
  const session = payload?.session || {};
  return ["intake_chat", "draft_ready", "awaiting_human_confirmation", "needs_revision"].includes(String(session.status));
}

function openGapIds(session) {
  return (session.gaps || []).filter((gap) => gap.status !== "closed" && gap.required !== false).map((gap) => gap.id).filter(Boolean);
}

function isLiveJob(job) {
  return Boolean(job && !terminalStatuses.has(String(job.status || "")));
}

createRoot(document.getElementById("root")).render(<App />);

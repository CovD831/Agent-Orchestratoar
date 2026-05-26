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
  completed: "完成",
  failed: "失败",
  cancelled: "取消",
  codex: "Codex",
  claude: "Claude",
  mock: "Mock",
  decision_core: "Decision Core",
};

const statusLabel = (value) => labels[value] || value || "未知";

function App() {
  const [sessions, setSessions] = useState([]);
  const [selectedSessionId, setSelectedSessionId] = useState(null);
  const [sessionPayload, setSessionPayload] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [selectedJobId, setSelectedJobId] = useState(null);
  const [jobLog, setJobLog] = useState("选择一个任务查看日志。");
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [inspectorTab, setInspectorTab] = useState("plan");

  const session = sessionPayload?.session || {};
  const brief = session.structured_brief || {};
  const nextAction = sessionPayload?.next_action || {};
  const actions = sessionPayload?.actions || [];
  const messages = sessionPayload?.messages?.items || [];
  const events = sessionPayload?.events || [];
  const selectedJob = useMemo(() => jobs.find((job) => job.id === selectedJobId), [jobs, selectedJobId]);
  const sessionTitle = brief.goal || session.requirement || selectedSessionId || "描述你想让团队完成的事";
  const transcript = useMemo(() => buildTranscript(sessionPayload), [sessionPayload]);
  const activeJobs = useMemo(
    () => jobs.filter((job) => ["running", "working", "pending"].includes(String(job.status))).slice(0, 4),
    [jobs],
  );

  const refreshSessions = async (autoselect = true) => {
    const payload = await api("/api/sessions");
    const nextSessions = payload.sessions || [];
    setSessions(nextSessions);
    if (autoselect && !selectedSessionId && nextSessions[0]) {
      setSelectedSessionId(nextSessions[0].id);
    }
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
        setJobLog(log.log || "这个任务暂无日志。");
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
        setError("当前会话已经进入只读检查阶段。可以执行主操作，或选择一个运行中的终端任务后发送输入。");
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
    const text = draft.trim() || window.prompt("发送给当前终端的输入");
    if (!selectedJobId || !text) return;
    await api(`/api/jobs/${selectedJobId}/terminal/input`, { method: "POST", body: JSON.stringify({ message: text }) });
    setDraft("");
    setJobLog((current) => `${current}\n\n> ${text}`);
  };

  const enabledActions = actions.filter((action) => action.enabled);

  return (
    <main className="workspace">
      <aside className="session-rail">
        <div className="rail-brand">
          <strong>Agent Team</strong>
          <button onClick={() => Promise.all([refreshSessions(false), refreshJobs()])}>刷新</button>
        </div>
        <button className={!selectedSessionId ? "session-chip active" : "session-chip"} onClick={() => setSelectedSessionId(null)}>
          <strong>新对话</strong>
          <span>从任务描述开始</span>
        </button>
        <div className="session-list">
          {sessions.length ? sessions.map((item) => (
            <button key={item.id} className={item.id === selectedSessionId ? "session-chip active" : "session-chip"} onClick={() => setSelectedSessionId(item.id)}>
              <strong>{item.goal || item.requirement || item.id}</strong>
              <span>{statusLabel(item.status)} · {item.phase || item.primary_action || "plan"}</span>
            </button>
          )) : <p className="muted">还没有历史会话。</p>}
        </div>
      </aside>

      <section className="conversation">
        <header className="conversation-head">
          <div>
            <span className="eyebrow">{selectedSessionId ? "当前计划" : "新任务"}</span>
            <h1>{sessionTitle}</h1>
          </div>
          <span className={`badge ${session.status || "idle"}`}>{statusLabel(session.status || "idle")}</span>
        </header>

        <section className="chat-scroll" aria-live="polite">
          {!selectedSessionId ? <Welcome /> : transcript.map((item) => <MessageBubble key={item.id} item={item} />)}
        </section>

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
            <button className="primary" disabled={busy || !draft.trim()} type="submit">{selectedSessionId ? "发送" : "开始"}</button>
          </div>
          <div className="quick-actions">
            {enabledActions.length ? enabledActions.slice(0, 4).map((action) => (
              <button key={action.id} type="button" onClick={() => runAction(action.id)} disabled={busy || action.id === "lead_chat" || action.id === "revise"}>
                {action.label}
              </button>
            )) : <span>计划确认后，主要操作会出现在这里。</span>}
          </div>
        </form>
      </section>

      <aside className="inspector">
        <div className="tabbar">
          {["plan", "agents", "jobs"].map((tab) => (
            <button key={tab} className={inspectorTab === tab ? "active" : ""} onClick={() => setInspectorTab(tab)}>
              {tab === "plan" ? "计划" : tab === "agents" ? "Agent" : "日志"}
            </button>
          ))}
        </div>
        {inspectorTab === "plan" ? (
          <PlanInspector payload={sessionPayload} nextAction={nextAction} />
        ) : inspectorTab === "agents" ? (
          <AgentInspector payload={sessionPayload} activeJobs={activeJobs} />
        ) : (
          <JobInspector
            jobs={jobs}
            selectedJob={selectedJob}
            selectedJobId={selectedJobId}
            setSelectedJobId={setSelectedJobId}
            jobLog={jobLog}
            sendJobMessage={sendJobMessage}
          />
        )}
      </aside>

      {error ? <div className="error">{error}</div> : null}
    </main>
  );
}

function Welcome() {
  return (
    <div className="welcome">
      <span className="eyebrow">Planning chat</span>
      <h2>先把第一版计划聊清楚。</h2>
      <p>输入任务后，计划主控会生成第一版计划，审核 agent 的意见会按对话流展示。第一版先只保留你能读懂的主线，复杂的作战室以后再做成摘要视图。</p>
    </div>
  );
}

function MessageBubble({ item }) {
  return (
    <article className={`message ${item.side}`}>
      <div className="message-meta">
        <strong>{item.role}</strong>
        <span>{item.type}</span>
      </div>
      <p>{item.content}</p>
    </article>
  );
}

function PlanInspector({ payload, nextAction }) {
  const session = payload?.session || {};
  const brief = session.structured_brief || {};
  const nodes = flattenPlan(payload?.plan_tree).slice(0, 8);
  return (
    <div className="inspector-stack">
      <section className="panel next">
        <span className="eyebrow">下一步</span>
        <h2>{nextAction.primary_label || "等待计划"}</h2>
        <p>{nextAction.primary_reason || "创建会话后，这里会显示当前建议动作。"}</p>
      </section>
      <section className="panel">
        <h2>计划轮廓</h2>
        <div className="outline">
          <div><span>目标</span><strong>{brief.goal || session.requirement || "未开始"}</strong></div>
          <div><span>拓扑</span><strong>{statusLabel(brief.topology_recommendation?.topology || session.status_summary?.selected_topology)}</strong></div>
          <div><span>子任务</span><strong>{(brief.subtasks || []).length} 个</strong></div>
          <div><span>缺口</span><strong>{(session.gaps || []).length} 个</strong></div>
        </div>
      </section>
      <section className="panel">
        <h2>计划节点</h2>
        <div className="node-list">
          {nodes.length ? nodes.map((node) => (
            <div key={node.id} className={`node ${node.state || "planned"}`}>
              <span>{node.kind}</span>
              <strong>{node.label || node.id}</strong>
              <small>{statusLabel(node.status || node.state)}</small>
            </div>
          )) : <p className="muted">计划生成后会在这里展开。</p>}
        </div>
      </section>
    </div>
  );
}

function AgentInspector({ payload, activeJobs }) {
  const groups = payload?.role_groups || [];
  const cards = groups.flatMap((group) => group.cards || []);
  return (
    <div className="inspector-stack">
      <section className="panel">
        <h2>Agent 摘要</h2>
        <div className="agent-list">
          {cards.length ? cards.map((card) => (
            <div key={card.id || `${card.role}-${card.kind}`} className="agent-row">
              <span className={`dot ${card.status || "idle"}`} />
              <div>
                <strong>{card.role_label || card.kind || "Agent"}</strong>
                <p>{card.latest_message_summary || card.current_action || card.summary || "暂无活动"}</p>
              </div>
            </div>
          )) : <p className="muted">暂无可见 agent 活动。</p>}
        </div>
      </section>
      <section className="panel">
        <h2>运行中</h2>
        <div className="agent-list">
          {activeJobs.length ? activeJobs.map((job) => (
            <div key={job.id} className="agent-row">
              <span className={`dot ${job.status || "idle"}`} />
              <div>
                <strong>{job.provider} · {job.kind}</strong>
                <p>{job.last_log_excerpt || job.output_preview || job.summary || job.id}</p>
              </div>
            </div>
          )) : <p className="muted">没有运行中的 job。</p>}
        </div>
      </section>
    </div>
  );
}

function JobInspector({ jobs, selectedJob, selectedJobId, setSelectedJobId, jobLog, sendJobMessage }) {
  return (
    <div className="inspector-stack">
      <section className="panel">
        <h2>最近任务</h2>
        <div className="job-list">
          {jobs.length ? jobs.slice(0, 12).map((job) => (
            <button key={job.id} className={job.id === selectedJobId ? "active" : ""} onClick={() => setSelectedJobId(job.id)}>
              <strong>{job.provider} · {job.kind}</strong>
              <span>{statusLabel(job.status)} · {job.terminal_ref || job.id}</span>
            </button>
          )) : <p className="muted">暂无任务。</p>}
        </div>
      </section>
      <section className="panel log-panel">
        <div className="panel-head">
          <h2>{selectedJob ? `${selectedJob.provider} · ${selectedJob.kind}` : "任务日志"}</h2>
          <button disabled={!selectedJob || !isLiveJob(selectedJob)} onClick={sendJobMessage}>发送输入</button>
        </div>
        <pre className="log">{jobLog}</pre>
      </section>
    </div>
  );
}

function buildTranscript(payload) {
  if (!payload) return [];
  const session = payload.session || {};
  const brief = session.structured_brief || {};
  const messages = payload.messages?.items || [];
  const events = payload.events || [];
  const items = [
    {
      id: "user-requirement",
      side: "user",
      role: "你",
      type: "需求",
      content: session.requirement || "新任务",
    },
    {
      id: "lead-plan",
      side: "agent",
      role: "计划主控",
      type: "第一版计划",
      content: formatPlanBrief(brief, session),
    },
  ];
  for (const message of [...messages].reverse()) {
    items.push({
      id: message.id || `${message.from_role}-${message.content}`,
      side: "agent",
      role: roleLabel(message.from_role || message.to_role),
      type: messageTypeLabel(message.message_type),
      content: message.content || JSON.stringify(message.payload || {}),
    });
  }
  for (const event of [...events].reverse().slice(-4)) {
    items.push({
      id: event.id || event.message,
      side: "system",
      role: "系统",
      type: event.type || "event",
      content: event.message || "状态已更新",
    });
  }
  return items.filter((item) => item.content);
}

function formatPlanBrief(brief, session) {
  const subtasks = Array.isArray(brief.subtasks) ? brief.subtasks : [];
  const lines = [];
  lines.push(brief.goal || session.requirement || "等待计划目标");
  if (brief.context) lines.push(`背景：${brief.context}`);
  if (subtasks.length) {
    lines.push(`子任务：${subtasks.map((item, index) => `${index + 1}. ${item.title || item.id}`).join("；")}`);
  }
  const gaps = session.gaps || [];
  if (gaps.length) lines.push(`待确认缺口：${gaps.map((gap) => gap.title || gap.id).join("；")}`);
  return lines.join("\n");
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
    proponent: "正方构想",
    skeptic: "反方质询",
  };
  return map[role] || role || "Agent";
}

function messageTypeLabel(type) {
  const map = {
    review_request: "请求",
    review_result: "回复",
    handoff: "交接",
    note: "消息",
  };
  return map[type] || type || "消息";
}

function composerPlaceholder(selectedSessionId, payload, selectedJob) {
  if (!selectedSessionId) return "输入任务目标，例如：帮我把这个仓库的 UI 改成更像 Codex 的计划对话流...";
  if (canRevise(payload)) return "回复计划主控：补充约束、确认缺口，或说明要怎样修订第一版计划...";
  if (canLeadChat(payload)) return "继续和计划主控聊：补充约束、调整范围，确认第一版计划前都可以反复说明...";
  if (selectedJob && isLiveJob(selectedJob)) return "向当前运行中的终端任务发送输入...";
  return "当前阶段以查看和执行动作为主。选择运行中的任务后可发送终端输入。";
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
  return (session.gaps || [])
    .filter((gap) => gap.status !== "closed" && gap.required !== false)
    .map((gap) => gap.id)
    .filter(Boolean);
}

function isLiveJob(job) {
  return Boolean(job && !["completed", "failed", "cancelled"].includes(String(job.status || "")));
}

createRoot(document.getElementById("root")).render(<App />);

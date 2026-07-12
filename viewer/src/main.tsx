/**
 * Grok Agent Observer — local read-only viewer.
 * Continuous conversation stream (Codex/Claude style), not a card grid.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import {
  Activity,
  Bot,
  Braces,
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  FileCode2,
  GitCompareArrows,
  MessageSquare,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Search,
  ServerOff,
  Sun,
  Terminal,
  Trash2,
  Wrench,
  X,
  Zap,
} from "lucide-react";
import "./styles.css";
import { HighlightSnippet, type SearchMatch } from "./searchHighlight";

import {
  buildStream,
  cleanText,
  parsePayload,
  type Event,
  type StreamItem,
  type ToolStep,
  type Turn,
} from "./streamModel";

/* ─── types ─────────────────────────────────────────────────────────────── */

type Task = { thread_id: string; title: string; cwd: string; updated_at: string };
type Agent = {
  id: string;
  thread_id: string;
  name: string;
  cwd: string;
  status: string;
  revision: number;
  signoff_verdict?: string;
  created_at: string;
  updated_at: string;
  final_text?: string;
  error?: string;
  grok_session_id?: string;
  signoff_summary?: string;
  verification?: string;
};
type Change = {
  id: number;
  path: string;
  kind: string;
  preexisting: number;
  diff_artifact?: string;
};
type SearchHit = {
  agent_id: string;
  kind: string;
  snippet: string;
  matches?: SearchMatch[];
};

type ThemeMode = "system" | "light" | "dark";
type MainTab = "conversation" | "changes" | "details";

/* ─── constants ─────────────────────────────────────────────────────────── */

const STATUS_LABELS: Record<string, string> = {
  queued: "排队",
  running: "运行中",
  completed: "待签收",
  failed: "失败",
  cancelled: "已取消",
  accepted: "已接受",
  partial: "部分采用",
  rejected: "已拒绝",
};

function parseHash(): string {
  const m = location.hash.match(/agents\/([^/?#]+)/);
  return m?.[1] || "";
}

function formatTime(value?: string): string {
  if (!value) return "—";
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(new Date(value));
  } catch {
    return value;
  }
}

function formatDateTime(value?: string): string {
  if (!value) return "—";
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(new Date(value));
  } catch {
    return value;
  }
}

function prefersReducedMotion(): boolean {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/* ─── small UI atoms ────────────────────────────────────────────────────── */

function CopyButton({ value, label = "复制" }: { value: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="icon-btn"
      title={label}
      aria-label={label}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setDone(true);
          window.setTimeout(() => setDone(false), 1200);
        } catch {
          /* ignore */
        }
      }}
    >
      {done ? <Check size={14} aria-hidden /> : <Copy size={14} aria-hidden />}
    </button>
  );
}

function ScrollBody({
  children,
  className = "",
  maxHeight = "min(48vh, 420px)",
}: {
  children: React.ReactNode;
  className?: string;
  maxHeight?: string;
}) {
  return (
    <div className={`scroll-body ${className}`} style={{ maxHeight }}>
      {children}
    </div>
  );
}

function Markdown({ children }: { children: string }) {
  const text = cleanText(children);
  if (!text.trim()) return null;
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSanitize]}
        components={{
          a({ href, children: linkChildren }) {
            return (
              <a href={href} target="_blank" rel="noreferrer noopener">
                {linkChildren}
              </a>
            );
          },
          code({ className, children: codeChildren }) {
            const value = String(codeChildren).replace(/\n$/, "");
            const isBlock = Boolean(className) || value.includes("\n");
            if (!isBlock) return <code className="inline-code">{codeChildren}</code>;
            const lang = className?.replace("language-", "") || "code";
            return (
              <div className="code-block">
                <div className="code-head">
                  <span>{lang}</span>
                  <CopyButton value={value} />
                </div>
                <ScrollBody maxHeight="min(50vh, 480px)">
                  <pre>
                    <code>{value}</code>
                  </pre>
                </ScrollBody>
              </div>
            );
          },
          table({ children: tableChildren }) {
            return (
              <div className="table-wrap">
                <table>{tableChildren}</table>
              </div>
            );
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

function RawPayload({ event }: { event: Event }) {
  const raw = useMemo(() => {
    const data = parsePayload(event);
    if (data == null) return cleanText(event.summary || "");
    if (typeof data === "string") return cleanText(data);
    try {
      return cleanText(JSON.stringify(data, null, 2));
    } catch {
      return cleanText(String(data));
    }
  }, [event]);

  return (
    <div className="raw-block">
      <div className="code-head">
        <span>原始事件 · {event.type}</span>
        <CopyButton value={raw} />
      </div>
      <ScrollBody>
        <pre>{raw}</pre>
      </ScrollBody>
    </div>
  );
}

function formatJson(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return cleanText(value);
  try {
    return cleanText(JSON.stringify(value, null, 2));
  } catch {
    return cleanText(String(value));
  }
}

/* ─── stream row components ─────────────────────────────────────────────── */

function UserMessage({ text, time }: { text: string; time: string }) {
  return (
    <article className="msg msg-user">
      <header className="msg-meta">
        <span className="role">Codex → Grok</span>
        <time dateTime={time}>{formatTime(time)}</time>
      </header>
      <Markdown>{text}</Markdown>
    </article>
  );
}

function AssistantMessage({ text, time }: { text: string; time: string }) {
  return (
    <article className="msg msg-assistant">
      <header className="msg-meta">
        <span className="role">
          <Bot size={13} aria-hidden /> Grok
        </span>
        <time dateTime={time}>{formatTime(time)}</time>
      </header>
      <Markdown>{text}</Markdown>
    </article>
  );
}

function ThoughtRow({ text, time }: { text: string; time: string }) {
  return (
    <details className="row thought-row">
      <summary>
        <Zap size={13} className="row-icon thought-icon" aria-hidden />
        <span className="row-title">思考</span>
        <span className="row-sub">{formatTime(time)}</span>
        <ChevronRight size={14} className="row-chevron" aria-hidden />
      </summary>
      <div className="row-body">
        <ScrollBody maxHeight="min(40vh, 360px)">
          <Markdown>{text}</Markdown>
        </ScrollBody>
      </div>
    </details>
  );
}

function ToolStepRow({ step }: { step: ToolStep }) {
  const inputText = formatJson(step.input);
  const outputText = step.output || "";
  const statusLabel =
    step.status === "done" || step.status === "completed" || step.status === "success"
      ? "完成"
      : step.status === "failed" || step.status === "error"
        ? "失败"
        : step.status === "info"
          ? ""
          : step.status;

  return (
    <details className="row tool-step">
      <summary>
        <Wrench size={12} className="row-icon tool-icon" aria-hidden />
        <span className="row-title" title={step.title}>
          {step.title || step.name}
        </span>
        {statusLabel && <span className="row-badge">{statusLabel}</span>}
        {step.startedAt && <span className="row-sub">{formatTime(step.startedAt)}</span>}
        <ChevronRight size={13} className="row-chevron" aria-hidden />
      </summary>
      <div className="row-body tool-step-body">
        {inputText && (
          <div className="tool-pane">
            <div className="code-head">
              <span>输入</span>
              <CopyButton value={inputText} />
            </div>
            <ScrollBody maxHeight="min(36vh, 280px)">
              <pre>{inputText}</pre>
            </ScrollBody>
          </div>
        )}
        {outputText && (
          <div className="tool-pane">
            <div className="code-head">
              <span>输出</span>
              <CopyButton value={outputText} />
            </div>
            <ScrollBody maxHeight="min(40vh, 360px)">
              <pre>{outputText}</pre>
            </ScrollBody>
          </div>
        )}
        {!inputText && !outputText && step.events[0] && <RawPayload event={step.events[0]} />}
        {step.events.length > 1 && (
          <details className="raw-events">
            <summary>相关事件 ({step.events.length})</summary>
            <div className="raw-events-list">
              {step.events.map((ev) => (
                <RawPayload key={ev.id} event={ev} />
              ))}
            </div>
          </details>
        )}
      </div>
    </details>
  );
}

function ToolchainRow({ steps, events }: { steps: ToolStep[]; events: Event[] }) {
  const label =
    steps.length === 1
      ? steps[0].title || steps[0].name || "工具"
      : `工具链 · ${steps.length} 步`;
  const first = events[0]?.created_at;
  const names = steps
    .slice(0, 4)
    .map((s) => s.title || s.name)
    .join(" · ");

  return (
    <details className="row toolchain-row">
      <summary>
        <Terminal size={13} className="row-icon tool-icon" aria-hidden />
        <span className="row-title">{label}</span>
        {steps.length > 1 && (
          <span className="row-sub ellipsis" title={names}>
            {names}
          </span>
        )}
        {first && <span className="row-sub">{formatTime(first)}</span>}
        <ChevronRight size={14} className="row-chevron" aria-hidden />
      </summary>
      <div className="row-body toolchain-body">
        <div className="toolchain-list">
          {steps.map((step) => (
            <ToolStepRow key={step.key} step={step} />
          ))}
        </div>
      </div>
    </details>
  );
}

function DiagnosticGroup({ events }: { events: Event[] }) {
  const first = events[0];
  const preview = cleanText(first?.summary || "diagnostic").slice(0, 180);

  return (
    <details className="row diagnostic-group">
      <summary>
        <Activity size={13} className="row-icon diagnostic-icon" aria-hidden />
        <span className="row-title">诊断 · {events.length} 条</span>
        <span className="row-sub ellipsis" title={preview}>{preview}</span>
        {first && <span className="row-sub">{formatTime(first.created_at)}</span>}
        <ChevronRight size={14} className="row-chevron" aria-hidden />
      </summary>
      <div className="row-body diagnostic-body">
        {events.map((event) => (
          <MetaRow
            key={event.id}
            label={cleanText(event.summary || event.type).slice(0, 240)}
            time={event.created_at}
            event={event}
          />
        ))}
      </div>
    </details>
  );
}

function StatusRow({
  label,
  time,
  tone,
  event,
}: {
  label: string;
  time: string;
  tone: "ok" | "warn" | "err" | "info";
  event: Event;
}) {
  return (
    <details className={`row status-row tone-${tone}`}>
      <summary>
        <Activity size={13} className="row-icon" aria-hidden />
        <span className="row-title">{label}</span>
        <span className="row-sub">{formatTime(time)}</span>
        <ChevronRight size={14} className="row-chevron" aria-hidden />
      </summary>
      <div className="row-body">
        <RawPayload event={event} />
      </div>
    </details>
  );
}

function MetaRow({ label, time, event }: { label: string; time: string; event: Event }) {
  // Diagnostics are noisy — keep ultra-compact, collapsed.
  return (
    <details className="row meta-row">
      <summary>
        <span className="row-title muted">{event.type}</span>
        <span className="row-sub ellipsis" title={label}>
          {label}
        </span>
        <span className="row-sub">{formatTime(time)}</span>
        <ChevronRight size={13} className="row-chevron" aria-hidden />
      </summary>
      <div className="row-body">
        <RawPayload event={event} />
      </div>
    </details>
  );
}

function TurnSeparator({ turnNo, turnId }: { turnNo?: number; turnId: number | null }) {
  const label =
    turnNo != null ? `Turn ${turnNo}` : turnId != null ? `Turn #${turnId}` : "Turn";
  return (
    <div className="turn-sep" role="separator" aria-label={label} data-turn={turnNo ?? turnId ?? ""}>
      <i />
      <span>{label}</span>
      <i />
    </div>
  );
}

function StreamItemView({ item }: { item: StreamItem }) {
  switch (item.kind) {
    case "user":
      return <UserMessage text={item.text} time={item.event.created_at} />;
    case "text":
      return <AssistantMessage text={item.text} time={item.event.created_at} />;
    case "thought":
      return <ThoughtRow text={item.text} time={item.event.created_at} />;
    case "toolchain":
      return <ToolchainRow steps={item.steps} events={item.events} />;
    case "diagnostics":
      return <DiagnosticGroup events={item.events} />;
    case "status":
      return (
        <StatusRow
          label={item.label}
          time={item.event.created_at}
          tone={item.tone}
          event={item.event}
        />
      );
    case "meta":
      return <MetaRow label={item.label} time={item.event.created_at} event={item.event} />;
    case "turn_sep":
      return <TurnSeparator turnNo={item.turnNo} turnId={item.turnId} />;
  }
}

/* ─── panels ────────────────────────────────────────────────────────────── */

function ArtifactView({ path }: { path: string }) {
  const [value, setValue] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  const load = async () => {
    setLoading(true);
    setErr("");
    try {
      const res = await fetch(`/api/artifact?path=${encodeURIComponent(path)}`);
      const data = await res.json();
      if (data.error) setErr(data.error);
      else setValue(cleanText(data.content || ""));
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  };

  if (value) {
    return (
      <div className="code-block">
        <div className="code-head">
          <span>diff</span>
          <CopyButton value={value} />
        </div>
        <ScrollBody maxHeight="min(55vh, 520px)">
          <pre>
            <code>{value}</code>
          </pre>
        </ScrollBody>
      </div>
    );
  }
  return (
    <div className="artifact-actions">
      <button type="button" className="btn" onClick={load} disabled={loading}>
        {loading ? "加载中…" : "加载完整 diff"}
      </button>
      {err && <span className="err-text">{err}</span>}
    </div>
  );
}

function ChangesPanel({ changes }: { changes: Change[] }) {
  if (!changes.length) {
    return (
      <div className="empty">
        <GitCompareArrows size={22} aria-hidden />
        <p>尚未检测到文件变化</p>
      </div>
    );
  }
  return (
    <div className="panel">
      <div className="panel-head">
        <div>
          <span className="eyebrow">WORKTREE</span>
          <h2>文件变化</h2>
        </div>
        <span className="count-pill">{changes.length}</span>
      </div>
      <ul className="change-list">
        {changes.map((c) => (
          <li key={c.id}>
            <details className="row change-row">
              <summary>
                <FileCode2 size={13} className="row-icon" aria-hidden />
                <span className="row-title mono" title={c.path}>
                  {c.path}
                </span>
                <span className="row-badge">{c.kind}</span>
                {!!c.preexisting && <span className="row-sub">任务前已修改</span>}
                <ChevronRight size={14} className="row-chevron" aria-hidden />
              </summary>
              <div className="row-body">
                <p className="hint">完整 diff 已压缩保存；与工具事件共同归因。</p>
                {c.diff_artifact && <ArtifactView path={c.diff_artifact} />}
              </div>
            </details>
          </li>
        ))}
      </ul>
    </div>
  );
}

function DetailsPanel({ agent, turns }: { agent: Agent; turns: Turn[] }) {
  const prompt = turns[0]?.prompt || "";
  return (
    <div className="panel details-grid">
      <section>
        <div className="panel-head">
          <div>
            <span className="eyebrow">DELEGATION</span>
            <h2>委托提示词</h2>
          </div>
          <CopyButton value={prompt} />
        </div>
        <div className="surface">
          <Markdown>{prompt}</Markdown>
        </div>
      </section>
      <section>
        <div className="panel-head">
          <div>
            <span className="eyebrow">CODEX REVIEW</span>
            <h2>签收状态</h2>
          </div>
        </div>
        <div className="surface review">
          <strong>
            {STATUS_LABELS[agent.signoff_verdict || ""] || "等待 Codex 审查"}
          </strong>
          <p>
            {agent.signoff_summary ||
              "Grok 完成后，Codex 将在验证结果后记录签收。"}
          </p>
          {agent.verification && (
            <ScrollBody maxHeight="min(30vh, 240px)">
              <pre className="verify-pre">{cleanText(agent.verification)}</pre>
            </ScrollBody>
          )}
          {agent.error && (
            <ScrollBody maxHeight="min(24vh, 200px)">
              <pre className="err-pre">{cleanText(agent.error)}</pre>
            </ScrollBody>
          )}
        </div>
      </section>
      <section className="turns-section">
        <div className="panel-head">
          <div>
            <span className="eyebrow">TURNS</span>
            <h2>轮次</h2>
          </div>
        </div>
        <div className="turn-list">
          {turns.map((turn) => (
            <details key={turn.id} className="row">
              <summary>
                <MessageSquare size={13} className="row-icon" aria-hidden />
                <span className="row-title">Turn {turn.turn_no}</span>
                <span className="row-badge">
                  {STATUS_LABELS[turn.status] || turn.status}
                </span>
                <span className="row-sub">{formatTime(turn.created_at)}</span>
                <ChevronRight size={14} className="row-chevron" aria-hidden />
              </summary>
              <div className="row-body">
                <Markdown>{turn.prompt}</Markdown>
                {turn.result && (
                  <>
                    <div className="divider-label">结果</div>
                    <Markdown>{turn.result}</Markdown>
                  </>
                )}
              </div>
            </details>
          ))}
          {!turns.length && <p className="hint">暂无轮次</p>}
        </div>
      </section>
    </div>
  );
}

/* ─── dialogs ───────────────────────────────────────────────────────────── */

function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel,
  danger,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  body: string;
  confirmLabel: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (open) cancelRef.current?.focus();
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onCancel]);
  if (!open) return null;
  return (
    <div className="modal-root" role="presentation">
      <div className="modal-backdrop" onClick={onCancel} />
      <div
        className="modal"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="confirm-title"
        aria-describedby="confirm-body"
      >
        <h2 id="confirm-title">{title}</h2>
        <p id="confirm-body">{body}</p>
        <div className="modal-actions">
          <button type="button" className="btn" ref={cancelRef} onClick={onCancel}>
            取消
          </button>
          <button
            type="button"
            className={danger ? "btn btn-danger" : "btn btn-primary"}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

/* ─── app ───────────────────────────────────────────────────────────────── */

function App() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [selected, setSelected] = useState(parseHash);
  const [detail, setDetail] = useState<{
    agent: Agent;
    turns: Turn[];
    changes: Change[];
  } | null>(null);
  const [events, setEvents] = useState<Event[]>([]);
  const [tab, setTab] = useState<MainTab>("conversation");
  const [query, setQuery] = useState("");
  const [searchHits, setSearchHits] = useState<SearchHit[]>([]);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [theme, setTheme] = useState<ThemeMode>(() => {
    const saved = localStorage.getItem("grok-observer-theme") as ThemeMode | null;
    return saved || "system";
  });
  const [collapsedTasks, setCollapsedTasks] = useState<Record<string, boolean>>({});
  const [deleteTarget, setDeleteTarget] = useState<Agent | null>(null);
  const [deleteError, setDeleteError] = useState("");
  const [hasNew, setHasNew] = useState(false);
  const [bootError, setBootError] = useState("");

  const timelineRef = useRef<HTMLDivElement>(null);
  const autoFollowRef = useRef(true);
  const selectedRef = useRef(selected);
  selectedRef.current = selected;

  const stream = useMemo(
    () => buildStream(events, detail?.turns || []),
    [events, detail?.turns],
  );

  const bootstrap = useCallback(async () => {
    try {
      const res = await fetch("/api/bootstrap");
      if (!res.ok) throw new Error(`bootstrap ${res.status}`);
      const data = await res.json();
      setTasks(data.tasks || []);
      setAgents(data.agents || []);
      setBootError("");
      if (!selectedRef.current && data.agents?.length) {
        const first = data.agents[0].id as string;
        location.hash = `#/agents/${first}`;
        setSelected(first);
      }
    } catch (e) {
      setBootError(String(e));
    }
  }, []);

  useEffect(() => {
    void bootstrap();
    const timer = window.setInterval(() => void bootstrap(), 5000);
    return () => window.clearInterval(timer);
  }, [bootstrap]);

  useEffect(() => {
    const onHash = () => setSelected(parseHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  // Load agent detail + events; subscribe to SSE. Scroll only when following.
  useEffect(() => {
    if (!selected) {
      setDetail(null);
      setEvents([]);
      return;
    }
    let cancelled = false;
    autoFollowRef.current = true;
    setHasNew(false);
    setTab("conversation");

    const load = async () => {
      try {
        const [dRes, eRes] = await Promise.all([
          fetch(`/api/agents/${selected}`),
          fetch(`/api/events?agent_id=${selected}`),
        ]);
        if (cancelled) return;
        if (dRes.ok) setDetail(await dRes.json());
        else setDetail(null);
        if (eRes.ok) {
          const data = await eRes.json();
          setEvents(data.events || []);
          // Initial open: jump to bottom after paint
          requestAnimationFrame(() => {
            const el = timelineRef.current;
            if (el) el.scrollTop = el.scrollHeight;
          });
        }
      } catch {
        /* network blip */
      }
    };
    void load();

    const source = new EventSource(`/api/stream?agent_id=${selected}`);
    source.onmessage = (message) => {
      try {
        const event = JSON.parse(message.data) as Event;
        setEvents((prev) => {
          if (prev.some((x) => x.id === event.id)) return prev;
          return [...prev, event];
        });
        if (autoFollowRef.current) {
          requestAnimationFrame(() => {
            const el = timelineRef.current;
            if (!el) return;
            const behavior = prefersReducedMotion() ? "auto" : "smooth";
            el.scrollTo({ top: el.scrollHeight, behavior });
          });
        } else {
          setHasNew(true);
        }
        // Soft-refresh agent metadata (status, signoff) without touching scroll
        void fetch(`/api/agents/${selected}`)
          .then((r) => (r.ok ? r.json() : null))
          .then((d) => {
            if (d && !cancelled) setDetail(d);
          });
      } catch {
        /* bad event */
      }
    };
    return () => {
      cancelled = true;
      source.close();
    };
  }, [selected]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("grok-observer-theme", theme);
  }, [theme]);

  useEffect(() => {
    if (!query.trim()) {
      setSearchHits([]);
      return;
    }
    const timer = window.setTimeout(async () => {
      try {
        const data = await fetch(`/api/search?q=${encodeURIComponent(query)}`).then((r) =>
          r.json(),
        );
        setSearchHits(data.results || []);
      } catch {
        setSearchHits([]);
      }
    }, 250);
    return () => window.clearTimeout(timer);
  }, [query]);

  const onTimelineScroll = (e: React.UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 96;
    autoFollowRef.current = nearBottom;
    if (nearBottom) setHasNew(false);
  };

  const jumpToBottom = () => {
    const el = timelineRef.current;
    if (!el) return;
    autoFollowRef.current = true;
    setHasNew(false);
    el.scrollTo({
      top: el.scrollHeight,
      behavior: prefersReducedMotion() ? "auto" : "smooth",
    });
  };

  const selectAgent = (id: string) => {
    location.hash = `#/agents/${id}`;
    setSelected(id);
    setQuery("");
    // On narrow screens, close drawer after pick
    if (window.matchMedia("(max-width: 860px)").matches) setSidebarOpen(false);
  };

  const cycleTheme = () => {
    setTheme((t) => (t === "system" ? "dark" : t === "dark" ? "light" : "system"));
  };

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    setDeleteError("");
    const id = deleteTarget.id;
    try {
      const res = await fetch(`/api/agents/${id}/delete`, { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setDeleteError(data.error || `删除失败 (${res.status})`);
        return;
      }
      const remaining = agents.filter((a) => a.id !== id);
      setAgents(remaining);
      setTasks((prev) =>
        prev.filter(
          (t) =>
            t.thread_id !== deleteTarget.thread_id ||
            remaining.some((a) => a.thread_id === t.thread_id),
        ),
      );
      setDeleteTarget(null);
      if (selected === id) {
        const next = remaining[0];
        if (next) {
          location.hash = `#/agents/${next.id}`;
          setSelected(next.id);
        } else {
          location.hash = "";
          setSelected("");
          setDetail(null);
          setEvents([]);
        }
      }
      void bootstrap();
    } catch (e) {
      setDeleteError(String(e));
    }
  };

  const grouped = useMemo(() => {
    const byThread = new Map<string, Agent[]>();
    for (const a of agents) {
      const list = byThread.get(a.thread_id) || [];
      list.push(a);
      byThread.set(a.thread_id, list);
    }
    const fromTasks = tasks.map((task) => ({
      task,
      agents: byThread.get(task.thread_id) || [],
    }));
    // Orphan agents (task row missing)
    const known = new Set(tasks.map((t) => t.thread_id));
    const orphans: Agent[] = agents.filter((a) => !known.has(a.thread_id));
    if (orphans.length) {
      const fake: Task = {
        thread_id: "_orphan",
        title: "其他代理",
        cwd: "",
        updated_at: "",
      };
      fromTasks.push({ task: fake, agents: orphans });
    }
    return fromTasks.filter((g) => g.agents.length > 0);
  }, [tasks, agents]);

  const current = detail?.agent;
  const stateKey = current?.signoff_verdict || current?.status || "";

  return (
    <div className={`shell ${sidebarOpen ? "" : "sidebar-collapsed"}`}>
      {/* ── sidebar ───────────────────────────────────────────────────── */}
      <aside className="sidebar" aria-label="会话列表">
        <div className="brand">
          <div className="brand-mark" aria-hidden>
            <Braces size={16} />
          </div>
          <div className="brand-text">
            <strong>Grok Observer</strong>
            <span>本机只读 · 7 天</span>
          </div>
          <button
            type="button"
            className="icon-btn mobile-only"
            aria-label="关闭侧栏"
            onClick={() => setSidebarOpen(false)}
          >
            <X size={16} />
          </button>
        </div>

        <div className="search-wrap">
          <label className="search" htmlFor="global-search">
            <Search size={14} aria-hidden />
            <input
              id="global-search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="搜索工作内容…"
              autoComplete="off"
            />
            {query && (
              <button
                type="button"
                className="icon-btn ghost"
                aria-label="清除搜索"
                onClick={() => setQuery("")}
              >
                <X size={14} />
              </button>
            )}
          </label>
          {query.trim() && (
            <div className="search-popover" role="listbox" aria-label="搜索结果">
              {searchHits.length ? (
                searchHits.map((hit, i) => (
                  <button
                    type="button"
                    key={`${hit.agent_id}-${i}`}
                    role="option"
                    onClick={() => selectAgent(hit.agent_id)}
                  >
                    <small>{hit.kind}</small>
                    <span>
                      <HighlightSnippet text={hit.snippet} matches={hit.matches} query={query} />
                    </span>
                  </button>
                ))
              ) : (
                <p className="hint">没有匹配内容</p>
              )}
            </div>
          )}
        </div>

        <div className="task-list" role="navigation" aria-label="Codex 任务与 Grok 代理">
          {bootError && (
            <p className="err-text pad">无法连接监督器：{bootError}</p>
          )}
          {!grouped.length && !bootError && (
            <p className="hint pad">暂无代理会话</p>
          )}
          {grouped.map(({ task, agents: kids }) => {
            const collapsed = !!collapsedTasks[task.thread_id];
            return (
              <section
                key={task.thread_id}
                className={`task-group ${collapsed ? "collapsed" : ""}`}
              >
                <button
                  type="button"
                  className="task-toggle"
                  aria-expanded={!collapsed}
                  onClick={() =>
                    setCollapsedTasks((m) => ({
                      ...m,
                      [task.thread_id]: !collapsed,
                    }))
                  }
                >
                  <ChevronDown size={12} className="task-chevron" aria-hidden />
                  <MessageSquare size={13} aria-hidden />
                  <span className="task-copy">
                    <strong title={task.title}>{task.title || "未命名任务"}</strong>
                    <span>
                      {kids.length} 代理
                      {task.thread_id !== "_orphan" &&
                        ` · ${task.thread_id.slice(0, 10)}…`}
                    </span>
                  </span>
                </button>
                {!collapsed && (
                  <ul className="agent-list">
                    {kids.map((agent) => {
                      const label = agent.signoff_verdict || agent.status;
                      const active = selected === agent.id;
                      const canDelete = !["queued", "running"].includes(agent.status);
                      return (
                        <li key={agent.id} className="agent-row">
                          <a
                            className={`agent-link ${active ? "active" : ""}`}
                            href={`#/agents/${agent.id}`}
                            onClick={(e) => {
                              e.preventDefault();
                              selectAgent(agent.id);
                            }}
                          >
                            <i
                              className={`status-dot status-${label}`}
                              title={STATUS_LABELS[label] || label}
                              aria-hidden
                            />
                            <span className="agent-copy">
                              <strong title={agent.name}>{agent.name}</strong>
                              <span>
                                {STATUS_LABELS[label] || label} ·{" "}
                                {formatTime(agent.updated_at)}
                              </span>
                            </span>
                          </a>
                          {canDelete && (
                            <button
                              type="button"
                              className="delete-agent"
                              title="从观察器移除"
                              aria-label={`删除 ${agent.name}`}
                              onClick={() => {
                                setDeleteError("");
                                setDeleteTarget(agent);
                              }}
                            >
                              <Trash2 size={13} />
                            </button>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                )}
              </section>
            );
          })}
        </div>

        <div className="sidebar-foot">
          <span>
            <i className="live-dot" aria-hidden />
            仅本机观察
          </span>
          <button
            type="button"
            className="text-btn"
            onClick={() => {
              if (window.confirm("关闭网页展示？（不会取消正在运行的 Grok）")) {
                void fetch("/api/viewer/shutdown", { method: "POST" });
              }
            }}
          >
            <ServerOff size={13} aria-hidden />
            关闭展示
          </button>
        </div>
      </aside>

      {sidebarOpen && (
        <button
          type="button"
          className="sidebar-scrim mobile-only"
          aria-label="关闭侧栏"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* ── main ──────────────────────────────────────────────────────── */}
      <main className="workspace">
        <header className="topbar">
          <button
            type="button"
            className="icon-btn"
            aria-label={sidebarOpen ? "折叠侧栏" : "展开侧栏"}
            onClick={() => setSidebarOpen((v) => !v)}
          >
            {sidebarOpen ? <PanelLeftClose size={17} /> : <PanelLeftOpen size={17} />}
          </button>
          <div className="agent-heading">
            {current ? (
              <>
                <div className="eyebrow-row">
                  <span className={`status-pill status-${stateKey}`}>
                    {STATUS_LABELS[stateKey] || stateKey}
                  </span>
                  <span className="faint">整机权限 · 自动批准 · 只读观察</span>
                </div>
                <h1 title={current.name}>{current.name}</h1>
              </>
            ) : (
              <>
                <div className="eyebrow-row">
                  <span className="faint">Grok Agent Observer</span>
                </div>
                <h1>选择一个 Grok 子代理</h1>
              </>
            )}
          </div>
          <div className="top-actions">
            <button
              type="button"
              className="icon-btn"
              title={`主题：${theme}`}
              aria-label={`切换主题（当前 ${theme}）`}
              onClick={cycleTheme}
            >
              {theme === "dark" ? (
                <Moon size={16} />
              ) : theme === "light" ? (
                <Sun size={16} />
              ) : (
                <Activity size={16} />
              )}
            </button>
          </div>
        </header>

        {current ? (
          <>
            <div className="meta-strip" title={current.cwd}>
              <span>
                <Bot size={12} aria-hidden />
                {current.grok_session_id
                  ? current.grok_session_id.slice(0, 8)
                  : "—"}
              </span>
              <span className="mono ellipsis">{current.cwd}</span>
              <span>更新 {formatDateTime(current.updated_at)}</span>
            </div>

            <nav className="tabs" aria-label="主区域">
              {(
                [
                  ["conversation", "对话", <MessageSquare size={14} key="i" />],
                  [
                    "changes",
                    "Changes",
                    <GitCompareArrows size={14} key="i" />,
                  ],
                  ["details", "详情", <Braces size={14} key="i" />],
                ] as const
              ).map(([id, label, icon]) => (
                <button
                  key={id}
                  type="button"
                  className={tab === id ? "active" : ""}
                  aria-current={tab === id ? "page" : undefined}
                  onClick={() => setTab(id)}
                >
                  {icon}
                  {label}
                  {id === "changes" && (
                    <b>{detail?.changes.length || 0}</b>
                  )}
                </button>
              ))}
            </nav>

            {tab === "conversation" && (
              <div
                className="timeline"
                ref={timelineRef}
                onScroll={onTimelineScroll}
                role="log"
                aria-live="polite"
                aria-relevant="additions"
              >
                <div className="timeline-inner">
                  <div className="session-start">
                    <span>SESSION</span>
                    <i />
                  </div>
                  {stream.map((item, idx) => (
                    <StreamItemView
                      key={
                        item.kind === "toolchain" ||
                        item.kind === "diagnostics" ||
                        item.kind === "turn_sep"
                          ? item.key
                          : `${item.kind}-${item.event.id}-${idx}`
                      }
                      item={item}
                    />
                  ))}
                  {!stream.length && (
                    <div className="empty inline">
                      <p>等待事件流…</p>
                    </div>
                  )}
                </div>
                {hasNew && (
                  <button type="button" className="new-content" onClick={jumpToBottom}>
                    有新内容
                    <ChevronDown size={14} aria-hidden />
                  </button>
                )}
              </div>
            )}

            {tab === "changes" && (
              <div className="panel-scroll">
                <ChangesPanel changes={detail?.changes || []} />
              </div>
            )}

            {tab === "details" && (
              <div className="panel-scroll">
                <DetailsPanel agent={current} turns={detail?.turns || []} />
              </div>
            )}
          </>
        ) : (
          <div className="empty">
            <Bot size={28} aria-hidden />
            <p>从左侧选择一个 Grok 子代理查看对话与轨迹</p>
            {!sidebarOpen && (
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => setSidebarOpen(true)}
              >
                打开侧栏
              </button>
            )}
          </div>
        )}
      </main>

      <ConfirmDialog
        open={!!deleteTarget}
        title="从观察器移除？"
        body={
          deleteError
            ? deleteError
            : `将删除「${deleteTarget?.name || ""}」的展示记录与本地产物，不可恢复。运行中的代理无法移除。`
        }
        confirmLabel={deleteError ? "重试" : "删除"}
        danger
        onConfirm={() => void confirmDelete()}
        onCancel={() => {
          setDeleteTarget(null);
          setDeleteError("");
        }}
      />
    </div>
  );
}

const rootEl = document.getElementById("root");
if (rootEl) {
  createRoot(rootEl).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  );
}

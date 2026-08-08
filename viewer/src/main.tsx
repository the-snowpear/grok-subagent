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
  Archive,
  ArchiveRestore,
  Bot,
  Braces,
  Check,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  Copy,
  FileCode2,
  Folder,
  FolderOpen,
  GitCompareArrows,
  ListTodo,
  Loader2,
  MessageSquare,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Pencil,
  Pin,
  PinOff,
  Search,
  ServerOff,
  Square,
  Sun,
  Trash2,
  Wrench,
  X,
  Zap,
} from "lucide-react";
import "./styles.css";
import { HighlightSnippet, type SearchMatch } from "./searchHighlight";
import {
  buildChangeTree,
  countChangeTreeFiles,
  extractFileDiff,
  parseUnifiedDiffLines,
  type ChangeTreeNode,
} from "./changeTree";

import {
  buildStream,
  cleanText,
  classifyToolStep,
  collapseToolSteps,
  deriveLiveActivity,
  editDiffFromInput,
  isToolEvent,
  parsePayload,
  planFromEvents,
  sumToolStepDiffStats,
  summarizeToolchain,
  toolStepDiffStats,
  type DiffLineStats,
  type EditDiffView,
  type Event,
  type LiveActivity,
  type Plan,
  type StreamItem,
  type ToolStep,
  type Turn,
} from "./streamModel";

/* ─── types ─────────────────────────────────────────────────────────────── */

type Task = {
  thread_id: string;
  title: string;
  cwd: string;
  updated_at: string;
  pinned?: number;
  archived?: number;
};
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
  /** Sidebar label; falls back to name when empty. */
  display_title?: string;
  pinned?: number;
  archived?: number;
};

function agentLabel(agent: Pick<Agent, "display_title" | "name">): string {
  const title = (agent.display_title || "").trim();
  return title || agent.name || "未命名代理";
}

function isPinned(value?: number): boolean {
  return Number(value || 0) > 0;
}

function isArchived(value?: number): boolean {
  return Number(value || 0) > 0;
}
type Change = {
  id: number;
  path: string;
  kind: string;
  preexisting: number;
  diff_artifact?: string;
  /** claimed | observed | both — tool edit ledger vs workspace snapshot. */
  source?: string;
  shared?: number;
  tool_name?: string;
  added?: number;
  deleted?: number;
};
type SearchHit = {
  agent_id: string;
  kind: string;
  snippet: string;
  matches?: SearchMatch[];
  /** Optional deep-link targets (backend may add these later). */
  event_id?: number;
  turn_id?: number;
  event_seq?: number;
};

/** Status-like SSE types that force an immediate soft-refresh of agent metadata. */
const STATUS_LIKE_TYPES = new Set([
  "completed",
  "failed",
  "cancelled",
  "interrupted",
  "signoff",
  "end",
  "process",
  "pending_update",
  "update_applied",
  "interjection",
]);

const SOFT_REFRESH_MS = 1500;

type ThemeMode = "system" | "light" | "dark";
type MainTab = "conversation" | "changes" | "details";

/* ─── constants ─────────────────────────────────────────────────────────── */

const STATUS_LABELS: Record<string, string> = {
  queued: "排队",
  running: "运行中",
  completed: "待签收",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断并更新",
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

/** Match CSS @media (max-width: 860px) drawer layout. */
const NARROW_VIEWPORT_MQ = "(max-width: 860px)";

function isNarrowViewport(): boolean {
  return window.matchMedia(NARROW_VIEWPORT_MQ).matches;
}

/** Normalize cwd for grouping; empty → unknown bucket. */
function workspaceKey(cwd?: string): string {
  const raw = (cwd || "").trim();
  if (!raw) return "_unknown";
  // Case-fold on Windows-style paths for stable grouping.
  return raw.replace(/[/\\]+$/, "").toLowerCase();
}

/** Display name = path basename; keep full path for tooltip/subtitle. */
function workspaceLabel(cwd?: string): { name: string; path: string } {
  const path = (cwd || "").trim();
  if (!path) return { name: "未知工作区", path: "" };
  const parts = path.replace(/[/\\]+$/, "").split(/[/\\]/).filter(Boolean);
  const name = parts[parts.length - 1] || path;
  return { name, path };
}

function newerStamp(a?: string, b?: string): string {
  if (!a) return b || "";
  if (!b) return a;
  return a >= b ? a : b;
}

/* ─── small UI atoms ────────────────────────────────────────────────────── */

function CopyButton({
  value,
  label = "复制",
  className = "",
  size = 14,
}: {
  value: string;
  label?: string;
  className?: string;
  size?: number;
}) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className={`icon-btn ${className}`.trim()}
      title={label}
      aria-label={label}
      onClick={async (e) => {
        // Avoid toggling parent <details> when nested in summaries (rare).
        e.stopPropagation();
        try {
          await navigator.clipboard.writeText(value);
          setDone(true);
          window.setTimeout(() => setDone(false), 1200);
        } catch {
          /* ignore */
        }
      }}
    >
      {done ? <Check size={size} aria-hidden /> : <Copy size={size} aria-hidden />}
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

const MarkdownBody = React.memo(function MarkdownBody({ children }: { children: string }) {
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
});

/** Back-compat alias used throughout the file. */
const Markdown = MarkdownBody;

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

function toolStepTone(status: string): "ok" | "err" | "run" | "info" {
  if (status === "done" || status === "completed" || status === "success") return "ok";
  if (status === "failed" || status === "error") return "err";
  if (status === "info") return "info";
  return "run";
}

/** One I/O pane: fixed header row (label + copy), independent scroll body. */
function ToolIoPane({ label, text }: { label: string; text: string }) {
  return (
    <section className="tool-section">
      <header className="tool-section-head">
        <span className="tool-section-label">{label}</span>
        <CopyButton value={text} className="tool-copy-btn" size={13} />
      </header>
      <div className="tool-pre-scroll">
        <pre className="tool-pre">{text}</pre>
      </div>
    </section>
  );
}

/**
 * Edit tools: show line-level unified diff (not raw old_string/new_string JSON).
 * Header: path + stats badge + copy unified text.
 */
function EditDiffPane({ view }: { view: EditDiffView }) {
  const label = view.path || "Diff";
  return (
    <section className="tool-section tool-section-diff">
      <header className="tool-section-head">
        <span className="tool-section-label tool-section-label-path" title={label}>
          {label}
        </span>
        <DiffStatsBadge stats={view.stats} />
        <CopyButton value={view.unifiedText} className="tool-copy-btn" size={13} />
      </header>
      <div className="tool-pre-scroll">
        <div className="diff-view" role="text" aria-label={`diff ${label}`}>
          {view.lines.length === 0 ? (
            <div className="diff-line diff-ctx">(empty)</div>
          ) : (
            view.lines.map((line, idx) => {
              const mark = line.kind === "add" ? "+" : line.kind === "del" ? "−" : " ";
              return (
                <div key={idx} className={`diff-line diff-${line.kind}`}>
                  <span className="diff-mark" aria-hidden>
                    {mark}
                  </span>
                  <span className="diff-text">{line.text || " "}</span>
                </div>
              );
            })
          )}
        </div>
      </div>
    </section>
  );
}

/** Path from read_file-style rawInput (target_file / path / …). */
function readPathFromInput(input: unknown): string | null {
  if (!input || typeof input !== "object" || Array.isArray(input)) return null;
  const rec = input as Record<string, unknown>;
  for (const key of ["target_file", "file_path", "path", "filePath", "url"]) {
    const v = rec[key];
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  return null;
}

/**
 * Read tools: show file body only (path as label) — not 输入 JSON / 输出 panels.
 */
function ReadContentPane({ path, content }: { path: string | null; content: string }) {
  const label = path || "内容";
  return (
    <section className="tool-section tool-section-read">
      <header className="tool-section-head">
        <span className="tool-section-label tool-section-label-path" title={label}>
          {label}
        </span>
        {content ? <CopyButton value={content} className="tool-copy-btn" size={13} /> : null}
      </header>
      <div className="tool-pre-scroll">
        <pre className="tool-pre">{content || "（尚无内容）"}</pre>
      </div>
    </section>
  );
}

/**
 * Tool body: edit → unified diff; read → file content; others → input/output JSON.
 * Edit/read hide the generic 输出 pane (content is the signal).
 */
function ToolStepDetail({ step }: { step: ToolStep }) {
  // Prefer structured diff when rawInput carries old/new or write content.
  const editDiff = step.input != null ? editDiffFromInput(step.input) : null;
  const isRead = !editDiff && classifyToolStep(step) === "read";
  const readPath = isRead ? readPathFromInput(step.input) : null;
  const readContent = isRead ? step.output || "" : "";

  const inputText = editDiff || isRead ? "" : formatJson(step.input);
  // Edit/read: no separate 输出 panel.
  const outputText = editDiff || isRead ? "" : step.output || "";
  const hasIo = Boolean(editDiff || isRead || inputText || outputText);

  return (
    <div className="tool-detail">
      {editDiff && <EditDiffPane view={editDiff} />}
      {isRead && <ReadContentPane path={readPath} content={readContent} />}
      {inputText && <ToolIoPane label="输入" text={inputText} />}
      {outputText && <ToolIoPane label="输出" text={outputText} />}
      {!hasIo && step.events[0] && (
        <section className="tool-section tool-section-raw">
          <header className="tool-section-head">
            <span className="tool-section-label">原始数据</span>
          </header>
          <div className="tool-raw-wrap">
            <RawPayload event={step.events[0]} />
          </div>
        </section>
      )}
      {step.events.length > 1 && (
        <details className="tool-raw-events">
          <summary>相关事件 · {step.events.length}</summary>
          <div className="tool-raw-events-list">
            {step.events.map((ev) => (
              <RawPayload key={ev.id} event={ev} />
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

/** Format elapsed seconds as `12s` or `1m 05s`. */
function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}m ${String(s).padStart(2, "0")}s`;
}

/**
 * CLI-style live status (Grok Build / Claude Code bottom-left):
 * only while the selected subagent is queued/running; phase-colored.
 * Click jumps to bottom of the conversation stream.
 */
function ActivityBar({
  activity,
  startedAt,
  onClick,
}: {
  activity: LiveActivity;
  /** ISO timestamp for elapsed tick (agent.created_at). */
  startedAt?: string;
  onClick?: () => void;
}) {
  const [elapsedSec, setElapsedSec] = useState(0);

  useEffect(() => {
    if (!activity.visible || !startedAt) {
      setElapsedSec(0);
      return;
    }
    const startMs = new Date(startedAt).getTime();
    if (Number.isNaN(startMs)) {
      setElapsedSec(0);
      return;
    }
    const tick = () => {
      setElapsedSec(Math.max(0, Math.floor((Date.now() - startMs) / 1000)));
    };
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [activity.visible, startedAt]);

  if (!activity.visible) return null;

  const elapsedLabel = startedAt ? formatElapsed(elapsedSec) : "";
  const title = elapsedLabel ? `${activity.label} · ${elapsedLabel}` : activity.label;
  const interactive = Boolean(onClick);

  return (
    <div
      className={`activity-bar phase-${activity.phase} tone-${activity.tone}${
        interactive ? " activity-bar-clickable" : ""
      }`}
      role={interactive ? "button" : "status"}
      tabIndex={interactive ? 0 : undefined}
      aria-live="polite"
      aria-atomic="true"
      title={interactive ? `${title}（点击跳到底部）` : title}
      onClick={onClick}
      onKeyDown={
        interactive
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onClick?.();
              }
            }
          : undefined
      }
    >
      <span className="activity-spinner" aria-hidden />
      <span className="activity-label">{activity.label}</span>
      {elapsedLabel && (
        <span className="activity-elapsed" aria-label={`已运行 ${elapsedLabel}`}>
          · {elapsedLabel}
        </span>
      )}
    </div>
  );
}

/** Codex-style +N −M next to Edit titles (from old/new strings, not git). */
function DiffStatsBadge({ stats }: { stats: DiffLineStats | null | undefined }) {
  if (!stats) return null;
  const { added, removed } = stats;
  if (added <= 0 && removed <= 0) return null;
  return (
    <span
      className="diff-stats"
      aria-label={`+${added} −${removed}`}
      title={`变更行数（由 old/new 文本推算，非 git）· +${added} −${removed}`}
    >
      {added > 0 && <span className="diff-add">+{added}</span>}
      {removed > 0 && <span className="diff-del">−{removed}</span>}
    </span>
  );
}

/** One tool row inside an expanded chain (still collapsed by default). */
function ToolStepRow({ step }: { step: ToolStep }) {
  const tone = toolStepTone(step.status);
  const label = step.title || step.name || "工具";
  const diff = toolStepDiffStats(step);
  const tip = [
    label,
    diff && (diff.added > 0 || diff.removed > 0) ? `+${diff.added} −${diff.removed}` : "",
    step.startedAt ? formatTime(step.startedAt) : "",
    step.status,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <details className={`tool-step tone-${tone}`}>
      <summary title={tip}>
        <Wrench size={12} className="row-icon tool-icon" aria-hidden />
        <span className="tool-step-title">{label}</span>
        <DiffStatsBadge stats={diff} />
        <span className={`tool-status-dot tone-${tone}`} aria-hidden />
        <ChevronRight size={12} className="row-chevron" aria-hidden />
      </summary>
      <div className="tool-step-body">
        <ToolStepDetail step={step} />
      </div>
    </details>
  );
}

/**
 * Whole toolchain folds as one unit (default collapsed).
 * Collapsed text uses Grok Build aggregates: "Read 3 files · Searched 4 patterns".
 * Thoughts still split consecutive groups via buildStream.
 */
function ToolchainRow({ steps, events }: { steps: ToolStep[]; events: Event[] }) {
  if (!steps.length) return null;

  const n = steps.length;
  const aggregate = summarizeToolchain(steps);
  const labels = steps.map((s) => s.title || s.name || "工具");
  const detailHint = labels.join(" · ");
  const hasErr = steps.some((s) => toolStepTone(s.status) === "err");
  const hasRun = steps.some((s) => toolStepTone(s.status) === "run");
  const groupTone = hasErr ? "err" : hasRun ? "run" : "ok";
  const firstAt = events[0]?.created_at || steps[0]?.startedAt;
  const tip = [aggregate, firstAt ? formatTime(firstAt) : "", detailHint]
    .filter(Boolean)
    .join(" · ");

  // Aggregate +N/−M across all edit steps (shown on multi-step header).
  const totalDiff = n > 1 ? sumToolStepDiffStats(steps) : null;

  // Single tool: one fold level (summary = tool title, body = I/O)
  if (n === 1) {
    const singleDiff = toolStepDiffStats(steps[0]);
    const singleTip = [
      tip,
      singleDiff && (singleDiff.added > 0 || singleDiff.removed > 0)
        ? `+${singleDiff.added} −${singleDiff.removed}`
        : "",
    ]
      .filter(Boolean)
      .join(" · ");
    return (
      <details
        className={`toolchain-group tone-${groupTone}`}
        data-event-id={events[0]?.id}
        data-seq={events[0]?.seq}
      >
        {events.slice(1).map((ev) => (
          <span
            key={ev.id}
            className="stream-anchor"
            data-event-id={ev.id}
            data-seq={ev.seq}
            aria-hidden
          />
        ))}
        <summary title={singleTip}>
          <Wrench size={13} className="row-icon tool-icon" aria-hidden />
          <span className="toolchain-summary-title">{aggregate}</span>
          <DiffStatsBadge stats={singleDiff} />
          <span className={`tool-status-dot tone-${groupTone}`} aria-hidden />
          <ChevronRight size={13} className="row-chevron" aria-hidden />
        </summary>
        <div className="toolchain-body toolchain-body-single">
          <ToolStepDetail step={steps[0]} />
        </div>
      </details>
    );
  }

  const multiTip = [
    tip,
    totalDiff ? `+${totalDiff.added} −${totalDiff.removed}` : "",
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <details
      className={`toolchain-group tone-${groupTone}`}
      data-event-id={events[0]?.id}
      data-seq={events[0]?.seq}
    >
      {/* Anchors so search can deep-link any event inside the collapsed chain. */}
      {events.map((ev) => (
        <span
          key={ev.id}
          className="stream-anchor"
          data-event-id={ev.id}
          data-seq={ev.seq}
          aria-hidden
        />
      ))}
      <summary title={multiTip}>
        <Wrench size={13} className="row-icon tool-icon" aria-hidden />
        <span className="toolchain-summary-title toolchain-summary-aggregate">{aggregate}</span>
        <DiffStatsBadge stats={totalDiff} />
        <span className={`tool-status-dot tone-${groupTone}`} aria-hidden />
        <ChevronRight size={13} className="row-chevron" aria-hidden />
      </summary>
      <div className="toolchain-body">
        {steps.map((step) => (
          <ToolStepRow key={step.key} step={step} />
        ))}
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

/** Wrap a single-event stream row with deep-link anchors for search. */
function StreamEventWrap({
  event,
  children,
  className = "",
}: {
  event: Event;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`stream-item ${className}`.trim()}
      data-event-id={event.id}
      data-seq={event.seq}
    >
      {children}
    </div>
  );
}

const StreamItemView = React.memo(function StreamItemView({ item }: { item: StreamItem }) {
  switch (item.kind) {
    case "user":
      return (
        <StreamEventWrap event={item.event}>
          <UserMessage text={item.text} time={item.event.created_at} />
        </StreamEventWrap>
      );
    case "text":
      return (
        <StreamEventWrap event={item.event}>
          <AssistantMessage text={item.text} time={item.event.created_at} />
        </StreamEventWrap>
      );
    case "thought":
      return (
        <StreamEventWrap event={item.event}>
          <ThoughtRow text={item.text} time={item.event.created_at} />
        </StreamEventWrap>
      );
    case "toolchain":
      return <ToolchainRow steps={item.steps} events={item.events} />;
    case "diagnostics":
      return (
        <div
          className="stream-item"
          data-event-id={item.events[0]?.id}
          data-seq={item.events[0]?.seq}
        >
          {item.events.map((ev) => (
            <span
              key={ev.id}
              className="stream-anchor"
              data-event-id={ev.id}
              data-seq={ev.seq}
              aria-hidden
            />
          ))}
          <DiagnosticGroup events={item.events} />
        </div>
      );
    case "status":
      return (
        <StreamEventWrap event={item.event}>
          <StatusRow
            label={item.label}
            time={item.event.created_at}
            tone={item.tone}
            event={item.event}
          />
        </StreamEventWrap>
      );
    case "meta":
      return (
        <StreamEventWrap event={item.event}>
          <MetaRow label={item.label} time={item.event.created_at} event={item.event} />
        </StreamEventWrap>
      );
    case "turn_sep":
      return <TurnSeparator turnNo={item.turnNo} turnId={item.turnId} />;
  }
});

/* ─── panels ────────────────────────────────────────────────────────────── */

function changeSourceLabel(source?: string): string | null {
  switch ((source || "").toLowerCase()) {
    case "claimed":
      return "工具编辑";
    case "observed":
      return "工作区观测";
    case "both":
      return "工具+磁盘";
    default:
      return null;
  }
}

type ArtifactLoader = (artifactPath: string) => Promise<string>;

/**
 * Auto-load shared git-diff artifact on expand; slice to this file path.
 * Cache lives on ChangesPanel so multi-file expand hits the network once.
 */
function GitDiffPane({
  artifactPath,
  filePath,
  loadArtifact,
}: {
  artifactPath: string;
  filePath: string;
  loadArtifact: ArtifactLoader;
}) {
  const [status, setStatus] = useState<"loading" | "ok" | "err">("loading");
  const [text, setText] = useState("");
  const [matched, setMatched] = useState(true);
  const [err, setErr] = useState("");

  useEffect(() => {
    let cancelled = false;
    setStatus("loading");
    setErr("");
    loadArtifact(artifactPath)
      .then((full) => {
        if (cancelled) return;
        const sliced = extractFileDiff(full, filePath);
        setText(sliced.text);
        setMatched(sliced.matched);
        setStatus("ok");
      })
      .catch((e) => {
        if (cancelled) return;
        setErr(String(e));
        setStatus("err");
      });
    return () => {
      cancelled = true;
    };
  }, [artifactPath, filePath, loadArtifact]);

  if (status === "loading") {
    return <p className="hint change-diff-status">加载 diff…</p>;
  }
  if (status === "err") {
    return <p className="err-text change-diff-status">{err}</p>;
  }
  if (!text.trim()) {
    return <p className="hint change-diff-status">（空 diff）</p>;
  }

  const lines = parseUnifiedDiffLines(text);
  return (
    <div className="change-diff-wrap">
      {!matched && (
        <p className="hint">未在 diff 中定位到该路径，展示完整 diff。</p>
      )}
      <div className="code-block change-diff-block">
        <div className="code-head">
          <span>diff</span>
          <CopyButton value={text} />
        </div>
        <ScrollBody maxHeight="min(45vh, 400px)">
          <div className="diff-view" role="text" aria-label={`diff ${filePath}`}>
            {lines.map((line, idx) => {
              // Meta (headers/hunks) keep full git text; add/del strip leading +/- for mark column.
              if (line.kind === "meta") {
                return (
                  <div key={idx} className="diff-line diff-meta">
                    <span className="diff-text">{line.text || " "}</span>
                  </div>
                );
              }
              const mark =
                line.kind === "add" ? "+" : line.kind === "del" ? "−" : " ";
              const body =
                line.kind === "add" || line.kind === "del"
                  ? line.text.slice(1)
                  : line.text.startsWith(" ")
                    ? line.text.slice(1)
                    : line.text;
              return (
                <div key={idx} className={`diff-line diff-${line.kind}`}>
                  <span className="diff-mark" aria-hidden>
                    {mark}
                  </span>
                  <span className="diff-text">{body || " "}</span>
                </div>
              );
            })}
          </div>
        </ScrollBody>
      </div>
    </div>
  );
}

function ChangeFileRow({
  change,
  name,
  depth,
  loadArtifact,
}: {
  change: Change;
  name: string;
  depth: number;
  loadArtifact: ArtifactLoader;
}) {
  const srcLabel = changeSourceLabel(change.source);
  const stats: DiffLineStats | null =
    (change.added && change.added > 0) || (change.deleted && change.deleted > 0)
      ? { added: change.added || 0, removed: change.deleted || 0 }
      : null;
  const label = change.path.includes(" → ") ? change.path : name;

  return (
    <details className="row change-row change-file-row" data-depth={depth}>
      <summary title={change.path}>
        <FileCode2 size={13} className="row-icon" aria-hidden />
        <span className="row-title mono change-file-name">{label}</span>
        <span className="row-badge">{change.kind}</span>
        <DiffStatsBadge stats={stats} />
        {srcLabel && (
          <span
            className={`row-badge change-src change-src-${(change.source || "observed").toLowerCase()}`}
          >
            {srcLabel}
          </span>
        )}
        {!!change.shared && <span className="row-badge change-shared">可能交叉</span>}
        {!!change.preexisting && <span className="row-sub">任务前已修改</span>}
        {change.tool_name && <span className="row-sub mono">{change.tool_name}</span>}
        <ChevronRight size={14} className="row-chevron" aria-hidden />
      </summary>
      <div className="row-body change-file-body">
        <p className="hint">
          {change.source === "claimed"
            ? "来自工具编辑账本；磁盘差分未单独确认此路径。"
            : change.source === "both"
              ? "工具声明编辑且工作区快照确认变更。"
              : "工作区快照观测；未必由本代理工具直接写出。"}
        </p>
        {change.diff_artifact ? (
          <GitDiffPane
            artifactPath={change.diff_artifact}
            filePath={change.path}
            loadArtifact={loadArtifact}
          />
        ) : (
          <p className="hint">无工作区 diff 快照（例如仅工具声明路径）。</p>
        )}
      </div>
    </details>
  );
}

function ChangeTreeNodes({
  nodes,
  depth,
  defaultOpen,
  loadArtifact,
}: {
  nodes: ChangeTreeNode[];
  depth: number;
  defaultOpen: boolean;
  loadArtifact: ArtifactLoader;
}) {
  return (
    <ul className="change-tree-list" data-depth={depth}>
      {nodes.map((node) => {
        if (node.type === "dir") {
          return (
            <li key={`d:${node.path}`} className="change-tree-item">
              {/* Small trees: expand all dirs; large: only top-level folders. */}
              <details className="change-dir-row" open={defaultOpen || depth === 0}>
                <summary>
                  <FolderOpen size={13} className="row-icon change-dir-icon open-only" aria-hidden />
                  <Folder size={13} className="row-icon change-dir-icon closed-only" aria-hidden />
                  <span className="change-dir-name mono">{node.name}</span>
                  <span className="row-sub">
                    {countChangeTreeFiles(node.children)} 文件
                  </span>
                  <ChevronRight size={14} className="row-chevron" aria-hidden />
                </summary>
                <ChangeTreeNodes
                  nodes={node.children}
                  depth={depth + 1}
                  defaultOpen={defaultOpen}
                  loadArtifact={loadArtifact}
                />
              </details>
            </li>
          );
        }
        // One row per change entry (multi-turn same path → multiple leaves under same name).
        return node.changes.map((c) => (
          <li key={`f:${c.id}`} className="change-tree-item">
            <ChangeFileRow
              change={c as Change}
              name={node.name}
              depth={depth}
              loadArtifact={loadArtifact}
            />
          </li>
        ));
      })}
    </ul>
  );
}

function ChangesPanel({ changes }: { changes: Change[] }) {
  // Shared artifact cache: turn-level git-diff is reused across file rows.
  const cacheRef = useRef(new Map<string, Promise<string>>());
  const changesKey = useMemo(
    () => changes.map((c) => c.id).join(","),
    [changes],
  );

  useEffect(() => {
    cacheRef.current = new Map();
  }, [changesKey]);

  const loadArtifact = useCallback<ArtifactLoader>((artifactPath: string) => {
    const existing = cacheRef.current.get(artifactPath);
    if (existing) return existing;
    const pending = fetch(`/api/artifact?path=${encodeURIComponent(artifactPath)}`)
      .then((res) => res.json())
      .then((data: { error?: string; content?: string }) => {
        if (data.error) throw new Error(data.error);
        return cleanText(data.content || "");
      })
      .catch((e) => {
        // Drop failed entry so a later expand can retry.
        cacheRef.current.delete(artifactPath);
        throw e;
      });
    cacheRef.current.set(artifactPath, pending);
    return pending;
  }, []);

  const tree = useMemo(() => buildChangeTree(changes), [changes]);
  const fileCount = useMemo(() => countChangeTreeFiles(tree), [tree]);
  // Small trees: expand all dirs; large: only first level (depth 0 open via CSS/details default).
  const defaultOpenDirs = fileCount <= 30;

  if (!changes.length) {
    return (
      <div className="empty">
        <GitCompareArrows size={22} aria-hidden />
        <p>尚未检测到文件变化</p>
      </div>
    );
  }

  const hasShared = changes.some((c) => c.shared);
  const hasClaimed = changes.some((c) => c.source === "claimed" || c.source === "both");

  return (
    <div className="panel">
      <div className="panel-head">
        <div>
          <span className="eyebrow">CHANGES</span>
          <h2>文件变化</h2>
        </div>
        <span className="count-pill">{changes.length}</span>
      </div>
      <p className="hint change-ledger-hint">
        {hasClaimed
          ? "优先展示本代理工具编辑路径；工作区观测为 turn 起止磁盘差分。"
          : "未从工具解析到写文件路径（例如仅 bash 改盘）；下列为工作区观测。"}
        {hasShared ? " 同目录有其他代理时，「可能交叉」项请人工核对。" : ""}
      </p>
      <div className="change-tree">
        <ChangeTreeNodes
          nodes={tree}
          depth={0}
          defaultOpen={defaultOpenDirs}
          loadArtifact={loadArtifact}
        />
      </div>
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

/* ─── plan / todo panel ─────────────────────────────────────────────────── */

/**
 * Codex-style persistent todo card pinned at the bottom of the conversation
 * tab. Renders the latest Grok plan snapshot: pending / in_progress /
 * completed rows with a progress header. Default expanded, manually collapsible.
 */
function TodoPanel({ plan }: { plan: Plan | null }) {
  const [open, setOpen] = useState(true);
  if (!plan || plan.entries.length === 0) return null;
  const total = plan.entries.length;
  const done = plan.entries.filter((e) => e.status === "completed").length;
  const active = plan.entries.filter((e) => e.status === "in_progress").length;
  return (
    <section className="todo-panel" aria-label="计划">
      <header className="todo-header">
        <ListTodo size={15} className="todo-head-icon" aria-hidden />
        <span className="todo-title">计划</span>
        <span className={`todo-progress${active ? " has-active" : ""}`}>
          {done}/{total} 完成
          {active > 0 && <span className="todo-active"> · {active} 进行中</span>}
        </span>
        <button
          type="button"
          className="icon-btn todo-toggle"
          aria-expanded={open}
          title={open ? "折叠计划" : "展开计划"}
          onClick={() => setOpen(!open)}
        >
          {open ? (
            <ChevronDown size={14} aria-hidden />
          ) : (
            <ChevronUp size={14} aria-hidden />
          )}
        </button>
      </header>
      {open && (
        <ol className="todo-list">
          {plan.entries.map((entry, idx) => (
            <li
              key={`${idx}-${entry.content}`}
              className={`todo-item status-${entry.status}`}
              title={
                entry.status === "completed"
                  ? "已完成"
                  : entry.status === "in_progress"
                    ? "进行中"
                    : "未开始"
              }
            >
              <span className="todo-icon" aria-hidden>
                {entry.status === "completed" ? (
                  <Check size={13} strokeWidth={3} />
                ) : entry.status === "in_progress" ? (
                  <Loader2 size={13} className="todo-spin" />
                ) : (
                  <Square size={12} />
                )}
              </span>
              <span className="todo-content">{entry.content}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
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
  // Narrow viewports use the drawer layout — start collapsed so content is visible.
  const [sidebarOpen, setSidebarOpen] = useState(() => !isNarrowViewport());
  const [theme, setTheme] = useState<ThemeMode>(() => {
    const saved = localStorage.getItem("grok-observer-theme") as ThemeMode | null;
    return saved || "system";
  });
  // true = collapsed; missing key defaults to expanded
  const [collapsedProjects, setCollapsedProjects] = useState<Record<string, boolean>>({});
  const [collapsedSessions, setCollapsedSessions] = useState<Record<string, boolean>>({});
  const [deleteTarget, setDeleteTarget] = useState<Agent | null>(null);
  const [deleteError, setDeleteError] = useState("");
  const [hasNew, setHasNew] = useState(false);
  const [bootError, setBootError] = useState("");
  /** SSE open state for the selected agent (EventSource auto-reconnects). */
  const [streamConnected, setStreamConnected] = useState(false);
  /** Show archived sessions/agents in the sidebar. */
  const [showArchived, setShowArchived] = useState(() => {
    try {
      return localStorage.getItem("grok-observer-show-archived") === "1";
    } catch {
      return false;
    }
  });

  const timelineRef = useRef<HTMLDivElement>(null);
  const autoFollowRef = useRef(true);
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  /** Soft-refresh throttle: last successful schedule time + pending timer. */
  const softRefreshLastRef = useRef(0);
  const softRefreshTimerRef = useRef<number | null>(null);
  /** Catalog (sidebar) bootstrap throttle for event-driven refresh. */
  const catalogRefreshLastRef = useRef(0);
  const catalogRefreshTimerRef = useRef<number | null>(null);
  /** Search hit deep-link target; applied after events load. */
  const pendingScrollRef = useRef<{
    event_id?: number;
    event_seq?: number;
    turn_id?: number;
  } | null>(null);

  const stream = useMemo(
    () => buildStream(events, detail?.turns || []),
    [events, detail?.turns],
  );

  const current = useMemo(
    () => detail?.agent || agents.find((a) => a.id === selected) || null,
    [detail, agents, selected],
  );

  // Collapse tool events once; reuse for live activity (and future consumers).
  const collapsedToolSteps = useMemo(
    () =>
      collapseToolSteps(
        events.filter((e) => isToolEvent(e.type) || e.type === "process"),
      ),
    [events],
  );

  const liveActivity = useMemo(
    () => deriveLiveActivity(current, events, { steps: collapsedToolSteps }),
    [current, events, collapsedToolSteps],
  );

  // Latest plan snapshot (Grok emits full-list plan events) → bottom todo card.
  const plan = useMemo(() => planFromEvents(events), [events]);

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

  /** Throttled sidebar refresh driven by /api/stream/catalog. */
  const scheduleCatalogRefresh = useCallback(
    (force = false) => {
      const run = () => {
        catalogRefreshLastRef.current = Date.now();
        catalogRefreshTimerRef.current = null;
        void bootstrap();
      };
      if (force) {
        if (catalogRefreshTimerRef.current != null) {
          window.clearTimeout(catalogRefreshTimerRef.current);
          catalogRefreshTimerRef.current = null;
        }
        run();
        return;
      }
      const elapsed = Date.now() - catalogRefreshLastRef.current;
      if (elapsed >= SOFT_REFRESH_MS) {
        run();
        return;
      }
      if (catalogRefreshTimerRef.current == null) {
        catalogRefreshTimerRef.current = window.setTimeout(
          run,
          SOFT_REFRESH_MS - elapsed,
        );
      }
    },
    [bootstrap],
  );

  useEffect(() => {
    void bootstrap();
    // Fallback only — primary refresh is catalog SSE (event-driven).
    const timer = window.setInterval(() => void bootstrap(), 60_000);
    const source = new EventSource("/api/stream/catalog");
    source.onmessage = () => {
      scheduleCatalogRefresh(false);
    };
    return () => {
      window.clearInterval(timer);
      source.close();
      if (catalogRefreshTimerRef.current != null) {
        window.clearTimeout(catalogRefreshTimerRef.current);
        catalogRefreshTimerRef.current = null;
      }
    };
  }, [bootstrap, scheduleCatalogRefresh]);

  useEffect(() => {
    try {
      localStorage.setItem("grok-observer-show-archived", showArchived ? "1" : "0");
    } catch {
      /* ignore */
    }
  }, [showArchived]);

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
      setStreamConnected(false);
      return;
    }
    let cancelled = false;
    autoFollowRef.current = true;
    setHasNew(false);
    setStreamConnected(false);
    setTab("conversation");
    softRefreshLastRef.current = 0;
    if (softRefreshTimerRef.current != null) {
      window.clearTimeout(softRefreshTimerRef.current);
      softRefreshTimerRef.current = null;
    }

    /** Soft-refresh agent detail at most once per 1.5s; status-like events force immediate. */
    const softRefreshDetail = (force: boolean) => {
      const run = () => {
        softRefreshLastRef.current = Date.now();
        softRefreshTimerRef.current = null;
        void fetch(`/api/agents/${selected}`)
          .then((r) => (r.ok ? r.json() : null))
          .then((d) => {
            if (d && !cancelled) setDetail(d);
          });
      };
      if (force) {
        if (softRefreshTimerRef.current != null) {
          window.clearTimeout(softRefreshTimerRef.current);
          softRefreshTimerRef.current = null;
        }
        run();
        return;
      }
      const now = Date.now();
      const elapsed = now - softRefreshLastRef.current;
      if (elapsed >= SOFT_REFRESH_MS) {
        run();
        return;
      }
      // Coalesce trailing refresh within the throttle window.
      if (softRefreshTimerRef.current == null) {
        softRefreshTimerRef.current = window.setTimeout(
          run,
          SOFT_REFRESH_MS - elapsed,
        );
      }
    };

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
          // Initial open: jump to bottom after paint (unless deep-linking a search hit).
          if (!pendingScrollRef.current) {
            requestAnimationFrame(() => {
              const el = timelineRef.current;
              if (el) el.scrollTop = el.scrollHeight;
            });
          }
        }
      } catch {
        /* network blip */
      }
    };
    void load();

    const source = new EventSource(`/api/stream?agent_id=${selected}`);
    // Browser EventSource reconnects automatically; only track connected state.
    source.onopen = () => {
      if (!cancelled) setStreamConnected(true);
    };
    source.onerror = () => {
      if (!cancelled) setStreamConnected(false);
    };
    source.onmessage = (message) => {
      if (!cancelled) setStreamConnected(true);
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
        // Soft-refresh agent metadata (status, signoff); throttle noisy SSE.
        softRefreshDetail(STATUS_LIKE_TYPES.has(event.type));
      } catch {
        /* bad event */
      }
    };
    return () => {
      cancelled = true;
      source.close();
      if (softRefreshTimerRef.current != null) {
        window.clearTimeout(softRefreshTimerRef.current);
        softRefreshTimerRef.current = null;
      }
    };
  }, [selected]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("grok-observer-theme", theme);
    // Keep OS native scrollbars in sync with UI (light bg + dark bars is wrong).
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const resolved =
      theme === "light" ? "light" : theme === "dark" ? "dark" : mq.matches ? "dark" : "light";
    document.documentElement.style.colorScheme = resolved;
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

  // After events load, scroll/highlight a search deep-link target if present.
  useEffect(() => {
    const target = pendingScrollRef.current;
    if (!target || !events.length || !selected) return;

    const tryScroll = () => {
      let el: Element | null = null;
      if (target.event_id != null) {
        el = document.querySelector(`[data-event-id="${target.event_id}"]`);
      }
      if (!el && target.event_seq != null) {
        el = document.querySelector(`[data-seq="${target.event_seq}"]`);
      }
      if (!el) return false;

      pendingScrollRef.current = null;
      autoFollowRef.current = false;
      const flashEl =
        (el.closest(".stream-item, .toolchain-group, .row, .msg") as HTMLElement | null) ||
        (el as HTMLElement);
      flashEl.scrollIntoView({
        block: "center",
        behavior: prefersReducedMotion() ? "auto" : "smooth",
      });
      flashEl.classList.add("stream-flash");
      window.setTimeout(() => flashEl.classList.remove("stream-flash"), 2000);
      return true;
    };

    // Two rAFs: wait for stream rows to paint after events state commit.
    let cancelled = false;
    let retryTimer: number | null = null;
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (cancelled) return;
        if (!tryScroll()) {
          // Events may still be streaming in; keep target for a short window.
          retryTimer = window.setTimeout(() => {
            if (!cancelled) tryScroll();
          }, 400);
        }
      });
    });
    return () => {
      cancelled = true;
      if (retryTimer != null) window.clearTimeout(retryTimer);
    };
  }, [events, selected]);

  // Keyboard shortcuts: / search, Escape clear/close, [ toggle sidebar.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      const tag = (t?.tagName || "").toLowerCase();
      const isField =
        tag === "input" ||
        tag === "textarea" ||
        tag === "select" ||
        Boolean(t?.isContentEditable);

      if (e.key === "/" && !isField && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        document.getElementById("global-search")?.focus();
        return;
      }

      if (e.key === "Escape") {
        if (query.trim()) {
          setQuery("");
          return;
        }
        if (isNarrowViewport() && sidebarOpen) {
          setSidebarOpen(false);
          return;
        }
        if (t && typeof t.blur === "function") t.blur();
        return;
      }

      if (e.key === "[" && !isField && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        setSidebarOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [query, sidebarOpen]);

  const onTimelineScroll = (e: React.UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 96;
    autoFollowRef.current = nearBottom;
    if (nearBottom) setHasNew(false);
  };

  const jumpToBottom = useCallback(() => {
    const el = timelineRef.current;
    if (!el) return;
    autoFollowRef.current = true;
    setHasNew(false);
    el.scrollTo({
      top: el.scrollHeight,
      behavior: prefersReducedMotion() ? "auto" : "smooth",
    });
  }, []);

  const selectAgent = (id: string) => {
    location.hash = `#/agents/${id}`;
    setSelected(id);
    setQuery("");
    // On narrow screens, close drawer after pick
    if (isNarrowViewport()) setSidebarOpen(false);
  };

  /** Select agent and optionally deep-link to an event after load. */
  const openSearchHit = (hit: SearchHit) => {
    if (hit.event_id != null || hit.event_seq != null || hit.turn_id != null) {
      pendingScrollRef.current = {
        event_id: hit.event_id,
        event_seq: hit.event_seq,
        turn_id: hit.turn_id,
      };
    } else {
      pendingScrollRef.current = null;
    }
    selectAgent(hit.agent_id);
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

  const patchAgentMeta = async (
    agent: Agent,
    patch: { pinned?: boolean; archived?: boolean; display_title?: string },
  ) => {
    try {
      const res = await fetch(`/api/agents/${agent.id}/meta`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        window.alert(data.error || `更新失败 (${res.status})`);
        return;
      }
      if (data.agent) {
        setAgents((prev) => prev.map((a) => (a.id === agent.id ? { ...a, ...data.agent } : a)));
        setDetail((prev) =>
          prev && prev.agent.id === agent.id
            ? { ...prev, agent: { ...prev.agent, ...data.agent } }
            : prev,
        );
      } else {
        void bootstrap();
      }
    } catch (e) {
      window.alert(String(e));
    }
  };

  const patchTaskMeta = async (
    task: Task,
    patch: { pinned?: boolean; archived?: boolean; title?: string },
  ) => {
    if (task.thread_id.startsWith("_orphan:")) return;
    try {
      const res = await fetch(`/api/tasks/${encodeURIComponent(task.thread_id)}/meta`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        window.alert(data.error || `更新失败 (${res.status})`);
        return;
      }
      if (data.task) {
        setTasks((prev) =>
          prev.map((t) => (t.thread_id === task.thread_id ? { ...t, ...data.task } : t)),
        );
      } else {
        void bootstrap();
      }
    } catch (e) {
      window.alert(String(e));
    }
  };

  const renameAgent = (agent: Agent) => {
    const next = window.prompt("代理显示标题", agentLabel(agent));
    if (next == null) return;
    const title = next.trim();
    if (!title || title === agentLabel(agent)) return;
    void patchAgentMeta(agent, { display_title: title });
  };

  const renameTask = (task: Task) => {
    if (task.thread_id.startsWith("_orphan:")) return;
    const next = window.prompt("会话标题", task.title || "");
    if (next == null) return;
    const title = next.trim();
    if (!title || title === task.title) return;
    void patchTaskMeta(task, { title });
  };

  // Codex-style tree: project (cwd) → session (thread) → subagent
  const tree = useMemo(() => {
    type SessionNode = {
      task: Task;
      agents: Agent[];
      updated_at: string;
      pinned: boolean;
      archived: boolean;
    };
    type ProjectNode = {
      cwdKey: string;
      cwd: string;
      name: string;
      sessions: SessionNode[];
      updated_at: string;
      agentCount: number;
      pinned: boolean;
    };

    const visibleAgents = showArchived
      ? agents
      : agents.filter((a) => !isArchived(a.archived));

    const byThread = new Map<string, Agent[]>();
    for (const a of visibleAgents) {
      const list = byThread.get(a.thread_id) || [];
      list.push(a);
      byThread.set(a.thread_id, list);
    }
    // Pinned agents first, then newest within a session.
    for (const list of byThread.values()) {
      list.sort((a, b) => {
        const pin = Number(isPinned(b.pinned)) - Number(isPinned(a.pinned));
        if (pin) return pin;
        return (b.updated_at || "").localeCompare(a.updated_at || "");
      });
    }

    const projectMap = new Map<string, ProjectNode>();

    const ensureProject = (cwd: string): ProjectNode => {
      const key = workspaceKey(cwd);
      let node = projectMap.get(key);
      if (!node) {
        const label = workspaceLabel(cwd);
        node = {
          cwdKey: key,
          cwd: cwd || "",
          name: label.name,
          sessions: [],
          updated_at: "",
          agentCount: 0,
          pinned: false,
        };
        projectMap.set(key, node);
      } else if (!node.cwd && cwd) {
        // Prefer a real path if we first saw an empty bucket
        node.cwd = cwd;
        node.name = workspaceLabel(cwd).name;
      }
      return node;
    };

    const known = new Set(tasks.map((t) => t.thread_id));
    for (const task of tasks) {
      if (!showArchived && isArchived(task.archived)) continue;
      const kids = byThread.get(task.thread_id) || [];
      if (!kids.length) continue;
      const cwd = task.cwd || kids[0]?.cwd || "";
      const project = ensureProject(cwd);
      let updated = task.updated_at || "";
      for (const a of kids) updated = newerStamp(updated, a.updated_at);
      const sessionPinned = isPinned(task.pinned) || kids.some((a) => isPinned(a.pinned));
      project.sessions.push({
        task: { ...task, cwd: cwd || task.cwd },
        agents: kids,
        updated_at: updated,
        pinned: sessionPinned,
        archived: isArchived(task.archived),
      });
      project.agentCount += kids.length;
      project.updated_at = newerStamp(project.updated_at, updated);
      if (sessionPinned) project.pinned = true;
    }

    // Orphan agents (no task row): one virtual session per workspace
    const orphans = visibleAgents.filter((a) => !known.has(a.thread_id));
    if (orphans.length) {
      const byCwd = new Map<string, Agent[]>();
      for (const a of orphans) {
        const key = workspaceKey(a.cwd);
        const list = byCwd.get(key) || [];
        list.push(a);
        byCwd.set(key, list);
      }
      for (const [, kids] of byCwd) {
        kids.sort((a, b) => {
          const pin = Number(isPinned(b.pinned)) - Number(isPinned(a.pinned));
          if (pin) return pin;
          return (b.updated_at || "").localeCompare(a.updated_at || "");
        });
        const cwd = kids[0]?.cwd || "";
        const project = ensureProject(cwd);
        let updated = "";
        for (const a of kids) updated = newerStamp(updated, a.updated_at);
        const sessionPinned = kids.some((a) => isPinned(a.pinned));
        const fake: Task = {
          thread_id: `_orphan:${workspaceKey(cwd)}`,
          title: "其他代理",
          cwd,
          updated_at: updated,
        };
        project.sessions.push({
          task: fake,
          agents: kids,
          updated_at: updated,
          pinned: sessionPinned,
          archived: false,
        });
        project.agentCount += kids.length;
        project.updated_at = newerStamp(project.updated_at, updated);
        if (sessionPinned) project.pinned = true;
      }
    }

    const projects = [...projectMap.values()];
    for (const p of projects) {
      p.sessions.sort((a, b) => {
        const pin = Number(b.pinned) - Number(a.pinned);
        if (pin) return pin;
        return (b.updated_at || "").localeCompare(a.updated_at || "");
      });
    }
    projects.sort((a, b) => {
      const pin = Number(b.pinned) - Number(a.pinned);
      if (pin) return pin;
      return (b.updated_at || "").localeCompare(a.updated_at || "");
    });
    return projects;
  }, [tasks, agents, showArchived]);

  const archivedCount = useMemo(
    () =>
      agents.filter((a) => isArchived(a.archived)).length +
      tasks.filter((t) => isArchived(t.archived)).length,
    [agents, tasks],
  );

  // Keep the selected agent visible: expand its project + session
  useEffect(() => {
    if (!selected) return;
    const agent = agents.find((a) => a.id === selected);
    if (!agent) return;
    const task = tasks.find((t) => t.thread_id === agent.thread_id);
    const cwd = task?.cwd || agent.cwd || "";
    const projKey = workspaceKey(cwd);
    const sessionKey = task
      ? agent.thread_id
      : `_orphan:${workspaceKey(agent.cwd)}`;
    setCollapsedProjects((m) => (m[projKey] ? { ...m, [projKey]: false } : m));
    setCollapsedSessions((m) => (m[sessionKey] ? { ...m, [sessionKey]: false } : m));
  }, [selected, agents, tasks]);

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
                    key={`${hit.agent_id}-${hit.event_id ?? hit.event_seq ?? i}`}
                    role="option"
                    onClick={() => openSearchHit(hit)}
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

        <div className="list-toolbar">
          <button
            type="button"
            className={`chip-btn ${showArchived ? "active" : ""}`}
            aria-pressed={showArchived}
            title={showArchived ? "隐藏已归档" : "显示已归档"}
            onClick={() => setShowArchived((v) => !v)}
          >
            <Archive size={12} aria-hidden />
            {showArchived ? "归档中" : "归档"}
            {archivedCount > 0 ? ` · ${archivedCount}` : ""}
          </button>
        </div>

        <div className="task-list" role="navigation" aria-label="工作区 / 会话 / 子代理">
          {bootError && (
            <p className="err-text pad">无法连接监督器：{bootError}</p>
          )}
          {!tree.length && !bootError && (
            <p className="hint pad">
              {showArchived ? "暂无代理会话" : "暂无活跃会话（可打开「归档」查看）"}
            </p>
          )}
          {tree.map((project) => {
            const projectCollapsed = !!collapsedProjects[project.cwdKey];
            const pathTitle = project.cwd || "未知工作区";
            return (
              <section
                key={project.cwdKey}
                className={`project-group ${projectCollapsed ? "collapsed" : ""} ${project.pinned ? "is-pinned" : ""}`}
              >
                <button
                  type="button"
                  className="project-toggle"
                  aria-expanded={!projectCollapsed}
                  onClick={() =>
                    setCollapsedProjects((m) => ({
                      ...m,
                      [project.cwdKey]: !projectCollapsed,
                    }))
                  }
                >
                  <ChevronDown size={12} className="tree-chevron" aria-hidden />
                  {projectCollapsed ? (
                    <Folder size={13} aria-hidden />
                  ) : (
                    <FolderOpen size={13} aria-hidden />
                  )}
                  <span className="tree-copy">
                    <strong title={pathTitle}>{project.name}</strong>
                    <span title={pathTitle}>
                      {project.agentCount} 代理 · {project.sessions.length} 会话
                    </span>
                  </span>
                </button>
                {!projectCollapsed &&
                  project.sessions.map(({ task, agents: kids, pinned: sessionPinned, archived: sessionArchived }) => {
                    const sessionCollapsed = !!collapsedSessions[task.thread_id];
                    const isOrphan = task.thread_id.startsWith("_orphan");
                    return (
                      <section
                        key={task.thread_id}
                        className={`session-group ${sessionCollapsed ? "collapsed" : ""} ${sessionPinned ? "is-pinned" : ""} ${sessionArchived ? "is-archived" : ""}`}
                      >
                        <div className="session-row">
                          <button
                            type="button"
                            className="session-toggle"
                            aria-expanded={!sessionCollapsed}
                            onClick={() =>
                              setCollapsedSessions((m) => ({
                                ...m,
                                [task.thread_id]: !sessionCollapsed,
                              }))
                            }
                          >
                            <ChevronDown size={12} className="tree-chevron" aria-hidden />
                            <MessageSquare size={13} aria-hidden />
                            <span className="tree-copy">
                              <strong title={task.title}>
                                {task.title || "未命名会话"}
                              </strong>
                              <span>
                                {kids.length} 代理
                                {sessionArchived ? " · 已归档" : ""}
                                {!isOrphan &&
                                  task.thread_id &&
                                  ` · ${task.thread_id.slice(0, 10)}…`}
                              </span>
                            </span>
                          </button>
                          {!isOrphan && (
                            <div className="row-actions session-actions">
                              <button
                                type="button"
                                className="row-action"
                                title={isPinned(task.pinned) ? "取消置顶会话" : "置顶会话"}
                                aria-label={isPinned(task.pinned) ? "取消置顶会话" : "置顶会话"}
                                onClick={() =>
                                  void patchTaskMeta(task, { pinned: !isPinned(task.pinned) })
                                }
                              >
                                {isPinned(task.pinned) ? <PinOff size={12} /> : <Pin size={12} />}
                              </button>
                              <button
                                type="button"
                                className="row-action"
                                title="重命名会话"
                                aria-label="重命名会话"
                                onClick={() => renameTask(task)}
                              >
                                <Pencil size={12} />
                              </button>
                              <button
                                type="button"
                                className="row-action"
                                title={isArchived(task.archived) ? "取消归档会话" : "归档会话"}
                                aria-label={isArchived(task.archived) ? "取消归档会话" : "归档会话"}
                                onClick={() =>
                                  void patchTaskMeta(task, {
                                    archived: !isArchived(task.archived),
                                  })
                                }
                              >
                                {isArchived(task.archived) ? (
                                  <ArchiveRestore size={12} />
                                ) : (
                                  <Archive size={12} />
                                )}
                              </button>
                            </div>
                          )}
                        </div>
                        {!sessionCollapsed && (
                          <ul className="agent-list">
                            {kids.map((agent) => {
                              const label = agent.signoff_verdict || agent.status;
                              const active = selected === agent.id;
                              const canDelete = !["queued", "running"].includes(
                                agent.status,
                              );
                              const title = agentLabel(agent);
                              return (
                                <li
                                  key={agent.id}
                                  className={`agent-row ${isPinned(agent.pinned) ? "is-pinned" : ""} ${isArchived(agent.archived) ? "is-archived" : ""}`}
                                >
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
                                      <strong title={`${title}\n${agent.name}`}>{title}</strong>
                                      <span>
                                        {STATUS_LABELS[label] || label}
                                        {isArchived(agent.archived) ? " · 归档" : ""}
                                        {" · "}
                                        {formatTime(agent.updated_at)}
                                      </span>
                                    </span>
                                  </a>
                                  <div className="row-actions agent-actions">
                                    <button
                                      type="button"
                                      className="row-action"
                                      title={isPinned(agent.pinned) ? "取消置顶" : "置顶"}
                                      aria-label={isPinned(agent.pinned) ? `取消置顶 ${title}` : `置顶 ${title}`}
                                      onClick={() =>
                                        void patchAgentMeta(agent, {
                                          pinned: !isPinned(agent.pinned),
                                        })
                                      }
                                    >
                                      {isPinned(agent.pinned) ? (
                                        <PinOff size={12} />
                                      ) : (
                                        <Pin size={12} />
                                      )}
                                    </button>
                                    <button
                                      type="button"
                                      className="row-action"
                                      title="重命名"
                                      aria-label={`重命名 ${title}`}
                                      onClick={() => renameAgent(agent)}
                                    >
                                      <Pencil size={12} />
                                    </button>
                                    <button
                                      type="button"
                                      className="row-action"
                                      title={isArchived(agent.archived) ? "取消归档" : "归档"}
                                      aria-label={
                                        isArchived(agent.archived)
                                          ? `取消归档 ${title}`
                                          : `归档 ${title}`
                                      }
                                      onClick={() =>
                                        void patchAgentMeta(agent, {
                                          archived: !isArchived(agent.archived),
                                        })
                                      }
                                    >
                                      {isArchived(agent.archived) ? (
                                        <ArchiveRestore size={12} />
                                      ) : (
                                        <Archive size={12} />
                                      )}
                                    </button>
                                    {canDelete && (
                                      <button
                                        type="button"
                                        className="row-action danger"
                                        title="从观察器移除"
                                        aria-label={`删除 ${title}`}
                                        onClick={() => {
                                          setDeleteError("");
                                          setDeleteTarget(agent);
                                        }}
                                      >
                                        <Trash2 size={12} />
                                      </button>
                                    )}
                                  </div>
                                </li>
                              );
                            })}
                          </ul>
                        )}
                      </section>
                    );
                  })}
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
                <h1 title={`${agentLabel(current)}\n${current.name}`}>
                  {agentLabel(current)}
                </h1>
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

            {["running", "queued"].includes(current.status) && !streamConnected && (
              <div className="stream-conn-banner" role="status" aria-live="polite">
                实时连接中断，正在重连…
              </div>
            )}

            <nav className="tabs" aria-label="主区域">
              {(
                [
                  ["conversation", "对话", <MessageSquare size={14} key="i" />],
                  [
                    "changes",
                    "变更",
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

            {tab === "conversation" && <TodoPanel plan={plan} />}

            <ActivityBar
              activity={liveActivity}
              startedAt={current.created_at}
              onClick={jumpToBottom}
            />
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

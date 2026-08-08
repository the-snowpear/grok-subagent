/**
 * Pure conversation-stream model (testable without mounting the React app).
 */

export type Turn = {
  id: number;
  turn_no: number;
  prompt: string;
  status: string;
  result: string;
  created_at: string;
};

export type Event = {
  id: number;
  seq: number;
  type: string;
  summary: string;
  payload?: string;
  artifact_path?: string;
  created_at: string;
  turn_id?: number | null;
};

export type ToolStep = {
  key: string;
  name: string;
  title: string;
  status: string;
  input?: unknown;
  output?: string;
  events: Event[];
  startedAt?: string;
};

export type StreamItem =
  | { kind: "user"; event: Event; text: string }
  | { kind: "text"; event: Event; text: string }
  | { kind: "thought"; event: Event; text: string }
  | { kind: "toolchain"; key: string; steps: ToolStep[]; events: Event[] }
  | { kind: "diagnostics"; key: string; events: Event[] }
  | { kind: "status"; event: Event; label: string; tone: "ok" | "warn" | "err" | "info" }
  | { kind: "meta"; event: Event; label: string }
  | { kind: "turn_sep"; key: string; turnId: number | null; turnNo?: number };

/** Low-value stream noise — hide entirely from the conversation. */
export const HIDDEN_TYPES = new Set([
  "phase_changed",
  "loop_started",
  "first_token",
  "reasoning",
  "assistant",
  "user_message_chunk",
  "concurrency_warning",
  "rules",
  "permission_requested",
  "permission_resolved",
  "turn_started",
  "turn_ended",
  "turn_completed",
  "usage",
  "context_usage",
  // Plan snapshots render in the persistent todo panel, not the stream.
  "plan",
]);

export const TOOL_TYPES = new Set([
  "tool_call",
  "tool_call_update",
  "tool_started",
  "tool_completed",
  "tool_result",
  "tool_output",
  "terminal",
  "process",
]);

const ANSI_RE =
  // eslint-disable-next-line no-control-regex
  /(?:\u001B\[[0-9;?]*[ -/]*[@-~]|\u001B\][^\u0007]*(?:\u0007|\u001B\\)|\u001B[@-Z\\-_])/g;
// eslint-disable-next-line no-control-regex
const CONTROL_RE = /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g;
const OSC_GARBAGE_RE = /\]\d+;[^\u0007\n]*[\u0007]?/g;

export function cleanText(input: string): string {
  if (!input) return "";
  return input
    .replace(ANSI_RE, "")
    .replace(OSC_GARBAGE_RE, "")
    .replace(CONTROL_RE, "")
    .replace(/\r\n/g, "\n")
    .replace(/\r/g, "\n");
}

export function parsePayload(event: Event): unknown {
  if (!event.payload) return null;
  try {
    return JSON.parse(event.payload);
  } catch {
    return event.payload;
  }
}

export type DiffLineStats = { added: number; removed: number };

/** One visual line in a unified edit diff (snippet-level, not git). */
export type DiffLine = {
  kind: "ctx" | "add" | "del";
  text: string;
};

/** Structured view for expanded edit tools (search_replace / write). */
export type EditDiffView = {
  path: string | null;
  lines: DiffLine[];
  stats: DiffLineStats;
  /** Copyable unified text with leading " "/"+"/"-". */
  unifiedText: string;
};

/** Split into lines; empty string → no lines (not `[""]`). */
export function splitLinesForDiff(text: string): string[] {
  if (text == null || text === "") return [];
  const normalized = String(text).replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  return normalized.split("\n");
}

/** Cap DP so UI thread stays responsive (~500×500). */
function shouldUseEdgeStrip(aLen: number, bLen: number): boolean {
  return aLen > 500 || bLen > 500 || aLen * bLen > 200_000;
}

/**
 * Line-level +/− stats from two text blobs (no git).
 * Uses LCS for small snippets; falls back to common prefix/suffix strip for large files.
 * Matches Codex-style "Edit file +N −M" without reading the working tree.
 */
export function lineDiffStats(oldText: string, newText: string): DiffLineStats {
  return statsFromDiffLines(buildLineDiff(oldText, newText));
}

/**
 * Build unified line diff between two text blobs.
 * Prefer LCS; large inputs use edge-strip (shared prefix/suffix as ctx).
 */
export function buildLineDiff(oldText: string, newText: string): DiffLine[] {
  if (oldText === newText) {
    return splitLinesForDiff(oldText).map((text) => ({ kind: "ctx" as const, text }));
  }
  const a = splitLinesForDiff(oldText);
  const b = splitLinesForDiff(newText);
  if (a.length === 0) return b.map((text) => ({ kind: "add" as const, text }));
  if (b.length === 0) return a.map((text) => ({ kind: "del" as const, text }));
  if (shouldUseEdgeStrip(a.length, b.length)) return edgeStripLineDiff(a, b);
  return lcsLineDiff(a, b);
}

/** O(n) approx: shared prefix/suffix as ctx; middle is all del then all add. */
function edgeStripLineDiff(a: string[], b: string[]): DiffLine[] {
  let i = 0;
  const minLen = Math.min(a.length, b.length);
  while (i < minLen && a[i] === b[i]) i += 1;
  let ja = a.length - 1;
  let jb = b.length - 1;
  while (ja >= i && jb >= i && a[ja] === b[jb]) {
    ja -= 1;
    jb -= 1;
  }
  const lines: DiffLine[] = [];
  for (let k = 0; k < i; k++) lines.push({ kind: "ctx", text: a[k] });
  for (let k = i; k <= ja; k++) lines.push({ kind: "del", text: a[k] });
  for (let k = i; k <= jb; k++) lines.push({ kind: "add", text: b[k] });
  for (let k = ja + 1; k < a.length; k++) lines.push({ kind: "ctx", text: a[k] });
  return lines;
}

/** Full DP table + backtrack → ordered ctx/add/del lines. */
function lcsLineDiff(a: string[], b: string[]): DiffLine[] {
  const n = a.length;
  const m = b.length;
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = 1; i <= n; i++) {
    for (let j = 1; j <= m; j++) {
      if (a[i - 1] === b[j - 1]) dp[i][j] = dp[i - 1][j - 1] + 1;
      else dp[i][j] = dp[i - 1][j] > dp[i][j - 1] ? dp[i - 1][j] : dp[i][j - 1];
    }
  }
  const rev: DiffLine[] = [];
  let i = n;
  let j = m;
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && a[i - 1] === b[j - 1]) {
      rev.push({ kind: "ctx", text: a[i - 1] });
      i -= 1;
      j -= 1;
    } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
      rev.push({ kind: "add", text: b[j - 1] });
      j -= 1;
    } else {
      rev.push({ kind: "del", text: a[i - 1] });
      i -= 1;
    }
  }
  rev.reverse();
  return rev;
}

function statsFromDiffLines(lines: DiffLine[]): DiffLineStats {
  let added = 0;
  let removed = 0;
  for (const line of lines) {
    if (line.kind === "add") added += 1;
    else if (line.kind === "del") removed += 1;
  }
  return { added, removed };
}

function unifiedTextFromLines(lines: DiffLine[]): string {
  return lines
    .map((line) => {
      if (line.kind === "add") return `+${line.text}`;
      if (line.kind === "del") return `-${line.text}`;
      return ` ${line.text}`;
    })
    .join("\n");
}

/**
 * Build a line-level diff view from tool rawInput when it looks like an edit.
 * - search_replace / str_replace: old_string vs new_string
 * - write / create with content + path: all additions
 * Returns null for non-edit shapes (e.g. read_file) so UI can show JSON I/O.
 */
export function editDiffFromInput(input: unknown): EditDiffView | null {
  const rec = asRecord(input);
  if (!rec) return null;

  const path = pickString(rec, "file_path", "path", "filePath", "target_file");

  const oldS = pickString(rec, "old_string", "oldString", "old_str", "before");
  const newS = pickString(rec, "new_string", "newString", "new_str", "after");
  if (oldS != null && newS != null) {
    const lines = buildLineDiff(oldS, newS);
    return {
      path,
      lines,
      stats: statsFromDiffLines(lines),
      unifiedText: unifiedTextFromLines(lines),
    };
  }

  // Full-file write / create: only new body is in the event (no old snapshot → removed=0).
  // Prefer write-shaped fields; avoid read_file (target_file + limit/offset).
  const content = pickString(rec, "contents", "content", "file_text");
  const writePath = pickString(rec, "file_path", "path", "filePath");
  if (content == null || writePath == null) return null;
  if (rec.limit != null || rec.offset != null) return null;
  const bodyLines = splitLinesForDiff(content);
  const lines: DiffLine[] =
    bodyLines.length === 0 && content.length > 0
      ? [{ kind: "add", text: content }]
      : bodyLines.map((text) => ({ kind: "add" as const, text }));
  return {
    path: writePath,
    lines,
    stats: { added: lines.length, removed: 0 },
    unifiedText: unifiedTextFromLines(lines),
  };
}

/**
 * Derive +/− from tool rawInput when present.
 * Delegates to editDiffFromInput so badge stats match the expanded diff pane.
 */
export function editDiffStatsFromInput(input: unknown): DiffLineStats | null {
  const view = editDiffFromInput(input);
  return view ? view.stats : null;
}

function pickString(rec: Record<string, unknown>, ...keys: string[]): string | null {
  for (const k of keys) {
    const v = rec[k];
    if (typeof v === "string") return v;
  }
  return null;
}

/** Convenience: stats for a collapsed tool step when input is an edit. */
export function toolStepDiffStats(step: Pick<ToolStep, "name" | "title" | "input">): DiffLineStats | null {
  if (step.input == null) return null;
  // Prefer any step that carries replace pairs; also allow write tools.
  const fromInput = editDiffStatsFromInput(step.input);
  if (fromInput) return fromInput;
  return null;
}

/** Sum +/− across multi-step toolchains for an aggregate DiffStatsBadge. */
export function sumToolStepDiffStats(
  steps: Pick<ToolStep, "name" | "title" | "input">[],
): DiffLineStats | null {
  let added = 0;
  let removed = 0;
  let any = false;
  for (const step of steps) {
    const d = toolStepDiffStats(step);
    if (!d) continue;
    if (d.added <= 0 && d.removed <= 0) continue;
    added += d.added;
    removed += d.removed;
    any = true;
  }
  return any ? { added, removed } : null;
}

export function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/** Recursively extract readable text from dict/list/string content shapes. */
export function extractContentText(value: unknown, depth = 0): string {
  if (value == null || depth > 8) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) {
    // Grok rawOutput often stores stdout as a byte array (list of ints).
    if (value.length > 0 && value.every((x) => typeof x === "number" && x >= 0 && x <= 255)) {
      try {
        const bytes = Uint8Array.from(value as number[]);
        return new TextDecoder("utf-8", { fatal: false }).decode(bytes);
      } catch {
        return "";
      }
    }
    const parts: string[] = [];
    for (const item of value) {
      const piece = extractContentText(item, depth + 1);
      if (piece) parts.push(piece);
      if (parts.join("\n").length > 8000) break;
    }
    return parts.join("\n");
  }
  const rec = asRecord(value);
  if (!rec) return "";
  for (const key of [
    "text",
    "title",
    "summary",
    "message",
    "output_for_prompt",
    "content",
    "stdout",
    "FileContent",
    "EditsApplied",
    "Result",
    "output",
  ]) {
    if (key in rec) {
      const piece = extractContentText(rec[key], depth + 1);
      if (piece) return piece;
    }
  }
  return "";
}

export function textFromEvent(event: Event): string {
  const data = asRecord(parsePayload(event));
  if (!data) return cleanText(event.summary || "");
  const candidates = [
    data.prompt,
    data.data,
    data.message,
    data.text,
    data.content,
    asRecord(data.content)?.text,
    asRecord(asRecord(data.params)?.update)?.title,
    extractContentText(asRecord(data.params)?.update),
  ];
  for (const c of candidates) {
    if (typeof c === "string" && c.trim()) return cleanText(c);
  }
  return cleanText(event.summary || "");
}

export function isToolEvent(type: string): boolean {
  return TOOL_TYPES.has(type) || type.includes("tool") || type === "terminal";
}

/**
 * Fragments that only attach to an existing step in the current group.
 * Never open a new row alone (avoids post-thought late updates becoming "工具").
 */
const ATTACH_ONLY_TYPES = new Set([
  "tool_output",
  "tool_started",
  "tool_completed",
  "tool_result",
  "tool_call_update",
  "terminal",
]);

const NOISE_TITLE_TOKENS = new Set([
  "tool_result",
  "tool_started",
  "tool_completed",
  "tool_output",
  "tool_call",
  "tool_call_update",
  "terminal",
  "process",
  "工具",
]);

/** Short stable labels only — never full file/command output. */
export function isPlausibleToolTitle(value: string): boolean {
  const t = (value || "").trim();
  if (!t || t.length > 120) return false;
  if (t.includes("\n") || t.includes("\r")) return false;
  if (NOISE_TITLE_TOKENS.has(t)) return false;
  // Line-numbered file dumps from read_file previews.
  if (/^\d+→/.test(t)) return false;
  // Huge prose / markdown blobs.
  if (t.length > 80 && (t.includes("  ") || t.includes("# ") || t.includes("```"))) return false;
  return true;
}

/** Prefer Grok-style human titles (e.g. Read `path`) over bare tool names. */
export function preferTitle(current: string, next: string): string {
  const cur = (current || "").trim();
  const nxt = (next || "").trim();
  const nextOk = isPlausibleToolTitle(nxt);
  const curOk = isPlausibleToolTitle(cur);
  if (!nextOk) return curOk ? cur : cur || "工具";
  if (!curOk) return nxt;
  // Upgrade bare snake_case tool names to "Read `file`" style labels.
  if (nxt.includes("`") && !cur.includes("`")) return nxt;
  if (nxt.includes(" ") && !cur.includes(" ") && nxt.length <= 80) return nxt;
  // Prefer slightly more specific (longer) human titles over one-word names.
  if (nxt.length > cur.length && nxt.length <= 80 && /[A-Z`]/.test(nxt[0] || "")) return nxt;
  return cur;
}

/**
 * Terminal log files are often named `call-<id>[-N].log`.
 * Strip extension and trailing `-<digits>` stream suffix to recover toolCallId.
 */
export function callIdFromLogFile(file: string): string | undefined {
  if (!file || typeof file !== "string") return undefined;
  const base = file
    .replace(/^.*[/\\]/, "")
    .replace(/\.log$/i, "")
    .trim();
  if (!base) return undefined;
  // call-uuid-2 → call-uuid ; also keep fixture-style call-t1-read-0 intact if no uuid pattern
  const stripped = base.replace(/-(\d+)$/, "");
  // Prefer stripped when it still looks like a call id.
  if (stripped.startsWith("call-") && stripped.length >= 10) return stripped;
  if (base.startsWith("call-")) return base;
  return stripped || base;
}

function shortNameCandidate(...parts: unknown[]): string | undefined {
  for (const p of parts) {
    if (typeof p === "string" && isPlausibleToolTitle(p) && !p.includes(" ")) {
      // Prefer snake_case / bare identifiers as "name"
      if (p.length <= 64) return p;
    }
    if (typeof p === "string" && isPlausibleToolTitle(p) && p.length <= 64) return p;
  }
  return undefined;
}

export function toolMeta(event: Event): {
  callId?: string;
  name: string;
  title: string;
  status: string;
  input?: unknown;
  output?: string;
  /** True title came from update.title / label — safe to show. */
  titleTrusted: boolean;
} {
  const root = asRecord(parsePayload(event)) || {};
  // Some events store the update at root; most nest under params.update.
  const nestedUpdate = asRecord(asRecord(root.params)?.update);
  const update =
    nestedUpdate ||
    (root.sessionUpdate || root.toolCallId || root.rawInput != null ? root : {}) ||
    {};
  const metaTool = asRecord(asRecord(update._meta)?.["x.ai/tool"]) || {};

  let callId: string | undefined =
    (typeof update.toolCallId === "string" && update.toolCallId) ||
    (typeof root.tool_call_id === "string" && root.tool_call_id) ||
    (typeof root.toolCallId === "string" && root.toolCallId) ||
    undefined;
  if (!callId && typeof root.file === "string") {
    callId = callIdFromLogFile(root.file);
  }

  const name =
    shortNameCandidate(metaTool.name, root.tool_name, root.toolName) ||
    (typeof update.title === "string" && isPlausibleToolTitle(update.title) ? update.title : undefined) ||
    (event.type === "process" && isPlausibleToolTitle(event.summary) ? event.summary : undefined) ||
    (typeof metaTool.label === "string" && isPlausibleToolTitle(metaTool.label) ? metaTool.label : undefined) ||
    "工具";

  let titleTrusted = false;
  let title = name;
  if (typeof update.title === "string" && isPlausibleToolTitle(update.title)) {
    title = update.title;
    titleTrusted = true;
  } else if (typeof metaTool.label === "string" && isPlausibleToolTitle(metaTool.label)) {
    title = metaTool.label;
    titleTrusted = true;
  } else if (event.type === "process" && isPlausibleToolTitle(event.summary || "")) {
    title = event.summary;
    titleTrusted = true;
  }

  let status = "running";
  if (event.type === "tool_completed" || event.type === "tool_result") status = "done";
  if (typeof update.status === "string") status = update.status;
  if (typeof root.outcome === "string") {
    // success → done for display
    status = root.outcome === "success" ? "done" : String(root.outcome);
  }
  if (event.type === "process") status = "info";

  let input: unknown = update.rawInput ?? root.input ?? root.args;
  let output: string | undefined;
  if (event.type === "tool_result" || event.type === "tool_output") {
    const content = root.content ?? root.text ?? update.rawOutput;
    const extracted = extractContentText(content);
    if (extracted) output = cleanText(extracted);
    else if (typeof content === "string") output = cleanText(content);
    else if (content != null) output = cleanText(JSON.stringify(content, null, 2));
  }
  if (update.rawOutput != null && !output) {
    const extracted = extractContentText(update.rawOutput);
    if (extracted) output = cleanText(extracted);
    else {
      output = cleanText(
        typeof update.rawOutput === "string"
          ? update.rawOutput
          : JSON.stringify(update.rawOutput, null, 2),
      );
    }
  }
  // tool_call_update often carries progressive content as a list of chunks.
  if (!output && update.content != null) {
    const extracted = extractContentText(update.content);
    if (extracted) output = cleanText(extracted);
  }
  return { callId, name, title, status, input, output, titleTrusted };
}

/** Compact status-line event types (must not be swallowed by toolchain folds). */
export const UPDATE_STATUS_TYPES = new Set([
  "pending_update",
  "update_applied",
  "interjection",
  "interrupted",
]);

function statusTone(type: string): "ok" | "warn" | "err" | "info" {
  if (["completed", "accepted", "signoff", "end", "changes", "update_applied"].includes(type)) {
    return "ok";
  }
  if (
    ["partial", "cancelled", "interrupted", "pending_update", "interjection"].includes(type)
  ) {
    return "warn";
  }
  if (["failed", "error", "rejected"].includes(type)) return "err";
  return "info";
}

function isStepOpen(step: ToolStep): boolean {
  const s = step.status;
  return s === "running" || s === "in_progress" || s === "pending" || !s;
}

function appendOutput(step: ToolStep, chunk: string | undefined) {
  if (!chunk) return;
  const text = chunk.trimEnd();
  if (!text) return;
  if (!step.output) {
    step.output = text;
    return;
  }
  // Avoid duplicating identical progressive dumps.
  if (step.output.includes(text) || text.includes(step.output)) {
    if (text.length > step.output.length) step.output = text;
    return;
  }
  // Cap merged terminal lines so one noisy tool doesn't blow the UI.
  const next = `${step.output}\n${text}`;
  step.output = next.length > 12000 ? `${next.slice(0, 12000)}\n…` : next;
}

function findStepByCallId(
  byId: Map<string, ToolStep>,
  callId: string | undefined,
): ToolStep | undefined {
  if (!callId) return undefined;
  const direct = byId.get(callId);
  if (direct) return direct;
  // Prefix / containment match for log file suffixes.
  for (const [key, step] of byId) {
    if (key === callId || key.startsWith(callId) || callId.startsWith(key)) return step;
  }
  return undefined;
}

function findOpenStepByName(steps: ToolStep[], name: string): ToolStep | undefined {
  if (!name || name === "工具") return undefined;
  for (let i = steps.length - 1; i >= 0; i--) {
    const s = steps[i];
    if (!isStepOpen(s)) continue;
    if (s.name === name || s.title === name) return s;
  }
  return undefined;
}

/**
 * Merge related tool events into discrete steps (Grok Build-like: 1 call ≈ 1 row).
 * Attach-only fragments without a matching call are dropped (no e-${id} inflation).
 */
export function collapseToolSteps(events: Event[]): ToolStep[] {
  const steps: ToolStep[] = [];
  const byId = new Map<string, ToolStep>();

  const mergeInto = (step: ToolStep, event: Event, meta: ReturnType<typeof toolMeta>) => {
    step.events.push(event);
    if (meta.name && meta.name !== "工具" && meta.name !== event.type) {
      // Keep stable tool name; don't overwrite with noise.
      if (!isPlausibleToolTitle(step.name) || step.name === "工具") step.name = meta.name;
    }
    if (meta.titleTrusted || isPlausibleToolTitle(meta.title)) {
      step.title = preferTitle(step.title, meta.title);
    }
    if (meta.input != null) step.input = meta.input;
    if (meta.output) appendOutput(step, meta.output);
    if (meta.status && meta.status !== "running") step.status = meta.status;
    if (event.type === "tool_completed" || event.type === "tool_result") {
      step.status = meta.status === "running" ? "done" : meta.status || "done";
    }
  };

  for (const event of events) {
    const meta = toolMeta(event);
    const t = event.type;

    // process (e.g. "启动 Grok Build") → single info row, no callId needed
    if (t === "process") {
      const key = `process-${event.id}`;
      const step: ToolStep = {
        key,
        name: meta.name,
        title: isPlausibleToolTitle(meta.title) ? meta.title : "进程",
        status: "info",
        input: meta.input,
        output: meta.output,
        events: [event],
        startedAt: event.created_at,
      };
      steps.push(step);
      continue;
    }

    // Attach-only: never open a new step without a primary call id match
    if (ATTACH_ONLY_TYPES.has(t)) {
      let step = findStepByCallId(byId, meta.callId);
      if (!step && (t === "tool_started" || t === "tool_completed")) {
        step = findOpenStepByName(steps, meta.name);
      }
      if (!step) continue; // drop orphan fragments
      mergeInto(step, event, meta);
      continue;
    }

    // Primary path: only tool_call opens a new row (Grok Build: 1 call ≈ 1 row)
    if (t === "tool_call") {
      const key = meta.callId || `orphan-${event.id}`;
      let step = meta.callId ? findStepByCallId(byId, meta.callId) : undefined;
      if (!step) {
        step = {
          key,
          name: meta.name,
          title: isPlausibleToolTitle(meta.title) ? meta.title : meta.name,
          status: meta.status,
          input: meta.input,
          output: meta.output,
          events: [event],
          startedAt: event.created_at,
        };
        if (meta.callId) byId.set(meta.callId, step);
        steps.push(step);
      } else {
        mergeInto(step, event, meta);
      }
      continue;
    }
    // else: drop unknown tool-ish noise
  }
  return steps;
}

/**
 * Grok Build style aggregate labels for a collapsed toolchain, e.g.
 * "Read 3 files · Searched 4 patterns · Ran 2 commands".
 * Categories are derived from tool name / title (not free-form prose).
 */
export type ToolAggregateKind =
  | "read"
  | "search"
  | "edit"
  | "list"
  | "run"
  | "fetch"
  | "other";

export function classifyToolStep(step: Pick<ToolStep, "name" | "title">): ToolAggregateKind {
  const name = (step.name || "").toLowerCase();
  const title = (step.title || "").toLowerCase();
  const blob = `${name} ${title}`;
  // Order matters: edit before search (search_replace must not count as search).
  if (
    name === "search_replace" ||
    /\b(search_replace|str_replace|apply_patch|create_file|delete_file)\b/.test(blob) ||
    title.startsWith("edit `") ||
    /^edit\b/.test(title)
  ) {
    return "edit";
  }
  if (
    name === "grep" ||
    /\b(grep|keyword_search|semantic_search|web_search|x_keyword_search|x_semantic_search)\b/.test(
      blob,
    ) ||
    /\bsearched\b/.test(blob) ||
    name.includes("search") // tool names like web_search; not search_replace (handled above)
  ) {
    return "search";
  }
  if (
    name === "read_file" ||
    name === "open_page" ||
    name === "open_page_with_find" ||
    title.startsWith("read `") ||
    /\bread_file\b/.test(blob)
  ) {
    return "read";
  }
  if (name === "list_dir" || title.startsWith("list `") || /\blist_dir\b/.test(blob)) {
    return "list";
  }
  if (
    name === "run_terminal_command" ||
    /\b(run_terminal_command|bash|shell|terminal)\b/.test(blob) ||
    title.startsWith("run ") ||
    title.startsWith("execute `") ||
    title === "run command"
  ) {
    return "run";
  }
  if (/\b(web_fetch|fetch_url)\b/.test(blob) || name === "web_fetch") {
    return "fetch";
  }
  if (name === "process" || step.name === "process") {
    return "run";
  }
  return "other";
}

const AGGREGATE_LABEL: Record<
  ToolAggregateKind,
  { one: string; many: (n: number) => string }
> = {
  read: { one: "Read 1 file", many: (n) => `Read ${n} files` },
  search: { one: "Searched 1 pattern", many: (n) => `Searched ${n} patterns` },
  edit: { one: "Edited 1 file", many: (n) => `Edited ${n} files` },
  list: { one: "Listed 1 path", many: (n) => `Listed ${n} paths` },
  run: { one: "Ran 1 command", many: (n) => `Ran ${n} commands` },
  fetch: { one: "Fetched 1 url", many: (n) => `Fetched ${n} urls` },
  other: { one: "1 tool", many: (n) => `${n} tools` },
};

/** Preferred display order in collapsed summary (Grok-ish). */
const AGGREGATE_ORDER: ToolAggregateKind[] = [
  "read",
  "search",
  "edit",
  "list",
  "run",
  "fetch",
  "other",
];

/**
 * Build collapsed toolchain summary text.
 * - 0 steps → ""
 * - 1 step → that step's short title (more specific than "Read 1 file")
 * - N steps → "Read 3 files · Searched 4 patterns · …"
 */
export function summarizeToolchain(steps: ToolStep[]): string {
  if (!steps.length) return "";
  if (steps.length === 1) {
    return steps[0].title || steps[0].name || "1 tool";
  }
  const counts = new Map<ToolAggregateKind, number>();
  for (const step of steps) {
    // Skip pure process banner from aggregation noise when mixed with real tools.
    if (step.status === "info" && /启动|process|grok build/i.test(step.title || step.name || "")) {
      continue;
    }
    const kind = classifyToolStep(step);
    counts.set(kind, (counts.get(kind) || 0) + 1);
  }
  if (!counts.size) {
    return steps.length === 1
      ? steps[0].title || "1 tool"
      : `${steps.length} tools`;
  }
  const parts: string[] = [];
  for (const kind of AGGREGATE_ORDER) {
    const n = counts.get(kind);
    if (!n) continue;
    const label = AGGREGATE_LABEL[kind];
    parts.push(n === 1 ? label.one : label.many(n));
  }
  return parts.join(" · ");
}

/**
 * Build a conversation stream: consecutive tools merge into a toolchain,
 * but thoughts / messages always break the group (they stay peer-level).
 * Compact turn separators prevent "no tools" confusion after auto-scroll.
 */
export function buildStream(events: Event[], turns: Turn[] = []): StreamItem[] {
  const items: StreamItem[] = [];
  let toolBuf: Event[] = [];
  let diagnosticBuf: Event[] = [];
  const turnNoById = new Map(turns.map((t) => [t.id, t.turn_no]));
  let lastTurnId: number | null | undefined = undefined;

  const flushTools = () => {
    if (!toolBuf.length) return;
    const steps = collapseToolSteps(toolBuf);
    // All fragments may have been attach-only drops — skip empty groups.
    if (steps.length) {
      items.push({
        kind: "toolchain",
        key: `tc-${toolBuf[0].id}-${toolBuf[toolBuf.length - 1].id}`,
        steps,
        events: toolBuf,
      });
    }
    toolBuf = [];
  };

  const flushDiagnostics = () => {
    if (!diagnosticBuf.length) return;
    items.push({
      kind: "diagnostics",
      key: `diag-${diagnosticBuf[0].id}-${diagnosticBuf[diagnosticBuf.length - 1].id}`,
      events: diagnosticBuf,
    });
    diagnosticBuf = [];
  };

  const maybeTurnSep = (event: Event) => {
    const tid = event.turn_id ?? null;
    if (lastTurnId === undefined) {
      lastTurnId = tid;
      if (tid != null) {
        items.push({
          kind: "turn_sep",
          key: `turn-sep-${tid}-start`,
          turnId: tid,
          turnNo: turnNoById.get(tid),
        });
      }
      return;
    }
    if (tid !== lastTurnId) {
      flushTools();
      flushDiagnostics();
      lastTurnId = tid;
      items.push({
        kind: "turn_sep",
        key: `turn-sep-${tid ?? "none"}-${event.id}`,
        turnId: tid,
        turnNo: tid != null ? turnNoById.get(tid) : undefined,
      });
    }
  };

  for (const event of events) {
    const t = event.type;
    if (HIDDEN_TYPES.has(t)) continue;
    if (t === "observer_monitor_error") {
      flushTools();
      flushDiagnostics();
      maybeTurnSep(event);
      items.push({
        kind: "meta",
        event,
        label: cleanText(event.summary || t).slice(0, 240),
      });
      continue;
    }

    if (isToolEvent(t)) {
      flushDiagnostics();
      maybeTurnSep(event);
      toolBuf.push(event);
      continue;
    }
    flushTools();

    if (t === "diagnostic") {
      maybeTurnSep(event);
      diagnosticBuf.push(event);
      continue;
    }
    flushDiagnostics();
    maybeTurnSep(event);

    if (t === "user") {
      items.push({ kind: "user", event, text: textFromEvent(event) });
      continue;
    }
    if (t === "text") {
      items.push({ kind: "text", event, text: textFromEvent(event) });
      continue;
    }
    if (t === "thought" || t === "agent_thought_chunk") {
      items.push({ kind: "thought", event, text: textFromEvent(event) });
      continue;
    }
    if (
      [
        "completed",
        "failed",
        "cancelled",
        "interrupted",
        "signoff",
        "end",
        "changes",
        "error",
        "pending_update",
        "update_applied",
        "interjection",
      ].includes(t)
    ) {
      // flushTools already ran — these stay as peer status rows, never inside a toolchain.
      items.push({
        kind: "status",
        event,
        label: cleanText(event.summary || t),
        tone: statusTone(t),
      });
      continue;
    }
    items.push({
      kind: "meta",
      event,
      label: cleanText(event.summary || t).slice(0, 240),
    });
  }
  flushTools();
  flushDiagnostics();
  return items;
}

/* ── live activity (CLI-style bottom status) ─────────────────────────────── */

export type LivePhase =
  | "queued"
  | "thinking"
  | "tool"
  | "responding"
  | "failed"
  | "idle";

export type LiveActivityTone = "queued" | "thinking" | "tool" | "responding" | "err";

export type LiveActivity = {
  phase: LivePhase;
  label: string;
  detail?: string;
  tone: LiveActivityTone;
  visible: boolean;
};

const IDLE_ACTIVITY: LiveActivity = {
  phase: "idle",
  label: "",
  tone: "queued",
  visible: false,
};

/**
 * CLI-style live status for the selected agent (Grok Build / Claude Code bottom bar).
 * Only visible while queued or running; terminal agent states hide the bar.
 * Pass `options.steps` when the caller already collapsed tool events (avoids double work).
 */
export function deriveLiveActivity(
  agent: { status: string } | null | undefined,
  events: Event[],
  options?: { steps?: ToolStep[] },
): LiveActivity {
  if (!agent) return IDLE_ACTIVITY;
  const status = (agent.status || "").toLowerCase();

  if (status === "queued") {
    return {
      phase: "queued",
      label: "排队中",
      tone: "queued",
      visible: true,
    };
  }

  // Only active while the subagent is running (plan: hide when completed/failed/etc.)
  if (status !== "running") {
    return IDLE_ACTIVITY;
  }

  // Prefer an open tool step from the collapsed model (same as timeline).
  // Reuse precomputed steps when the UI already collapsed them for the stream.
  const steps =
    options?.steps ??
    collapseToolSteps(events.filter((e) => isToolEvent(e.type) || e.type === "process"));
  for (let i = steps.length - 1; i >= 0; i--) {
    const step = steps[i];
    if (step.status === "info") continue;
    if (isStepOpen(step)) {
      const label = (step.title || step.name || "工具").trim() || "工具";
      return {
        phase: "tool",
        label,
        tone: "tool",
        visible: true,
      };
    }
  }

  // No open tool → phase from the most recent meaningful event.
  for (let i = events.length - 1; i >= 0; i--) {
    const t = events[i].type;
    if (HIDDEN_TYPES.has(t)) continue;
    if (t === "diagnostic" || t === "observer_monitor_error") continue;
    if (t === "thought" || t === "agent_thought_chunk") {
      return {
        phase: "thinking",
        label: "思考中",
        tone: "thinking",
        visible: true,
      };
    }
    if (t === "text") {
      return {
        phase: "responding",
        label: "回复中",
        tone: "responding",
        visible: true,
      };
    }
    if (t === "user") {
      return {
        phase: "thinking",
        label: "运行中",
        tone: "thinking",
        visible: true,
      };
    }
    if (t === "process") {
      return {
        phase: "thinking",
        label: "启动中…",
        tone: "thinking",
        visible: true,
      };
    }
    // Completed tool / status noise → still running agent means thinking next.
    if (isToolEvent(t) || ["completed", "failed", "cancelled", "signoff", "end", "changes", "error"].includes(t)) {
      break;
    }
  }

  return {
    phase: "thinking",
    label: "运行中",
    tone: "thinking",
    visible: true,
  };
}

/* ── plan / todo snapshot (Codex-style persistent todo panel) ────────────── */

export type PlanStatus = "pending" | "in_progress" | "completed";

export type PlanEntry = {
  content: string;
  status: PlanStatus;
  priority?: string;
};

export type Plan = {
  entries: PlanEntry[];
  updatedAt?: string;
};

/** Normalize a Grok plan entry status; unknown values degrade to pending. */
function normalizePlanStatus(value: unknown): PlanStatus {
  const s = String(value || "").toLowerCase();
  if (s === "in_progress" || s === "running" || s === "active") {
    return "in_progress";
  }
  if (s === "completed" || s === "done") {
    return "completed";
  }
  return "pending";
}

/**
 * Pull the entry list out of a stored plan payload.
 * Known shapes: params.update.entries (updates.jsonl), update.entries,
 * or a bare top-level entries array (flattened streaming variants).
 */
function planEntriesFromPayload(payload: unknown): PlanEntry[] | null {
  const rec = asRecord(payload);
  if (!rec) return null;
  const params = asRecord(rec.params);
  const update = asRecord(params?.update);
  let entries: unknown = null;
  if (update && Array.isArray(update.entries)) entries = update.entries;
  else if (params && Array.isArray(params.entries)) entries = params.entries;
  else if (Array.isArray(rec.entries)) entries = rec.entries;
  if (!Array.isArray(entries)) return null;
  const out: PlanEntry[] = [];
  for (const raw of entries) {
    const entry = asRecord(raw);
    if (!entry) continue;
    const content = typeof entry.content === "string" ? entry.content.trim() : "";
    if (!content) continue;
    out.push({
      content,
      status: normalizePlanStatus(entry.status),
      priority: typeof entry.priority === "string" ? entry.priority : undefined,
    });
  }
  return out.length ? out : null;
}

/**
 * Latest plan snapshot for an agent's event stream. Grok emits full-list
 * snapshots, so the most recent `plan` event always wins — no diffing needed.
 * Returns null when no plan was ever created (or the last snapshot is empty).
 */
export function planFromEvents(events: Event[]): Plan | null {
  let plan: Plan | null = null;
  for (const event of events) {
    if (event.type !== "plan") continue;
    const entries = planEntriesFromPayload(parsePayload(event));
    plan = entries ? { entries, updatedAt: event.created_at } : null;
  }
  return plan;
}

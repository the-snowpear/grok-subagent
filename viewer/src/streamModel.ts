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

export function toolMeta(event: Event): {
  callId?: string;
  name: string;
  title: string;
  status: string;
  input?: unknown;
  output?: string;
} {
  const root = asRecord(parsePayload(event)) || {};
  const update = asRecord(asRecord(root.params)?.update) || {};
  const metaTool = asRecord(asRecord(update._meta)?.["x.ai/tool"]) || {};
  const callId =
    (typeof update.toolCallId === "string" && update.toolCallId) ||
    (typeof root.tool_call_id === "string" && root.tool_call_id) ||
    (typeof root.toolCallId === "string" && root.toolCallId) ||
    undefined;
  const name =
    (typeof metaTool.name === "string" && metaTool.name) ||
    (typeof root.tool_name === "string" && root.tool_name) ||
    (typeof root.toolName === "string" && root.toolName) ||
    (typeof update.title === "string" && update.title) ||
    event.summary ||
    event.type;
  const title =
    (typeof update.title === "string" && update.title) ||
    (typeof metaTool.label === "string" && metaTool.label) ||
    name;
  let status = "running";
  if (event.type === "tool_completed" || event.type === "tool_result") status = "done";
  if (typeof update.status === "string") status = update.status;
  if (typeof root.outcome === "string") status = root.outcome;
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
  return { callId, name, title, status, input, output };
}

function statusTone(type: string): "ok" | "warn" | "err" | "info" {
  if (["completed", "accepted", "signoff", "end", "changes"].includes(type)) return "ok";
  if (["partial", "cancelled"].includes(type)) return "warn";
  if (["failed", "error", "rejected"].includes(type)) return "err";
  return "info";
}

/** Merge related tool events into discrete steps (by call id when available). */
export function collapseToolSteps(events: Event[]): ToolStep[] {
  const steps: ToolStep[] = [];
  const byId = new Map<string, ToolStep>();

  for (const event of events) {
    const meta = toolMeta(event);
    const key = meta.callId || `e-${event.id}`;
    let step = byId.get(key);
    if (!step) {
      step = {
        key,
        name: meta.name,
        title: meta.title,
        status: meta.status,
        input: meta.input,
        output: meta.output,
        events: [event],
        startedAt: event.created_at,
      };
      byId.set(key, step);
      steps.push(step);
    } else {
      step.events.push(event);
      if (meta.name && meta.name !== event.type) step.name = meta.name;
      if (meta.title) step.title = meta.title;
      if (meta.input != null) step.input = meta.input;
      if (meta.output) step.output = meta.output;
      if (meta.status && meta.status !== "running") step.status = meta.status;
      if (event.type === "tool_completed" || event.type === "tool_result") {
        step.status = meta.status || "done";
      }
    }
  }
  return steps;
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
    items.push({
      kind: "toolchain",
      key: `tc-${toolBuf[0].id}-${toolBuf[toolBuf.length - 1].id}`,
      steps: collapseToolSteps(toolBuf),
      events: toolBuf,
    });
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
    if (["completed", "failed", "cancelled", "signoff", "end", "changes", "error"].includes(t)) {
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

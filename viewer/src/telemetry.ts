import "./telemetry.css";

type ObserverEvent = {
  id?: number;
  seq: number;
  type: string;
  payload?: string | null;
  summary?: string;
  created_at?: string;
};

type Usage = {
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
  reasoning: number;
  total: number;
};

type TelemetryState = {
  agentId: string;
  lastSeq: number;
  calls: number;
  cumulative: Usage;
  latest: Usage;
  hasPerResponseUsage: boolean;
  source: "usage" | "end" | "none";
};

const DEFAULT_CONTEXT_WINDOW = 1_000_000;
const EMPTY_USAGE: Usage = {
  input: 0,
  output: 0,
  cacheRead: 0,
  cacheWrite: 0,
  reasoning: 0,
  total: 0,
};

let state: TelemetryState = freshState("");
let stream: EventSource | null = null;
let root: HTMLElement | null = null;
let popover: HTMLElement | null = null;
let activePanel: "tokens" | "context" | null = null;

function freshState(agentId: string): TelemetryState {
  return {
    agentId,
    lastSeq: 0,
    calls: 0,
    cumulative: { ...EMPTY_USAGE },
    latest: { ...EMPTY_USAGE },
    hasPerResponseUsage: false,
    source: "none",
  };
}

function parseAgentId(): string {
  const match = location.hash.match(/agents\/([^/?#]+)/);
  return match?.[1] ? decodeURIComponent(match[1]) : "";
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function parsePayload(event: ObserverEvent): Record<string, unknown> | null {
  if (!event.payload) return null;
  try {
    return asRecord(JSON.parse(event.payload));
  } catch {
    return null;
  }
}

function numeric(rec: Record<string, unknown> | null, ...keys: string[]): number {
  if (!rec) return 0;
  for (const key of keys) {
    const value = rec[key];
    if (typeof value === "number" && Number.isFinite(value)) return Math.max(0, value);
    if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) {
      return Math.max(0, Number(value));
    }
  }
  return 0;
}

function usageFromRecord(rec: Record<string, unknown> | null): Usage | null {
  if (!rec) return null;
  const input = numeric(rec, "input_tokens", "inputTokens", "input");
  const output = numeric(rec, "output_tokens", "outputTokens", "output");
  const cacheRead = numeric(
    rec,
    "cache_read_input_tokens",
    "cached_input_tokens",
    "cacheReadInputTokens",
    "cachedInputTokens",
    "cache_read_tokens",
  );
  const cacheWrite = numeric(
    rec,
    "cache_creation_input_tokens",
    "cache_write_input_tokens",
    "cacheCreationInputTokens",
    "cacheWriteInputTokens",
    "cache_creation_tokens",
  );
  const reasoning = numeric(rec, "reasoning_tokens", "reasoningTokens", "reasoning_output_tokens");
  let total = numeric(rec, "total_tokens", "totalTokens", "total");
  if (!total) total = input + cacheRead + cacheWrite + output;
  if (input + output + cacheRead + cacheWrite + reasoning + total === 0) return null;
  return { input, output, cacheRead, cacheWrite, reasoning, total };
}

function usageFromEvent(event: ObserverEvent): Usage | null {
  const payload = parsePayload(event);
  if (!payload) return null;
  const direct = usageFromRecord(asRecord(payload.usage));
  if (direct) return direct;
  const data = asRecord(payload.data);
  const nested = usageFromRecord(asRecord(data?.usage));
  if (nested) return nested;
  return usageFromRecord(payload);
}

function addUsage(target: Usage, next: Usage): Usage {
  return {
    input: target.input + next.input,
    output: target.output + next.output,
    cacheRead: target.cacheRead + next.cacheRead,
    cacheWrite: target.cacheWrite + next.cacheWrite,
    reasoning: target.reasoning + next.reasoning,
    total: target.total + next.total,
  };
}

function processEvent(event: ObserverEvent): void {
  if (!event || typeof event.seq !== "number") return;
  state.lastSeq = Math.max(state.lastSeq, event.seq);
  if (event.type === "usage") {
    const usage = usageFromEvent(event);
    if (!usage) return;
    state.latest = usage;
    state.cumulative = addUsage(state.cumulative, usage);
    state.calls += 1;
    state.hasPerResponseUsage = true;
    state.source = "usage";
    render();
    return;
  }
  if (event.type === "end" && !state.hasPerResponseUsage) {
    const usage = usageFromEvent(event);
    if (!usage) return;
    state.latest = usage;
    state.cumulative = usage;
    state.calls = Math.max(state.calls, 1);
    state.source = "end";
    render();
  }
}

function contextWindow(): number {
  const configured = Number(localStorage.getItem("grok-observer-context-window") || "");
  return Number.isFinite(configured) && configured > 0 ? configured : DEFAULT_CONTEXT_WINDOW;
}

function promptTokens(usage: Usage): number {
  return usage.input + usage.cacheRead + usage.cacheWrite;
}

function cacheHit(usage: Usage): number {
  const prompt = promptTokens(usage);
  return prompt > 0 ? (usage.cacheRead / prompt) * 100 : 0;
}

function compact(value: number): string {
  if (!Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  if (abs >= 1_000_000) return `${(value / 1_000_000).toFixed(abs >= 10_000_000 ? 1 : 2).replace(/\.0+$/, "")}M`;
  if (abs >= 1_000) return `${(value / 1_000).toFixed(abs >= 100_000 ? 0 : 1).replace(/\.0$/, "")}k`;
  return Math.round(value).toLocaleString();
}

function full(value: number): string {
  return Math.round(value).toLocaleString("en-US");
}

function pct(value: number): string {
  if (!Number.isFinite(value)) return "0%";
  if (value >= 99.95) return "100%";
  if (value >= 10) return `${value.toFixed(1)}%`;
  return `${value.toFixed(2)}%`;
}

function ensureRoot(): HTMLElement | null {
  if (root?.isConnected) return root;
  root = document.getElementById("observer-telemetry-host");
  if (!root) {
    root = document.createElement("div");
    root.id = "observer-telemetry-host";
    document.body.appendChild(root);
  }
  root.className = "observer-telemetry";
  return root;
}

function metricButton(kind: "tokens" | "context", html: string, label: string): string {
  return `<button type="button" class="telemetry-metric" data-panel="${kind}" aria-label="${label}">${html}</button>`;
}

function render(): void {
  const mount = ensureRoot();
  if (!mount) return;
  if (!state.agentId) {
    mount.hidden = true;
    closePopover();
    return;
  }
  mount.hidden = false;
  const total = contextWindow();
  const used = Math.min(promptTokens(state.latest), total);
  const usedPct = total > 0 ? (used / total) * 100 : 0;
  mount.innerHTML = [
    metricButton("tokens", `<span class="telemetry-arrow">↑</span><strong>${compact(state.cumulative.input)}</strong>`, "查看输入 Token 详情"),
    metricButton("tokens", `<span class="telemetry-arrow">↓</span><strong>${compact(state.cumulative.output)}</strong>`, "查看输出 Token 详情"),
    `<span class="telemetry-sep" aria-hidden="true"></span>`,
    metricButton("tokens", `<strong>${compact(state.cumulative.cacheRead)}</strong><span class="telemetry-label">cache</span>`, "查看缓存 Token 详情"),
    `<span class="telemetry-sep" aria-hidden="true"></span>`,
    metricButton(
      "context",
      `<span class="telemetry-ring" style="--telemetry-pct:${Math.min(100, usedPct)}"></span><strong>${pct(usedPct)}</strong><span class="telemetry-label">/${compact(total)}</span>`,
      "查看 Context 使用详情",
    ),
  ].join("");
  mount.querySelectorAll<HTMLButtonElement>("[data-panel]").forEach((button) => {
    button.addEventListener("click", (clickEvent) => {
      clickEvent.stopPropagation();
      const panel = button.dataset.panel === "context" ? "context" : "tokens";
      if (activePanel === panel && popover?.isConnected) closePopover();
      else openPopover(panel, button);
    });
  });
  if (activePanel && popover?.isConnected) updatePopover(activePanel);
}

function openPopover(panel: "tokens" | "context", anchor: HTMLElement): void {
  closePopover();
  activePanel = panel;
  popover = document.createElement("section");
  popover.className = "telemetry-popover";
  popover.setAttribute("role", "dialog");
  popover.setAttribute("aria-label", panel === "context" ? "Context 构成" : "Token 用量详情");
  document.body.appendChild(popover);
  updatePopover(panel);
  positionPopover(anchor);
}

function closePopover(): void {
  popover?.remove();
  popover = null;
  activePanel = null;
}

function positionPopover(anchor: HTMLElement): void {
  if (!popover) return;
  const rect = anchor.getBoundingClientRect();
  const width = Math.min(520, Math.max(340, window.innerWidth - 20));
  popover.style.width = `${width}px`;
  const left = Math.max(10, Math.min(window.innerWidth - width - 10, rect.right - width));
  popover.style.left = `${left}px`;
  popover.style.top = `${Math.min(window.innerHeight - 20, rect.bottom + 8)}px`;
}

function usageRow(label: string, value: number, hint = ""): string {
  return `<div class="telemetry-row"><span>${label}${hint ? `<small>${hint}</small>` : ""}</span><strong>${full(value)}</strong></div>`;
}

function updatePopover(panel: "tokens" | "context"): void {
  if (!popover) return;
  if (panel === "tokens") {
    const aggregatePrompt = promptTokens(state.cumulative);
    const latestPrompt = promptTokens(state.latest);
    popover.innerHTML = `
      <header class="telemetry-popover-head">
        <div><span class="telemetry-kicker">TOKEN USAGE</span><h2>Token 用量</h2></div>
        <span class="telemetry-pill">${state.calls} calls</span>
      </header>
      <div class="telemetry-total"><span>处理总量</span><strong>${full(state.cumulative.total || aggregatePrompt + state.cumulative.output)}</strong></div>
      <div class="telemetry-grid telemetry-grid-2">
        <div class="telemetry-card"><span>↑ Input</span><strong>${compact(state.cumulative.input)}</strong><small>未缓存输入</small></div>
        <div class="telemetry-card"><span>↓ Output</span><strong>${compact(state.cumulative.output)}</strong><small>模型输出</small></div>
        <div class="telemetry-card"><span>Cache read</span><strong>${compact(state.cumulative.cacheRead)}</strong><small>${pct(cacheHit(state.cumulative))} hit</small></div>
        <div class="telemetry-card"><span>Reasoning</span><strong>${compact(state.cumulative.reasoning)}</strong><small>思考 token</small></div>
      </div>
      <div class="telemetry-section-title">累计明细</div>
      <div class="telemetry-rows">
        ${usageRow("未缓存输入", state.cumulative.input)}
        ${usageRow("缓存读取", state.cumulative.cacheRead)}
        ${usageRow("缓存写入", state.cumulative.cacheWrite)}
        ${usageRow("输出", state.cumulative.output)}
        ${usageRow("Reasoning", state.cumulative.reasoning, "（通常已包含在输出统计口径中）")}
      </div>
      <div class="telemetry-section-title">最近一次模型调用</div>
      <div class="telemetry-rows">
        ${usageRow("Prompt context", latestPrompt)}
        ${usageRow("Output", state.latest.output)}
        ${usageRow("Cache read", state.latest.cacheRead)}
        ${usageRow("Reasoning", state.latest.reasoning)}
      </div>
      <footer class="telemetry-foot">数据源：Grok streaming-json ${state.source === "end" ? "end.usage（兼容回退）" : "usage（逐模型调用）"}</footer>
    `;
    return;
  }

  const total = contextWindow();
  const latestPrompt = promptTokens(state.latest);
  const used = Math.min(latestPrompt, total);
  const free = Math.max(0, total - used);
  const usedPct = total > 0 ? (used / total) * 100 : 0;
  const segments = [
    ["context-input", state.latest.input],
    ["context-cache-read", state.latest.cacheRead],
    ["context-cache-write", state.latest.cacheWrite],
  ] as const;
  let cursor = 0;
  const stops: string[] = [];
  for (const [name, value] of segments) {
    const start = total > 0 ? (cursor / total) * 100 : 0;
    cursor += value;
    const end = total > 0 ? Math.min(100, (cursor / total) * 100) : 0;
    if (end > start) stops.push(`var(--${name}) ${start}% ${end}%`);
  }
  if (usedPct < 100) stops.push(`var(--context-free) ${usedPct}% 100%`);
  const gradient = stops.length ? `linear-gradient(90deg, ${stops.join(",")})` : "var(--context-free)";

  popover.innerHTML = `
    <header class="telemetry-popover-head">
      <div><span class="telemetry-kicker">CONTEXT</span><h2>Context 构成</h2></div>
      <span class="telemetry-pill telemetry-pill-context">${pct(usedPct)}</span>
    </header>
    <div class="telemetry-context-total">
      <span>当前模型调用 Prompt</span>
      <strong>${full(latestPrompt)} / ${full(total)} <small>(${pct(usedPct)})</small></strong>
    </div>
    <div class="telemetry-context-bar" style="background:${gradient}"></div>
    <div class="telemetry-context-legend">
      ${legend("context-input", "新输入", state.latest.input)}
      ${legend("context-cache-read", "缓存读取", state.latest.cacheRead)}
      ${legend("context-cache-write", "缓存写入", state.latest.cacheWrite)}
      ${legend("context-free", "剩余", free)}
    </div>
    <div class="telemetry-context-note">
      <strong>口径说明</strong>
      <span>这里的 Context 占比使用最近一次 <code>usage</code> 的 prompt buckets：uncached + cache read + cache creation。不会把整场会话的累计 token 当成当前上下文。</span>
    </div>
    <footer class="telemetry-foot">Context window 默认 ${compact(DEFAULT_CONTEXT_WINDOW)}；可在浏览器 localStorage 的 <code>grok-observer-context-window</code> 覆盖。系统提示词 / Skills / Tool definitions 等语义拆分需要 ACP session/info，Headless streaming-json 本身不提供这些分类。</footer>
  `;
}

function legend(variable: string, label: string, value: number): string {
  return `<div class="telemetry-legend-item"><span class="telemetry-dot" style="background:var(--${variable})"></span><span>${label}</span><strong>${compact(value)}</strong></div>`;
}

async function loadHistory(agentId: string): Promise<void> {
  let after = 0;
  for (let page = 0; page < 20; page += 1) {
    const response = await fetch(`/api/events?agent_id=${encodeURIComponent(agentId)}&after=${after}`);
    if (!response.ok) throw new Error(`events ${response.status}`);
    const body = (await response.json()) as { events?: ObserverEvent[] };
    const events = Array.isArray(body.events) ? body.events : [];
    for (const event of events) processEvent(event);
    if (events.length === 0) break;
    after = events[events.length - 1]?.seq || after;
    if (events.length < 1000) break;
  }
}

function connectStream(agentId: string): void {
  stream?.close();
  stream = new EventSource(`/api/stream?agent_id=${encodeURIComponent(agentId)}&after=${state.lastSeq}`);
  stream.onmessage = (message) => {
    try {
      processEvent(JSON.parse(message.data) as ObserverEvent);
    } catch {
      // Ignore malformed observer rows; the main viewer handles its own stream.
    }
  };
}

async function selectAgent(): Promise<void> {
  const agentId = parseAgentId();
  if (agentId === state.agentId && stream) return;
  stream?.close();
  stream = null;
  state = freshState(agentId);
  render();
  if (!agentId) return;
  try {
    await loadHistory(agentId);
  } catch {
    // Live stream can still populate telemetry for a new agent.
  }
  connectStream(agentId);
  render();
}

function boot(): void {
  const observer = new MutationObserver(() => {
    if (!root?.isConnected && ensureRoot()) render();
  });
  observer.observe(document.body, { childList: true, subtree: true });
  window.addEventListener("hashchange", () => void selectAgent());
  window.addEventListener("resize", () => {
    if (!popover || !activePanel) return;
    const anchor = document.querySelector<HTMLElement>(`.telemetry-metric[data-panel="${activePanel}"]`);
    if (anchor) positionPopover(anchor);
  });
  document.addEventListener("click", (event) => {
    const target = event.target as Node | null;
    if (target && (popover?.contains(target) || root?.contains(target))) return;
    closePopover();
  });
  void selectAgent();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot, { once: true });
else boot();

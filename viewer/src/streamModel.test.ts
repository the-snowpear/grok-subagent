import { describe, expect, it } from "vitest";
import {
  buildLineDiff,
  buildStream,
  callIdFromLogFile,
  classifyToolStep,
  collapseToolSteps,
  deriveLiveActivity,
  editDiffFromInput,
  editDiffStatsFromInput,
  extractContentText,
  isPlausibleToolTitle,
  lineDiffStats,
  planFromEvents,
  preferTitle,
  sumToolStepDiffStats,
  summarizeToolchain,
  toolMeta,
  toolStepDiffStats,
  type Event,
  type ToolStep,
  type Turn,
} from "./streamModel";

function ev(partial: Partial<Event> & Pick<Event, "id" | "type" | "summary">): Event {
  return {
    seq: partial.id,
    created_at: "2026-07-12T00:00:00Z",
    turn_id: 1,
    ...partial,
  };
}

function toolCall(
  id: number,
  callId: string,
  title: string,
  extra: Record<string, unknown> = {},
): Event {
  return ev({
    id,
    type: "tool_call",
    summary: title,
    payload: JSON.stringify({
      params: {
        update: {
          sessionUpdate: "tool_call",
          toolCallId: callId,
          title,
          ...extra,
        },
      },
    }),
  });
}

describe("extractContentText", () => {
  it("reads nested list content used by tool_call_update", () => {
    const text = extractContentText([
      { type: "content", content: { type: "text", text: "from-list" } },
    ]);
    expect(text).toContain("from-list");
  });

  it("decodes rawOutput byte arrays", () => {
    const bytes = Array.from(new TextEncoder().encode("hello-bytes"));
    const text = extractContentText({
      type: "Bash",
      output: bytes,
      output_for_prompt: "hello-bytes",
    });
    expect(text).toContain("hello-bytes");
  });
});

describe("deriveLiveActivity (CLI bottom status)", () => {
  it("hides when no agent or completed", () => {
    expect(deriveLiveActivity(null, []).visible).toBe(false);
    expect(deriveLiveActivity({ status: "completed" }, []).visible).toBe(false);
    expect(deriveLiveActivity({ status: "failed" }, []).visible).toBe(false);
  });

  it("queued shows 排队中", () => {
    const a = deriveLiveActivity({ status: "queued" }, []);
    expect(a.visible).toBe(true);
    expect(a.phase).toBe("queued");
    expect(a.label).toBe("排队中");
  });

  it("running + open tool → tool phase with title", () => {
    const events = [
      toolCall(1, "c1", "search_replace", {
        rawInput: { file_path: "a.ts", old_string: "x", new_string: "y" },
        _meta: { "x.ai/tool": { name: "search_replace", label: "Edit" } },
      }),
      ev({
        id: 2,
        type: "tool_call_update",
        summary: "edit",
        payload: JSON.stringify({
          params: {
            update: {
              toolCallId: "c1",
              title: "Edit `a.ts`",
              status: "in_progress",
              rawInput: { file_path: "a.ts", old_string: "x", new_string: "y" },
              _meta: { "x.ai/tool": { name: "search_replace", label: "Edit" } },
            },
          },
        }),
      }),
    ];
    const a = deriveLiveActivity({ status: "running" }, events);
    expect(a.visible).toBe(true);
    expect(a.phase).toBe("tool");
    expect(a.label).toMatch(/Edit|a\.ts|search_replace/);
    expect(a.tone).toBe("tool");
  });

  it("running + thought only → thinking", () => {
    const events = [
      ev({ id: 1, type: "thought", summary: "hmm", payload: JSON.stringify({ text: "plan" }) }),
    ];
    const a = deriveLiveActivity({ status: "running" }, events);
    expect(a.phase).toBe("thinking");
    expect(a.label).toBe("思考中");
    expect(a.visible).toBe(true);
  });

  it("running + assistant text → responding", () => {
    const events = [
      ev({ id: 1, type: "text", summary: "hi", payload: JSON.stringify({ text: "hello" }) }),
    ];
    const a = deriveLiveActivity({ status: "running" }, events);
    expect(a.phase).toBe("responding");
    expect(a.label).toBe("回复中");
  });

  it("after tool completes, thought returns thinking", () => {
    const events = [
      toolCall(1, "c1", "read_file", {
        _meta: { "x.ai/tool": { name: "read_file" } },
      }),
      ev({
        id: 2,
        type: "tool_call_update",
        summary: "done",
        payload: JSON.stringify({
          params: {
            update: {
              toolCallId: "c1",
              title: "Read `x`",
              status: "completed",
            },
          },
        }),
      }),
      ev({ id: 3, type: "thought", summary: "next" }),
    ];
    const a = deriveLiveActivity({ status: "running" }, events);
    expect(a.phase).toBe("thinking");
    expect(a.label).toBe("思考中");
  });

  it("reuses precomputed steps option without re-collapsing", () => {
    const events = [
      toolCall(1, "c1", "search_replace", {
        rawInput: { file_path: "a.ts", old_string: "x", new_string: "y" },
        _meta: { "x.ai/tool": { name: "search_replace", label: "Edit" } },
      }),
      ev({
        id: 2,
        type: "tool_call_update",
        summary: "edit",
        payload: JSON.stringify({
          params: {
            update: {
              toolCallId: "c1",
              title: "Edit `a.ts`",
              status: "in_progress",
            },
          },
        }),
      }),
    ];
    const steps = collapseToolSteps(events);
    // Empty events + precomputed steps still reports the open tool.
    const a = deriveLiveActivity({ status: "running" }, [], { steps });
    expect(a.phase).toBe("tool");
    expect(a.visible).toBe(true);
    expect(a.label).toMatch(/Edit|a\.ts|search_replace/);
  });
});

describe("lineDiffStats (Codex-style +/− without git)", () => {
  it("counts pure replace on one line as +1 −1", () => {
    expect(lineDiffStats("a", "b")).toEqual({ added: 1, removed: 1 });
  });

  it("counts multi-line insert/delete via LCS", () => {
    const oldT = "keep\nold1\nold2\nkeep-end";
    const newT = "keep\nnew1\nkeep-end";
    // LCS: keep, keep-end → removed 2, added 1
    expect(lineDiffStats(oldT, newT)).toEqual({ added: 1, removed: 2 });
  });

  it("treats empty old as all additions", () => {
    expect(lineDiffStats("", "x\ny")).toEqual({ added: 2, removed: 0 });
  });

  it("reads search_replace rawInput", () => {
    expect(
      editDiffStatsFromInput({
        file_path: "hello.txt",
        old_string: "a\nb",
        new_string: "a\nc\nd",
      }),
    ).toEqual({ added: 2, removed: 1 });
  });

  it("write tool shows +lines only", () => {
    expect(
      editDiffStatsFromInput({
        file_path: "new.ts",
        content: "line1\nline2\nline3",
      }),
    ).toEqual({ added: 3, removed: 0 });
  });

  it("ignores read_file-shaped input", () => {
    expect(
      editDiffStatsFromInput({
        target_file: "a.ts",
        limit: 50,
        content: "should not count as write when limit present without path write shape",
      }),
    ).toBeNull();
  });

  it("toolStepDiffStats uses step.input", () => {
    const stats = toolStepDiffStats({
      name: "search_replace",
      title: "Edit `hello.txt`",
      input: { file_path: "hello.txt", old_string: "a", new_string: "b\nc" },
    });
    expect(stats).toEqual({ added: 2, removed: 1 });
  });

  it("sumToolStepDiffStats aggregates multi-step edits", () => {
    const steps: ToolStep[] = [
      {
        key: "1",
        name: "search_replace",
        title: "Edit `a`",
        status: "done",
        events: [],
        input: { file_path: "a", old_string: "x", new_string: "y\nz" },
      },
      {
        key: "2",
        name: "search_replace",
        title: "Edit `b`",
        status: "done",
        events: [],
        input: { file_path: "b", old_string: "a\nb", new_string: "c" },
      },
      {
        key: "3",
        name: "read_file",
        title: "Read `c`",
        status: "done",
        events: [],
        input: { target_file: "c", limit: 10 },
      },
    ];
    // first: +2 −1 ; second: +1 −2 → total +3 −3
    expect(sumToolStepDiffStats(steps)).toEqual({ added: 3, removed: 3 });
    expect(sumToolStepDiffStats([])).toBeNull();
  });
});

describe("editDiffFromInput (expanded edit pane)", () => {
  it("builds ctx/del/add lines for search_replace", () => {
    const view = editDiffFromInput({
      file_path: "hello.txt",
      old_string: "keep\nold-a\nold-b\nkeep-end",
      new_string: "keep\nnew-mid\nkeep-end",
    });
    expect(view).not.toBeNull();
    expect(view!.path).toBe("hello.txt");
    expect(view!.stats).toEqual({ added: 1, removed: 2 });
    expect(view!.lines.map((l) => `${l.kind}:${l.text}`)).toEqual([
      "ctx:keep",
      "del:old-a",
      "del:old-b",
      "add:new-mid",
      "ctx:keep-end",
    ]);
    expect(view!.unifiedText).toContain("-old-a");
    expect(view!.unifiedText).toContain("+new-mid");
    // Badge stats stay in sync with the expanded view.
    expect(
      editDiffStatsFromInput({
        file_path: "hello.txt",
        old_string: "keep\nold-a\nold-b\nkeep-end",
        new_string: "keep\nnew-mid\nkeep-end",
      }),
    ).toEqual(view!.stats);
  });

  it("write tool is all additions", () => {
    const view = editDiffFromInput({
      file_path: "new.ts",
      content: "line1\nline2\nline3",
    });
    expect(view).not.toBeNull();
    expect(view!.stats).toEqual({ added: 3, removed: 0 });
    expect(view!.lines.every((l) => l.kind === "add")).toBe(true);
    expect(view!.unifiedText.split("\n")).toEqual(["+line1", "+line2", "+line3"]);
  });

  it("returns null for read_file-shaped input", () => {
    expect(
      editDiffFromInput({
        target_file: "a.ts",
        limit: 50,
        content: "should not count as write when limit present without path write shape",
      }),
    ).toBeNull();
  });

  it("identical old/new is all context with zero stats", () => {
    const view = editDiffFromInput({
      path: "x",
      old_string: "a\nb",
      new_string: "a\nb",
    });
    expect(view!.stats).toEqual({ added: 0, removed: 0 });
    expect(view!.lines).toEqual([
      { kind: "ctx", text: "a" },
      { kind: "ctx", text: "b" },
    ]);
  });

  it("buildLineDiff matches lineDiffStats counts", () => {
    const oldT = "a\nb\nc";
    const newT = "a\nx\nc";
    const lines = buildLineDiff(oldT, newT);
    const added = lines.filter((l) => l.kind === "add").length;
    const removed = lines.filter((l) => l.kind === "del").length;
    expect(lineDiffStats(oldT, newT)).toEqual({ added, removed });
  });
});

describe("summarizeToolchain (Grok Build aggregates)", () => {
  const step = (name: string, title: string): ToolStep => ({
    key: name + title,
    name,
    title,
    status: "done",
    events: [],
  });

  it("classifies search_replace as edit not search", () => {
    expect(classifyToolStep({ name: "search_replace", title: "Edit `a.ts`" })).toBe("edit");
    expect(classifyToolStep({ name: "grep", title: "foo" })).toBe("search");
    expect(classifyToolStep({ name: "read_file", title: "Read `x`" })).toBe("read");
  });

  it("aggregates like Read 3 files · Searched 4 patterns", () => {
    const steps = [
      step("read_file", "Read `a`"),
      step("read_file", "Read `b`"),
      step("read_file", "Read `c`"),
      step("grep", "pattern-a"),
      step("grep", "pattern-b"),
      step("grep", "pattern-c"),
      step("grep", "pattern-d"),
      step("run_terminal_command", "Run Command"),
    ];
    expect(summarizeToolchain(steps)).toBe(
      "Read 3 files · Searched 4 patterns · Ran 1 command",
    );
  });

  it("single step uses the concrete title", () => {
    expect(summarizeToolchain([step("read_file", "Read `README.md`")])).toBe("Read `README.md`");
  });
});

describe("title hygiene", () => {
  it("rejects content-like titles", () => {
    expect(isPlausibleToolTitle("Read `a.txt`")).toBe(true);
    expect(isPlausibleToolTitle("read_file")).toBe(true);
    expect(isPlausibleToolTitle("1→# Hello\nworld")).toBe(false);
    expect(isPlausibleToolTitle("tool_started")).toBe(false);
    expect(isPlausibleToolTitle("x".repeat(200))).toBe(false);
  });

  it("preferTitle upgrades bare name to Read `path`", () => {
    expect(preferTitle("read_file", "Read `README.md`")).toBe("Read `README.md`");
    expect(preferTitle("Read `README.md`", "1→# huge content blob that is not a title")).toBe(
      "Read `README.md`",
    );
  });

  it("callIdFromLogFile strips stream suffix", () => {
    expect(callIdFromLogFile("call-61da9f41-89cf-4800-9304-c2fe5731c144-2.log")).toBe(
      "call-61da9f41-89cf-4800-9304-c2fe5731c144",
    );
  });
});

describe("collapseToolSteps (Grok Build-like)", () => {
  it("merges call + updates + result into one step with short title", () => {
    const steps = collapseToolSteps([
      toolCall(1, "c1", "read_file", {
        rawInput: { target_file: "a.txt" },
        _meta: { "x.ai/tool": { name: "read_file", label: "Read" } },
      }),
      ev({
        id: 2,
        type: "tool_call_update",
        summary: "1→# full file body that must not become the title\nmore lines",
        payload: JSON.stringify({
          params: {
            update: {
              toolCallId: "c1",
              title: "Read `a.txt`",
              status: "completed",
              content: [{ type: "content", content: { type: "text", text: "file body" } }],
            },
          },
        }),
      }),
      ev({
        id: 3,
        type: "tool_result",
        summary: "tool_result",
        payload: JSON.stringify({
          type: "tool_result",
          tool_call_id: "c1",
          content: "file body",
        }),
      }),
    ]);
    expect(steps).toHaveLength(1);
    expect(steps[0].title).toBe("Read `a.txt`");
    expect(steps[0].output).toContain("file body");
    expect(steps[0].status).toMatch(/done|completed|success/);
  });

  it("attaches tool_output log lines via file name call id", () => {
    const callId = "call-61da9f41-89cf-4800-9304-c2fe5731c144";
    const steps = collapseToolSteps([
      toolCall(1, callId, "run_terminal_command", {
        _meta: { "x.ai/tool": { name: "run_terminal_command" } },
      }),
      ev({
        id: 2,
        type: "tool_output",
        summary: "line-a",
        payload: JSON.stringify({
          file: `${callId}-2.log`,
          text: "line-a",
          source: "terminal_log",
        }),
      }),
      ev({
        id: 3,
        type: "tool_output",
        summary: "line-b",
        payload: JSON.stringify({
          file: `${callId}-2.log`,
          text: "line-b",
          source: "terminal_log",
        }),
      }),
    ]);
    expect(steps).toHaveLength(1);
    expect(steps[0].output).toContain("line-a");
    expect(steps[0].output).toContain("line-b");
  });

  it("drops orphan tool_output / tool_started without a matching call", () => {
    const steps = collapseToolSteps([
      ev({
        id: 1,
        type: "tool_output",
        summary: "orphan",
        payload: JSON.stringify({ file: "unknown.log", text: "orphan", source: "terminal_log" }),
      }),
      ev({
        id: 2,
        type: "tool_started",
        summary: "tool_started",
        payload: JSON.stringify({ type: "tool_started", tool_name: "read_file" }),
      }),
    ]);
    expect(steps).toHaveLength(0);
  });

  it("does not let long content summary pollute title", () => {
    const blob = "1→# PROFILE\n\n## 用户角色\n- 主要使用中文";
    const steps = collapseToolSteps([
      toolCall(1, "c9", "read_file", {
        _meta: { "x.ai/tool": { name: "read_file" } },
      }),
      ev({
        id: 2,
        type: "tool_call_update",
        summary: blob,
        payload: JSON.stringify({
          params: {
            update: {
              toolCallId: "c9",
              // no title field — content only
              content: [{ type: "content", content: { type: "text", text: blob } }],
              status: "completed",
            },
          },
        }),
      }),
    ]);
    expect(steps).toHaveLength(1);
    expect(steps[0].title).toBe("read_file");
    expect(steps[0].title).not.toContain("PROFILE");
    expect(steps[0].output).toContain("PROFILE");
  });
});

describe("buildStream toolchain + turn separator", () => {
  it("merges consecutive tools into one toolchain peer to thoughts", () => {
    const events: Event[] = [
      ev({ id: 1, type: "user", summary: "do work", turn_id: 10 }),
      ev({ id: 2, type: "thought", summary: "thinking", turn_id: 10 }),
      ev({
        id: 3,
        type: "tool_call",
        summary: "read_file",
        turn_id: 10,
        payload: JSON.stringify({
          params: {
            update: {
              sessionUpdate: "tool_call",
              toolCallId: "c1",
              title: "read_file",
              rawInput: { target_file: "a.txt" },
              _meta: { "x.ai/tool": { name: "read_file" } },
            },
          },
        }),
      }),
      ev({
        id: 4,
        type: "tool_call_update",
        summary: "done",
        turn_id: 10,
        payload: JSON.stringify({
          params: {
            update: {
              sessionUpdate: "tool_call_update",
              toolCallId: "c1",
              status: "completed",
              content: [{ type: "content", content: { type: "text", text: "file body" } }],
            },
          },
        }),
      }),
      ev({ id: 5, type: "text", summary: "answer", turn_id: 10 }),
    ];
    const stream = buildStream(events, [{ id: 10, turn_no: 1, prompt: "p", status: "completed", result: "", created_at: "" }]);
    const kinds = stream.map((x) => x.kind);
    expect(kinds).toContain("turn_sep");
    expect(kinds).toContain("toolchain");
    expect(kinds).toContain("thought");
    expect(kinds).toContain("user");
    expect(kinds).toContain("text");
    // thought and toolchain are peers (not nested)
    const thoughtIdx = kinds.indexOf("thought");
    const toolIdx = kinds.indexOf("toolchain");
    expect(thoughtIdx).toBeGreaterThan(-1);
    expect(toolIdx).toBeGreaterThan(-1);
    const tc = stream.find((x) => x.kind === "toolchain");
    if (!tc || tc.kind !== "toolchain") throw new Error("missing toolchain");
    expect(tc.steps).toHaveLength(1);
    expect(tc.steps[0].output).toContain("file body");
  });

  it("thought splits toolchains into separate groups", () => {
    const events: Event[] = [
      toolCall(1, "a", "read_file"),
      ev({ id: 2, type: "thought", summary: "mid" }),
      toolCall(3, "b", "list_dir"),
    ];
    const stream = buildStream(events);
    const tools = stream.filter((x) => x.kind === "toolchain");
    expect(tools).toHaveLength(2);
    if (tools[0].kind !== "toolchain" || tools[1].kind !== "toolchain") {
      throw new Error("expected toolchains");
    }
    expect(tools[0].steps).toHaveLength(1);
    expect(tools[1].steps).toHaveLength(1);
    expect(tools[0].steps[0].key).toBe("a");
    expect(tools[1].steps[0].key).toBe("b");
  });

  it("inserts a second turn separator without replaying first-turn tools", () => {
    const turns: Turn[] = [
      { id: 10, turn_no: 1, prompt: "p1", status: "completed", result: "", created_at: "" },
      { id: 20, turn_no: 2, prompt: "p2", status: "completed", result: "", created_at: "" },
    ];
    const events: Event[] = [
      ev({ id: 1, type: "user", summary: "t1", turn_id: 10 }),
      ev({
        id: 2,
        type: "tool_call",
        summary: "edit",
        turn_id: 10,
        payload: JSON.stringify({
          params: { update: { toolCallId: "a", title: "search_replace" } },
        }),
      }),
      ev({ id: 3, type: "user", summary: "t2", turn_id: 20 }),
      ev({
        id: 4,
        type: "tool_call",
        summary: "term",
        turn_id: 20,
        payload: JSON.stringify({
          params: { update: { toolCallId: "b", title: "run_terminal_command" } },
        }),
      }),
      ev({
        id: 5,
        type: "tool_call_update",
        summary: "ok",
        turn_id: 20,
        payload: JSON.stringify({
          params: {
            update: {
              toolCallId: "b",
              status: "completed",
              rawOutput: {
                type: "Bash",
                output: Array.from(new TextEncoder().encode("Ran 1 test\nOK\n")),
                output_for_prompt: "Ran 1 test\nOK\n",
              },
            },
          },
        }),
      }),
    ];
    const stream = buildStream(events, turns);
    const seps = stream.filter((x) => x.kind === "turn_sep");
    expect(seps.length).toBeGreaterThanOrEqual(2);
    expect(seps.map((s) => (s.kind === "turn_sep" ? s.turnNo : null))).toEqual(
      expect.arrayContaining([1, 2]),
    );
    const tools = stream.filter((x) => x.kind === "toolchain");
    expect(tools.length).toBe(2);
    const second = tools[1];
    if (second.kind !== "toolchain") throw new Error("expected toolchain");
    expect(second.steps[0].output || second.steps[0].title).toBeTruthy();
    const out = collapseToolSteps(second.events);
    expect(out[0].output).toContain("OK");
  });

  it("toolMeta surfaces edit input and terminal output", () => {
    const event = ev({
      id: 9,
      type: "tool_call_update",
      summary: "edit",
      payload: JSON.stringify({
        params: {
          update: {
            toolCallId: "e1",
            title: "Edit `hello.txt`",
            status: "completed",
            rawInput: { file_path: "hello.txt", old_string: "a", new_string: "b" },
            rawOutput: { type: "EditsApplied", EditsApplied: { path: "hello.txt", replacements: 1 } },
            _meta: { "x.ai/tool": { name: "search_replace", label: "Edit" } },
          },
        },
      }),
    });
    const meta = toolMeta(event);
    expect(meta.name).toMatch(/search_replace|Edit/);
    expect(meta.input).toMatchObject({ file_path: "hello.txt" });
    expect(meta.output || "").toContain("hello.txt");
    expect(meta.title).toBe("Edit `hello.txt`");
    expect(meta.titleTrusted).toBe(true);
  });

  it("pending_update / update_applied / interjection stay as status peers (not tool-folded)", () => {
    const events: Event[] = [
      toolCall(1, "c1", "read_file"),
      ev({
        id: 2,
        type: "pending_update",
        summary: "等待工具边界后更新 (timeout=30s)",
        payload: JSON.stringify({
          mode: "waiting_tool_boundary",
          requested_mode: "tool_boundary",
          lossless_interject: false,
        }),
      }),
      ev({
        id: 3,
        type: "tool_call_update",
        summary: "done",
        payload: JSON.stringify({
          params: { update: { toolCallId: "c1", status: "completed" } },
        }),
      }),
      ev({
        id: 4,
        type: "interjection",
        summary: "new direction",
        payload: JSON.stringify({ mode: "interrupt_and_resume", lossless_interject: false }),
      }),
      ev({
        id: 5,
        type: "update_applied",
        summary: "工具边界后中断并更新 (turn 2)",
        payload: JSON.stringify({
          mode_used: "interrupt_and_resume",
          trigger: "tool_boundary",
          lossless_interject: false,
        }),
      }),
      ev({ id: 6, type: "interrupted", summary: "当前回合已中断，正在按更新继续" }),
    ];
    const stream = buildStream(events);
    const kinds = stream.map((x) => x.kind);
    // Status rows must appear as peers of toolchain, not inside it.
    expect(kinds.filter((k) => k === "status").length).toBeGreaterThanOrEqual(4);
    expect(kinds).toContain("toolchain");
    const statusLabels = stream
      .filter((x) => x.kind === "status")
      .map((x) => (x.kind === "status" ? x.label : ""));
    expect(statusLabels.some((l) => l.includes("等待工具边界"))).toBe(true);
    expect(statusLabels.some((l) => l.includes("工具边界后中断") || l.includes("update"))).toBe(true);
    // pending_update between tools flushes the first toolchain group.
    const toolchains = stream.filter((x) => x.kind === "toolchain");
    expect(toolchains.length).toBeGreaterThanOrEqual(1);
    const pendingIdx = stream.findIndex(
      (x) => x.kind === "status" && x.event.type === "pending_update",
    );
    const firstToolIdx = stream.findIndex((x) => x.kind === "toolchain");
    expect(pendingIdx).toBeGreaterThan(firstToolIdx);
  });
});

describe("planFromEvents", () => {
  function planEvent(
    id: number,
    entries: Array<{ content: string; status: string; priority?: string }>,
  ): Event {
    return ev({
      id,
      type: "plan",
      summary: "计划：2 项",
      payload: JSON.stringify({
        method: "session/update",
        params: {
          sessionId: "s",
          update: { sessionUpdate: "plan", entries },
        },
      }),
    });
  }

  it("returns null when no plan event exists", () => {
    expect(planFromEvents([ev({ id: 1, type: "thought", summary: "x" })])).toBeNull();
  });

  it("extracts entries from the last plan snapshot (full-snapshot semantics)", () => {
    const events = [
      planEvent(1, [
        { content: "step a", status: "in_progress" },
        { content: "step b", status: "pending" },
      ]),
      planEvent(2, [
        { content: "step a", status: "completed" },
        { content: "step b", status: "completed" },
      ]),
    ];
    const plan = planFromEvents(events);
    expect(plan).not.toBeNull();
    expect(plan!.entries).toEqual([
      { content: "step a", status: "completed", priority: undefined },
      { content: "step b", status: "completed", priority: undefined },
    ]);
    expect(plan!.updatedAt).toBe("2026-07-12T00:00:00Z");
  });

  it("normalizes unknown statuses to pending and keeps priority", () => {
    const plan = planFromEvents([
      planEvent(1, [
        { content: "a", status: "queued", priority: "high" },
        { content: "b", status: "running" },
        { content: "c", status: "done" },
      ]),
    ]);
    expect(plan!.entries.map((e) => e.status)).toEqual([
      "pending",
      "in_progress",
      "completed",
    ]);
    expect(plan!.entries[0].priority).toBe("high");
  });

  it("handles flattened payload shapes and drops empty snapshots", () => {
    const flat = ev({
      id: 1,
      type: "plan",
      summary: "x",
      payload: JSON.stringify({ entries: [{ content: "flat", status: "pending" }] }),
    });
    expect(planFromEvents([flat])!.entries[0].content).toBe("flat");

    const empty = ev({
      id: 2,
      type: "plan",
      summary: "x",
      payload: JSON.stringify({ entries: [] }),
    });
    expect(planFromEvents([empty])).toBeNull();
  });

  it("hides plan snapshots from the conversation stream", () => {
    const items = buildStream([planEvent(1, [{ content: "a", status: "pending" }])]);
    expect(items).toHaveLength(0);
  });
});

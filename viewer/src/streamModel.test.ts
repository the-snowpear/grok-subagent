import { describe, expect, it } from "vitest";
import {
  buildStream,
  collapseToolSteps,
  extractContentText,
  toolMeta,
  type Event,
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
  });
});

import { describe, expect, it } from "vitest";
import { effortLabel, roleLabel } from "./agentMeta";

describe("roleLabel", () => {
  it("maps canonical roles to worker-role labels", () => {
    expect(roleLabel("explore")).toBe("Explorer");
    expect(roleLabel("implement")).toBe("Implementer");
    expect(roleLabel("review")).toBe("Reviewer");
    expect(roleLabel("EXPLORE")).toBe("Explorer");
  });

  it("falls back to title-cased raw value for unknown roles", () => {
    expect(roleLabel("debugger")).toBe("Debugger");
    expect(roleLabel("  custom_role  ")).toBe("Custom_role");
  });

  it("returns null for empty / legacy NULL roles", () => {
    expect(roleLabel(null)).toBeNull();
    expect(roleLabel(undefined)).toBeNull();
    expect(roleLabel("")).toBeNull();
    expect(roleLabel("   ")).toBeNull();
  });
});

describe("effortLabel", () => {
  it("shows configured values verbatim", () => {
    expect(effortLabel("max")).toBe("max");
    expect(effortLabel(" high ")).toBe("high");
  });

  it("returns null when unset", () => {
    expect(effortLabel(null)).toBeNull();
    expect(effortLabel(undefined)).toBeNull();
    expect(effortLabel("")).toBeNull();
  });
});

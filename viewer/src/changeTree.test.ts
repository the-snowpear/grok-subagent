import { describe, expect, it } from "vitest";
import {
  buildChangeTree,
  countChangeTreeFiles,
  extractFileDiff,
  fileNameFromPath,
  normalizePath,
  parseUnifiedDiffLines,
  treePathFromDisplay,
  type ChangeLike,
} from "./changeTree";

function ch(partial: Partial<ChangeLike> & Pick<ChangeLike, "id" | "path">): ChangeLike {
  return {
    kind: "modified",
    ...partial,
  };
}

describe("path helpers", () => {
  it("normalizes separators and ./", () => {
    expect(normalizePath("\\a\\b\\c.ts")).toBe("a/b/c.ts");
    expect(normalizePath("./viewer/src/x")).toBe("viewer/src/x");
  });

  it("rename display uses right-hand path for tree", () => {
    expect(treePathFromDisplay("src/old.ts → src/new.ts")).toBe("src/new.ts");
    expect(fileNameFromPath("src/old.ts → src/new.ts")).toBe("new.ts");
  });
});

describe("buildChangeTree", () => {
  it("groups by project directories", () => {
    const tree = buildChangeTree([
      ch({ id: 1, path: "a/b.ts" }),
      ch({ id: 2, path: "a/c.ts" }),
      ch({ id: 3, path: "d.ts" }),
    ]);
    expect(tree.map((n) => n.name)).toEqual(["a", "d.ts"]);
    const dir = tree[0];
    expect(dir.type).toBe("dir");
    if (dir.type !== "dir") throw new Error("expected dir");
    expect(dir.children.map((c) => c.name)).toEqual(["b.ts", "c.ts"]);
    expect(countChangeTreeFiles(tree)).toBe(3);
  });

  it("hangs rename under target and merges multi-turn same path", () => {
    const tree = buildChangeTree([
      ch({ id: 1, path: "pkg/old.ts → pkg/new.ts", kind: "renamed" }),
      ch({ id: 2, path: "pkg/new.ts", kind: "modified", source: "observed" }),
    ]);
    expect(tree).toHaveLength(1);
    const dir = tree[0];
    expect(dir.type).toBe("dir");
    if (dir.type !== "dir") throw new Error("expected dir");
    expect(dir.children).toHaveLength(1);
    const file = dir.children[0];
    expect(file.type).toBe("file");
    if (file.type !== "file") throw new Error("expected file");
    expect(file.name).toBe("new.ts");
    // compareChanges prefers observed over unsourced rename rows.
    expect(file.changes.map((c) => c.id).sort()).toEqual([1, 2]);
    expect(file.changes).toHaveLength(2);
  });
});

describe("extractFileDiff", () => {
  const multi = [
    "diff --git a/foo.ts b/foo.ts",
    "index 111..222 100644",
    "--- a/foo.ts",
    "+++ b/foo.ts",
    "@@ -1 +1 @@",
    "-old foo",
    "+new foo",
    "diff --git a/bar/baz.ts b/bar/baz.ts",
    "index 333..444 100644",
    "--- a/bar/baz.ts",
    "+++ b/bar/baz.ts",
    "@@ -1 +1 @@",
    "-old bar",
    "+new bar",
    "",
  ].join("\n");

  it("slices multi-file git diff to one path", () => {
    const { text, matched } = extractFileDiff(multi, "bar/baz.ts");
    expect(matched).toBe(true);
    expect(text).toContain("bar/baz.ts");
    expect(text).toContain("+new bar");
    expect(text).not.toContain("foo.ts");
    expect(text).not.toContain("new foo");
  });

  it("matches rename display path on the right side", () => {
    const { text, matched } = extractFileDiff(multi, "x → foo.ts");
    expect(matched).toBe(true);
    expect(text).toContain("+new foo");
  });

  it("returns full text when path missing", () => {
    const { text, matched } = extractFileDiff(multi, "nope.ts");
    expect(matched).toBe(false);
    expect(text).toContain("foo.ts");
    expect(text).toContain("bar/baz.ts");
  });
});

describe("parseUnifiedDiffLines", () => {
  it("colors add/del/meta lines", () => {
    const lines = parseUnifiedDiffLines(
      ["diff --git a/x b/x", "--- a/x", "+++ b/x", "@@ -1 +1 @@", "-a", "+b", " c"].join("\n"),
    );
    expect(lines.map((l) => l.kind)).toEqual([
      "meta",
      "meta",
      "meta",
      "meta",
      "del",
      "add",
      "ctx",
    ]);
  });
});

/**
 * Changes-panel path tree + git unified-diff helpers (pure, testable).
 */

export type ChangeLike = {
  id: number;
  path: string;
  kind: string;
  preexisting?: number;
  diff_artifact?: string;
  source?: string;
  shared?: number;
  tool_name?: string;
  added?: number;
  deleted?: number;
};

export type ChangeTreeNode =
  | { type: "dir"; name: string; path: string; children: ChangeTreeNode[] }
  | {
      type: "file";
      name: string;
      /** Normalized tree path (post-rename target). */
      path: string;
      /** Original ledger path (may be `old → new`). */
      displayPath: string;
      changes: ChangeLike[];
    };

export type ColoredDiffLine = {
  kind: "add" | "del" | "ctx" | "meta";
  text: string;
};

/** Normalize separators and strip leading ./ */
export function normalizePath(path: string): string {
  let p = (path || "").replace(/\\/g, "/").trim();
  while (p.startsWith("./")) p = p.slice(2);
  while (p.startsWith("/")) p = p.slice(1);
  return p;
}

/**
 * Path used for tree placement.
 * Rename display `old → new` hangs under the new path (right-hand side).
 */
export function treePathFromDisplay(displayPath: string): string {
  const raw = (displayPath || "").trim();
  if (raw.includes(" → ")) {
    const parts = raw.split(" → ");
    return normalizePath(parts[parts.length - 1] || raw);
  }
  return normalizePath(raw);
}

/** File basename for tree leaf labels. */
export function fileNameFromPath(displayPath: string): string {
  const tree = treePathFromDisplay(displayPath);
  if (!tree) return displayPath || "(unknown)";
  const i = tree.lastIndexOf("/");
  return i >= 0 ? tree.slice(i + 1) : tree;
}

function sourceRank(source?: string): number {
  return source === "both" ? 0 : source === "claimed" ? 1 : source === "observed" ? 2 : 3;
}

/** Prefer claimed/both, then path — stable ordering before tree insert. */
export function compareChanges(a: ChangeLike, b: ChangeLike): number {
  const sharedA = a.shared ? 1 : 0;
  const sharedB = b.shared ? 1 : 0;
  if (sharedA !== sharedB) return sharedA - sharedB;
  const r = sourceRank(a.source) - sourceRank(b.source);
  if (r !== 0) return r;
  const pa = treePathFromDisplay(a.path).localeCompare(treePathFromDisplay(b.path));
  if (pa !== 0) return pa;
  return (a.id || 0) - (b.id || 0);
}

type MutableDir = {
  name: string;
  path: string;
  dirs: Map<string, MutableDir>;
  files: Map<string, Extract<ChangeTreeNode, { type: "file" }>>;
};

function finalizeDir(dir: MutableDir): ChangeTreeNode[] {
  const out: ChangeTreeNode[] = [];
  const dirNames = [...dir.dirs.keys()].sort((a, b) => a.localeCompare(b));
  for (const name of dirNames) {
    const child = dir.dirs.get(name)!;
    out.push({
      type: "dir",
      name: child.name,
      path: child.path,
      children: finalizeDir(child),
    });
  }
  const files = [...dir.files.values()].sort((a, b) => a.name.localeCompare(b.name));
  for (const f of files) out.push(f);
  return out;
}

/** Build a directory tree from change rows (project-relative paths). */
export function buildChangeTree(changes: ChangeLike[]): ChangeTreeNode[] {
  const root: MutableDir = { name: "", path: "", dirs: new Map(), files: new Map() };
  const sorted = [...changes].sort(compareChanges);

  for (const change of sorted) {
    const treePath = treePathFromDisplay(change.path);
    const parts = treePath.split("/").filter(Boolean);
    if (!parts.length) {
      // Fallback: keep opaque path as a root-level file leaf.
      const key = change.path || `id:${change.id}`;
      const existing = root.files.get(key);
      if (existing) existing.changes.push(change);
      else {
        root.files.set(key, {
          type: "file",
          name: change.path || "(unknown)",
          path: key,
          displayPath: change.path,
          changes: [change],
        });
      }
      continue;
    }

    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const name = parts[i];
      const dirPath = parts.slice(0, i + 1).join("/");
      let next = node.dirs.get(name);
      if (!next) {
        next = { name, path: dirPath, dirs: new Map(), files: new Map() };
        node.dirs.set(name, next);
      }
      node = next;
    }

    const fileName = parts[parts.length - 1];
    const filePath = parts.join("/");
    let leaf = node.files.get(filePath);
    if (!leaf) {
      leaf = {
        type: "file",
        name: fileName,
        path: filePath,
        displayPath: change.path,
        changes: [],
      };
      node.files.set(filePath, leaf);
    }
    leaf.changes.push(change);
  }

  return finalizeDir(root);
}

export function countChangeTreeFiles(nodes: ChangeTreeNode[]): number {
  let n = 0;
  for (const node of nodes) {
    if (node.type === "file") n += node.changes.length;
    else n += countChangeTreeFiles(node.children);
  }
  return n;
}

function stripGitPath(raw: string): string {
  let p = raw.trim();
  if (p.startsWith('"') && p.endsWith('"')) p = p.slice(1, -1);
  // git may use a/foo or b/foo; also /dev/null
  if (p === "/dev/null") return p;
  if (p.startsWith("a/") || p.startsWith("b/")) p = p.slice(2);
  return normalizePath(p);
}

function pathsMatch(candidate: string, target: string): boolean {
  const a = normalizePath(candidate);
  const b = normalizePath(target);
  if (!a || !b) return false;
  if (a === b) return true;
  // Allow absolute-ish or prefix mismatches when one ends with the other.
  if (a.endsWith("/" + b) || b.endsWith("/" + a)) return true;
  return false;
}

function sectionMatchesPath(section: string[], target: string): boolean {
  const header = section[0] || "";
  if (header.startsWith("diff --git ")) {
    // diff --git a/foo b/bar  |  diff --git "a/foo bar" "b/foo bar"
    const rest = header.slice("diff --git ".length);
    const bits = rest.match(/"[^"]+"|[^\s]+/g) || [];
    for (const bit of bits) {
      if (pathsMatch(stripGitPath(bit), target)) return true;
    }
  }
  for (const line of section.slice(0, 20)) {
    if (line.startsWith("--- ") || line.startsWith("+++ ")) {
      const raw = line.slice(4).replace(/\t.*$/, "");
      if (pathsMatch(stripGitPath(raw), target)) return true;
    }
    if (line.startsWith("rename from ") || line.startsWith("rename to ")) {
      const raw = line.replace(/^rename (?:from|to) /, "");
      if (pathsMatch(stripGitPath(raw), target)) return true;
    }
  }
  return false;
}

/**
 * Slice a multi-file `git diff` artifact down to one file.
 * When no section matches, returns the full text with matched=false.
 */
export function extractFileDiff(
  fullDiff: string,
  filePath: string,
): { text: string; matched: boolean } {
  const target = treePathFromDisplay(filePath);
  const normalized = (fullDiff || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  if (!normalized.trim()) return { text: "", matched: false };
  if (!target) return { text: normalized, matched: false };

  const lines = normalized.split("\n");
  const sections: string[][] = [];
  let current: string[] | null = null;
  const preamble: string[] = [];

  for (const line of lines) {
    if (line.startsWith("diff --git ")) {
      if (current) sections.push(current);
      current = [line];
    } else if (current) {
      current.push(line);
    } else {
      preamble.push(line);
    }
  }
  if (current) sections.push(current);

  if (!sections.length) {
    // Not a multi-file git patch — treat whole body as the file diff.
    return { text: normalized, matched: true };
  }

  const hit = sections.filter((sec) => sectionMatchesPath(sec, target));
  if (!hit.length) return { text: normalized, matched: false };
  return { text: hit.map((s) => s.join("\n")).join("\n"), matched: true };
}

/** Map unified git/patch text into colored line kinds for the diff viewer. */
export function parseUnifiedDiffLines(diff: string): ColoredDiffLine[] {
  if (!diff) return [];
  const lines = diff.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
  // Drop a single trailing empty line from split (keep intentional blanks mid-file).
  if (lines.length && lines[lines.length - 1] === "") lines.pop();

  return lines.map((text) => {
    if (
      text.startsWith("diff --git ") ||
      text.startsWith("index ") ||
      text.startsWith("similarity index ") ||
      text.startsWith("rename from ") ||
      text.startsWith("rename to ") ||
      text.startsWith("new file mode ") ||
      text.startsWith("deleted file mode ") ||
      text.startsWith("old mode ") ||
      text.startsWith("new mode ") ||
      text.startsWith("--- ") ||
      text.startsWith("+++ ") ||
      text.startsWith("@@")
    ) {
      return { kind: "meta" as const, text };
    }
    if (text.startsWith("+")) return { kind: "add" as const, text };
    if (text.startsWith("-")) return { kind: "del" as const, text };
    return { kind: "ctx" as const, text };
  });
}

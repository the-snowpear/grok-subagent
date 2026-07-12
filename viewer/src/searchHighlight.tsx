import React from "react";

export type SearchMatch = { start: number; end: number };

/**
 * Safe search highlight: only React text + controlled <mark> nodes.
 * Never parses or injects HTML from the backend.
 */
export function HighlightSnippet({
  text,
  matches,
  query,
}: {
  text: string;
  matches?: SearchMatch[];
  query: string;
}) {
  const parts: React.ReactNode[] = [];
  const ranges =
    matches && matches.length
      ? matches
          .filter((m) => m.end > m.start && m.start >= 0 && m.end <= text.length)
          .sort((a, b) => a.start - b.start)
      : (() => {
          const q = query.trim();
          if (!q) return [] as SearchMatch[];
          const found: SearchMatch[] = [];
          const lower = text.toLowerCase();
          const needle = q.toLowerCase();
          let from = 0;
          while (from < text.length) {
            const idx = lower.indexOf(needle, from);
            if (idx < 0) break;
            found.push({ start: idx, end: idx + needle.length });
            from = idx + Math.max(needle.length, 1);
            if (found.length >= 8) break;
          }
          return found;
        })();

  let cursor = 0;
  ranges.forEach((range, i) => {
    if (range.start < cursor) return;
    if (range.start > cursor) {
      parts.push(
        <React.Fragment key={`t-${i}-${cursor}`}>{text.slice(cursor, range.start)}</React.Fragment>,
      );
    }
    parts.push(<mark key={`m-${i}-${range.start}`}>{text.slice(range.start, range.end)}</mark>);
    cursor = range.end;
  });
  if (cursor < text.length) {
    parts.push(<React.Fragment key="t-end">{text.slice(cursor)}</React.Fragment>);
  }
  if (!parts.length) return <>{text}</>;
  return <>{parts}</>;
}

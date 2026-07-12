import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { HighlightSnippet } from "./searchHighlight";

describe("HighlightSnippet", () => {
  it("never turns payload HTML into real tags", () => {
    const evil =
      'hello <img onerror="alert(1)" src=x> world <script>alert(2)</script> &lt;b&gt;ok';
    const html = renderToStaticMarkup(
      <HighlightSnippet text={evil} query="hello" matches={[{ start: 0, end: 5 }]} />,
    );
    // Text nodes only: angle brackets stay escaped; no real img/script elements.
    expect(html).not.toMatch(/<img\b/i);
    expect(html).not.toMatch(/<script\b/i);
    // Attribute-like text may appear escaped, but must not form a real tag attribute.
    expect(html).not.toMatch(/<[^>]*onerror=/i);
    expect(html).toContain("<mark>hello</mark>");
    expect(html).toContain("&lt;img");
    expect(html).toContain("&lt;script&gt;");
    // Entity characters remain text, not decoded into tags.
    expect(html).toContain("&amp;lt;b&amp;gt;ok");
  });

  it("highlights query matches with controlled mark nodes", () => {
    const html = renderToStaticMarkup(
      <HighlightSnippet text="alpha beta alpha" query="alpha" />,
    );
    expect(html.match(/<mark>/g)?.length).toBe(2);
    expect(html).toContain("<mark>alpha</mark>");
    expect(html).toContain("beta");
  });
});

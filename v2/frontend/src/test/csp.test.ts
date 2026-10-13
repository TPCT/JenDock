// @vitest-environment node
/**
 * Production serves the UI with `Content-Security-Policy: default-src 'self'; frame-ancestors
 * 'none'; base-uri 'none'`: no `unsafe-inline`, so `<style>` elements and `style="..."` attributes
 * in HTML are blocked (CSSOM writes such as `element.style.x = ...` are not).
 *
 * This test builds the app for real and inspects the output:
 *  - `index.html` has no inline `<style>`, `style=` attribute, inline script or event handler;
 *  - all CSS is emitted as files (Primer React ships CSS Modules; nothing is injected at runtime);
 *  - every code path in the bundles that could create a `<style>` element is one of a short list
 *    of known, guarded ones. A new library (or a Primer upgrade) adding another one fails here.
 */
import { mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { build } from "vite";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

let out = "";
let html = "";
let scripts: Record<string, string> = {};
let styles: Record<string, string> = {};

beforeAll(async () => {
  out = mkdtempSync(join(tmpdir(), "shiftcodes-csp-"));
  await build({
    configFile: join(import.meta.dirname, "../../vite.config.ts"),
    logLevel: "silent",
    build: { outDir: out, emptyOutDir: true, write: true },
    publicDir: false,
  });
  html = readFileSync(join(out, "index.html"), "utf8");
  for (const file of readdirSync(join(out, "assets"))) {
    const text = readFileSync(join(out, "assets", file), "utf8");
    if (file.endsWith(".js")) scripts[file] = text;
    if (file.endsWith(".css")) styles[file] = text;
  }
}, 120_000);

afterAll(() => {
  rmSync(out, { recursive: true, force: true });
  scripts = {};
  styles = {};
});

describe("built index.html", () => {
  it("has no inline style, style attribute, inline script or event handler", () => {
    expect(html).not.toMatch(/<style[\s>]/i);
    expect(html).not.toMatch(/\sstyle\s*=/i);
    expect(html).not.toMatch(/\son[a-z]+\s*=/i);
    for (const tag of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
      expect(tag[1]).toMatch(/\ssrc=/);
      expect((tag[2] ?? "").trim()).toBe("");
    }
  });

  it("links its CSS as files", () => {
    expect(html).toMatch(/<link rel="stylesheet"[^>]*href="\/assets\/[^"]+\.css"/);
    expect(Object.keys(styles).length).toBeGreaterThan(0);
  });
});

describe("emitted CSS", () => {
  it("contains Primer's component CSS and design tokens", () => {
    const all = Object.values(styles).join("\n");
    expect(all).toContain("prc-Button-ButtonBase");
    expect(all).toContain("--bgColor-default");
    expect(all).toMatch(/\[data-color-mode=("?)auto\1\]/);
    expect(all).toContain("dark_dimmed");
  });
});

/**
 * Places where bundled code can create a `<style>` element or parse one from a string, and why
 * each is acceptable. Identified by a signature found next to the call.
 */
const KNOWN_STYLE_SITES: { name: string; signature: RegExp; why: string }[] = [
  {
    name: "React DOM hoistable <style href precedence>",
    signature: /precedence/,
    why: "Only runs when the app renders a <style href precedence> element (and in its error messages), which it never does.",
  },
  {
    name: "CodeMirror (style-mod) fallback",
    signature: /styleTag/,
    why: "Used only without adoptedStyleSheets or for a document root; the editor is mounted in a shadow root, where constructed stylesheets are used.",
  },
  {
    name: "@oddbird/popover-polyfill fallback",
    signature: /adoptedStyleSheets=\[[^\]]*\.\.\./,
    why: "Only runs where the Popover API is missing, and then prefers constructed stylesheets; the <style> branch needs a browser with neither.",
  },
  {
    name: "@primer/live-region-element template",
    signature: /aria-live="polite"/,
    why: "A custom element whose shadow-root template has a <style>; it is only created by announce(), which no component this app renders calls (see the runtime guard in vitest.setup.ts).",
  },
];

describe("runtime style injection paths in the JavaScript bundles", () => {
  const PATTERNS = [
    /createElement\((["'`])style\1\)/g,
    /<style[\s>]/g,
    /insertAdjacentHTML/g,
    /dangerouslySetInnerHTML:\s*\{\s*__html/g,
  ];

  it("are all known and documented", () => {
    const unknown: string[] = [];
    const seen = new Set<string>();
    for (const [file, text] of Object.entries(scripts)) {
      for (const pattern of PATTERNS) {
        for (const match of text.matchAll(pattern)) {
          const at = match.index;
          const context = text.slice(Math.max(0, at - 400), at + 400);
          const known = KNOWN_STYLE_SITES.find((site) => site.signature.test(context));
          if (known) seen.add(known.name);
          else unknown.push(`${file}: ...${context.slice(300, 520)}...`);
        }
      }
    }
    expect(unknown.map((entry) => entry.slice(0, 400))).toEqual([]);
    // If a known site disappears from the bundles, drop it from the list above.
    expect([...seen].sort()).toEqual(KNOWN_STYLE_SITES.map((site) => site.name).sort());
  });

  it("never sets the style attribute from a string in application code", () => {
    // style="..." written as an attribute string would be blocked; CSSOM (style.cssText,
    // style.setProperty, React's style prop) is not.
    for (const text of Object.values(scripts)) {
      expect(text).not.toMatch(/setAttribute\((["'`])style\1/);
    }
  });

  it("documents why each site is acceptable", () => {
    for (const site of KNOWN_STYLE_SITES) expect(site.why.length).toBeGreaterThan(20);
  });
});

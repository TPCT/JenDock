import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { installStyleBridge, mirrorInlineStyles } from "./styleBridge";

/** jsdom has no constructed stylesheets; this double records what is adopted. */
class FakeSheet {
  text = "";
  replaceSync(text: string): void {
    this.text = text;
  }
}

describe("installStyleBridge", () => {
  const originalSheet = (globalThis as unknown as { CSSStyleSheet?: unknown }).CSSStyleSheet;
  let restore: () => void = () => undefined;

  beforeEach(() => {
    (globalThis as unknown as { CSSStyleSheet: unknown }).CSSStyleSheet = FakeSheet;
    Object.defineProperty(document, "adoptedStyleSheets", {
      value: [],
      writable: true,
      configurable: true,
    });
    restore = installStyleBridge(document);
  });
  afterEach(() => {
    restore();
    (globalThis as unknown as { CSSStyleSheet: unknown }).CSSStyleSheet = originalSheet;
    Reflect.deleteProperty(document, "adoptedStyleSheets");
  });

  it("hands out an inert placeholder instead of a <style> element", () => {
    const style = document.createElement("style");
    expect(style.tagName).toBe("SPAN");
    expect(style.hidden).toBe(true);
    document.body.append(style);
    expect(document.querySelectorAll("style")).toHaveLength(0);
    style.remove();
  });

  it("mirrors the text into an adopted stylesheet and follows changes", async () => {
    const style = document.createElement("style");
    document.body.append(style);
    style.textContent = ".xterm-rows span { display: inline-block; }";
    await Promise.resolve();
    const adopted = document.adoptedStyleSheets as unknown as FakeSheet[];
    expect(adopted).toHaveLength(1);
    expect(adopted[0]?.text).toBe(".xterm-rows span { display: inline-block; }");
    style.textContent = ".xterm { color: red; }";
    await Promise.resolve();
    expect((document.adoptedStyleSheets as unknown as FakeSheet[])[0]?.text).toBe(
      ".xterm { color: red; }",
    );
    style.remove();
  });

  it("leaves other elements alone and cleans up when uninstalled", () => {
    expect(document.createElement("div").tagName).toBe("DIV");
    document.createElement("style");
    restore();
    expect(document.adoptedStyleSheets).toHaveLength(0);
    expect(document.createElement("style").tagName).toBe("STYLE");
    restore = installStyleBridge(document);
  });
});

describe("mirrorInlineStyles", () => {
  it("copies a style attribute into the CSSOM", async () => {
    const root = document.createElement("div");
    const cell = document.createElement("span");
    root.append(cell);
    document.body.append(root);
    const stop = mirrorInlineStyles(root);
    cell.setAttribute("style", "color: red;");
    await Promise.resolve();
    expect(cell.style.color).toBe("red");
    stop();
    root.remove();
  });
});

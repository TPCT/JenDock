/* eslint-disable @typescript-eslint/no-deprecated -- only the string overload of createElement is wrapped, the one xterm calls */
/**
 * xterm.js builds `<style>` elements at run time (scrollbar colours, cell sizes, the colour
 * theme). The production Content-Security-Policy has no `unsafe-inline` for styles, so such
 * elements are inert. While the terminal page is open, `document.createElement("style")` hands
 * out an inert placeholder element instead (no violation is reported), and a MutationObserver
 * mirrors its text into a constructed stylesheet adopted by the document. Constructed
 * stylesheets are allowed by the policy.
 *
 * Browsers without constructed stylesheets get the untouched behaviour.
 */
export function installStyleBridge(doc: Document = document): () => void {
  const supported =
    typeof CSSStyleSheet !== "undefined" &&
    "replaceSync" in CSSStyleSheet.prototype &&
    "adoptedStyleSheets" in doc;
  if (!supported) return () => undefined;

  const original: (tag: string, options?: ElementCreationOptions) => HTMLElement =
    doc.createElement.bind(doc);
  const sheets = new Map<Element, CSSStyleSheet>();
  const observers = new Set<MutationObserver>();

  const publish = () => {
    const ours = new Set(sheets.values());
    doc.adoptedStyleSheets = [
      ...doc.adoptedStyleSheets.filter((sheet) => !ours.has(sheet)),
      ...ours,
    ];
  };
  const sync = (host: Element) => {
    const sheet = sheets.get(host) ?? new CSSStyleSheet();
    try {
      sheet.replaceSync(host.textContent);
    } catch {
      /* unparsable CSS: keep the previous rules */
    }
    if (!sheets.has(host)) sheets.set(host, sheet);
    publish();
  };

  doc.createElement = (tag: string, options?: ElementCreationOptions) => {
    if (typeof tag === "string" && tag.toLowerCase() === "style") {
      const host = original("span");
      host.hidden = true;
      host.setAttribute("data-style-bridge", "");
      const observer = new MutationObserver(() => {
        sync(host);
      });
      observer.observe(host, { childList: true, characterData: true, subtree: true });
      observers.add(observer);
      sheets.set(host, new CSSStyleSheet());
      return host;
    }
    return original(tag, options);
  };

  return () => {
    // Restore the native method by removing the own property that shadows it.
    Reflect.deleteProperty(doc, "createElement");
    observers.forEach((observer) => {
      observer.disconnect();
    });
    const ours = new Set(sheets.values());
    doc.adoptedStyleSheets = doc.adoptedStyleSheets.filter((sheet) => !ours.has(sheet));
    sheets.clear();
  };
}

/**
 * xterm's DOM renderer colours true-colour cells with `setAttribute("style", "color:#...")`,
 * which the policy blocks. The attribute is still written, so this copies it into the element's
 * CSSOM (`element.style.cssText`), which is allowed. The WebGL renderer, used where available,
 * never needs this.
 */
export function mirrorInlineStyles(root: Element): () => void {
  const apply = (element: Element) => {
    if (!(element instanceof HTMLElement || element instanceof SVGElement)) return;
    const value = element.getAttribute("style") ?? "";
    if (element.style.cssText !== value) element.style.cssText = value;
  };
  const observer = new MutationObserver((records) => {
    for (const record of records) apply(record.target as Element);
  });
  observer.observe(root, { attributes: true, attributeFilter: ["style"], subtree: true });
  return () => {
    observer.disconnect();
  };
}

/**
 * jsdom lacks several platform features Primer React relies on (observers, the Popover API,
 * modal <dialog>). These minimal stand-ins keep behaviour observable in tests; layout is not.
 */
class NoopObserver {
  observe(): void {
    /* no layout in jsdom */
  }
  unobserve(): void {
    /* no layout in jsdom */
  }
  disconnect(): void {
    /* no layout in jsdom */
  }
  takeRecords(): unknown[] {
    return [];
  }
}

export function installJsdomPolyfills(): void {
  const globals = globalThis as Record<string, unknown>;
  globals.ResizeObserver ??= NoopObserver;
  globals.IntersectionObserver ??= NoopObserver;

  const proto = HTMLElement.prototype as unknown as Record<string, unknown>;
  if (typeof proto.showPopover !== "function") {
    // Primer checks `"popover" in HTMLElement.prototype` before loading its own polyfill.
    Object.defineProperty(HTMLElement.prototype, "popover", {
      configurable: true,
      get(this: HTMLElement) {
        return this.getAttribute("popover");
      },
      set(this: HTMLElement, value: string | null) {
        if (value === null) this.removeAttribute("popover");
        else this.setAttribute("popover", value);
      },
    });
    const open = new WeakSet<Element>();
    proto.showPopover = function showPopover(this: HTMLElement) {
      open.add(this);
      this.setAttribute("data-popover-open", "");
    };
    proto.hidePopover = function hidePopover(this: HTMLElement) {
      open.delete(this);
      this.removeAttribute("data-popover-open");
    };
    proto.togglePopover = function togglePopover(this: HTMLElement) {
      if (open.has(this)) (this as unknown as { hidePopover: () => void }).hidePopover();
      else (this as unknown as { showPopover: () => void }).showPopover();
      return open.has(this);
    };
    // eslint-disable-next-line @typescript-eslint/unbound-method -- re-applied with .call below
    const originalMatches = Element.prototype.matches;
    Element.prototype.matches = function matches(this: Element, selector: string) {
      if (selector === ":popover-open") return this.hasAttribute("data-popover-open");
      try {
        return originalMatches.call(this, selector);
      } catch {
        return false;
      }
    };
  }

  const dialog = HTMLDialogElement.prototype as unknown as Record<string, unknown>;
  if (typeof dialog.showModal !== "function") {
    const show = function show(this: HTMLDialogElement) {
      this.setAttribute("open", "");
    };
    dialog.showModal = show;
    dialog.show = show;
    dialog.close = function close(this: HTMLDialogElement) {
      this.removeAttribute("open");
      this.dispatchEvent(new Event("close"));
    };
  }
}

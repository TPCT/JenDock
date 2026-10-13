import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, beforeEach, expect } from "vitest";
import { resetAdminMock } from "./src/mocks/admin";
import { resetContainersMock } from "./src/mocks/containers";
import { resetMockData } from "./src/mocks/data";
import { server } from "./src/mocks/server";
import { setMockAuthenticated } from "./src/mocks/handlers";
import { FakeEventSource } from "./src/test/fakeEventSource";
import { installJsdomPolyfills } from "./src/test/jsdomPolyfills";

// Node-environment suites (the CSP build check) have no DOM.
const hasDom = typeof window !== "undefined";
if (hasDom) {
  installJsdomPolyfills();
  configure({ asyncUtilTimeout: 4000 });
}

beforeAll(() => {
  server.listen({ onUnhandledFrame: "error" });
});
beforeEach(() => {
  if (!hasDom) return;
  setMockAuthenticated(true);
  FakeEventSource.install();
  window.matchMedia = (query: string) =>
    ({
      matches: false,
      media: query,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    }) as unknown as MediaQueryList;
  // CodeMirror measures text with Range geometry, which jsdom does not implement.
  const rect = { x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
  Range.prototype.getBoundingClientRect = () => ({ ...rect, toJSON: () => rect });
  Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
  // eslint-disable-next-line @typescript-eslint/no-deprecated -- CodeMirror probes it for Safari
  document.execCommand = () => false;
  Element.prototype.scrollIntoView = () => undefined;
});
afterEach(() => {
  if (hasDom) {
    // CSP guard (production forbids inline <style>): whatever a test rendered, no component may
    // have put a <style> element into the document or created Primer's <live-region>, whose
    // shadow-root template contains one. See src/test/csp.test.ts.
    expect(document.querySelectorAll("style")).toHaveLength(0);
    expect(document.querySelector("live-region")).toBeNull();
    cleanup();
    window.localStorage.clear();
  }
  server.resetHandlers();
  resetMockData();
  resetAdminMock();
  resetContainersMock();
});
afterAll(() => {
  server.close();
});

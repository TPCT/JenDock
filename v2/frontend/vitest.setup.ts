import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, beforeEach } from "vitest";
import { resetMockData } from "./src/mocks/data";
import { server } from "./src/mocks/server";
import { setMockAuthenticated } from "./src/mocks/handlers";
import { FakeEventSource } from "./src/test/fakeEventSource";

beforeAll(() => {
  server.listen({ onUnhandledFrame: "error" });
});
beforeEach(() => {
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
  cleanup();
  server.resetHandlers();
  resetMockData();
  window.localStorage.clear();
});
afterAll(() => {
  server.close();
});

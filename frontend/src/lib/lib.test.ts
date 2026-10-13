import { describe, expect, it } from "vitest";
import type { LogChunk } from "../api/types";
import { parseAnsi } from "./ansi";
import { formatDuration, formatRelative, isCancellable, shortSha } from "./format";
import { LogStore } from "./logStore";
import { safeNext } from "./next";

const chunk = (offset: number, data: string, complete = false): LogChunk => ({
  offset,
  next_offset: offset + data.length,
  size: data.length,
  text: data,
  complete,
});

describe("format", () => {
  it("formats durations", () => {
    expect(formatDuration(400)).toBe("<1s");
    expect(formatDuration(83_000)).toBe("1m 23s");
    expect(formatDuration(3_900_000)).toBe("1h 5m");
    expect(formatDuration(-1)).toBe("-");
  });
  it("formats relative time", () => {
    const now = Date.parse("2026-01-01T12:00:00Z");
    expect(formatRelative("2026-01-01T11:57:00Z", now)).toBe("3 minutes ago");
    expect(formatRelative("2026-01-01T11:59:50Z", now)).toBe("just now");
  });
  it("shortens shas and knows which runs can be cancelled", () => {
    expect(shortSha("0123456789abcdef")).toBe("0123456");
    expect(isCancellable("queued")).toBe(true);
    expect(isCancellable("running")).toBe(true);
    expect(isCancellable("failed")).toBe(false);
  });
});

describe("safeNext", () => {
  it("only allows in-app paths", () => {
    expect(safeNext("/runs/4?x=1")).toBe("/runs/4?x=1");
    expect(safeNext("//evil.test")).toBe("/");
    expect(safeNext("https://evil.test")).toBe("/");
    expect(safeNext("/login")).toBe("/");
    expect(safeNext(null)).toBe("/");
  });
});

describe("parseAnsi", () => {
  it("maps colours and resets", () => {
    const spans = parseAnsi("a\u001b[31mred\u001b[0m b\u001b[1;92mbold\u001b[0m");
    expect(spans.map((s) => [s.text, s.className])).toEqual([
      ["a", ""],
      ["red", "ansi-red"],
      [" b", ""],
      ["bold", "ansi-bright-green ansi-bold"],
    ]);
  });
  it("drops unsupported escape sequences", () => {
    expect(
      parseAnsi("x\u001b[2Ky")
        .map((s) => s.text)
        .join(""),
    ).toBe("xy");
  });
});

describe("LogStore", () => {
  it("appends contiguous chunks and ignores replays", () => {
    const store = new LogStore();
    expect(store.apply(1, chunk(0, "abc"))).toBe(true);
    expect(store.apply(1, chunk(0, "abc"))).toBe(true);
    expect(store.apply(1, chunk(3, "def", true))).toBe(true);
    expect(store.get(1)).toMatchObject({ text: "abcdef", next: 6, complete: true });
  });
  it("rejects gaps so the caller can reload", () => {
    const store = new LogStore();
    expect(store.apply(1, chunk(5, "late"))).toBe(false);
    expect(store.get(1).text).toBe("");
  });
  it("loads all pages until complete and shares concurrent loads", async () => {
    const store = new LogStore();
    const calls: number[] = [];
    const pages = ["aa", "bb", "cc"];
    const fetcher = (_id: number, offset: number): Promise<LogChunk> => {
      calls.push(offset);
      const index = offset / 2;
      return Promise.resolve(chunk(offset, pages[index] ?? "", index === 2));
    };
    await Promise.all([store.load(7, fetcher), store.load(7, fetcher)]);
    expect(store.get(7)).toMatchObject({ text: "aabbcc", complete: true, loading: false });
    expect(calls).toEqual([0, 2, 4]);
  });
  it("stops when a running step has no new data", async () => {
    const store = new LogStore();
    await store.load(1, (_id, offset) => Promise.resolve(chunk(offset, offset === 0 ? "hi" : "")));
    expect(store.get(1)).toMatchObject({ text: "hi", complete: false, loading: false });
  });
  it("reports a load failure", async () => {
    const store = new LogStore();
    await store.load(1, () => Promise.reject(new Error("boom")));
    expect(store.get(1).error).toBe("boom");
  });
});

/** Minimal ANSI SGR parser: colours 30-37/90-97, bold, reset. Everything else is dropped. */
export interface AnsiSpan {
  text: string;
  className: string;
}

// eslint-disable-next-line no-control-regex
const ESCAPE = /\u001b\[([0-9;]*)([A-Za-z])/g;
const COLOURS = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"] as const;

export function parseAnsi(input: string): AnsiSpan[] {
  const spans: AnsiSpan[] = [];
  let colour: string | null = null;
  let bold = false;
  let last = 0;

  const push = (text: string) => {
    if (text === "") return;
    const classes = [colour === null ? "" : `ansi-${colour}`, bold ? "ansi-bold" : ""];
    spans.push({ text, className: classes.filter(Boolean).join(" ") });
  };

  for (const match of input.matchAll(ESCAPE)) {
    push(input.slice(last, match.index));
    last = match.index + match[0].length;
    if (match[2] !== "m") continue;
    const codes = (match[1] === "" ? "0" : (match[1] ?? "0")).split(";").map(Number);
    for (const code of codes) {
      if (code === 0) {
        colour = null;
        bold = false;
      } else if (code === 1) bold = true;
      else if (code === 22) bold = false;
      else if (code === 39) colour = null;
      else if (code >= 30 && code <= 37) colour = COLOURS[code - 30] ?? null;
      else if (code >= 90 && code <= 97) colour = `bright-${COLOURS[code - 90] ?? "white"}`;
    }
  }
  push(input.slice(last));
  return spans;
}

export interface LogLine {
  /** 1-based, counted over everything buffered. */
  number: number;
  /** RFC 3339 timestamp when the line starts with one (it is stripped from `spans`). */
  timestamp: string | null;
  spans: AnsiSpan[];
}

const LEADING_TIMESTAMP =
  /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))(?: |$)/;

/**
 * Splits a log into numbered lines with ANSI colour carried across line breaks. A leading
 * RFC 3339 timestamp (as `docker logs -t` writes it) becomes `timestamp`; logs without
 * timestamps simply have none.
 */
export function parseLog(text: string): LogLine[] {
  if (text === "") return [];
  const raw = (text.endsWith("\n") ? text.slice(0, -1) : text).split("\n");
  const stamps: (string | null)[] = [];
  const rest: string[] = [];
  for (const entry of raw) {
    const line = entry.endsWith("\r") ? entry.slice(0, -1) : entry;
    const match = LEADING_TIMESTAMP.exec(line);
    stamps.push(match?.[1] ?? null);
    rest.push(match ? line.slice(match[0].length) : line);
  }
  const lines: AnsiSpan[][] = [];
  let current: AnsiSpan[] = [];
  for (const span of parseAnsi(rest.join("\n"))) {
    span.text.split("\n").forEach((part, index) => {
      if (index > 0) {
        lines.push(current);
        current = [];
      }
      if (part !== "") current.push({ text: part, className: span.className });
    });
  }
  lines.push(current);
  return lines.map((spans, index) => ({
    number: index + 1,
    timestamp: stamps[index] ?? null,
    spans,
  }));
}

export function lineText(line: LogLine): string {
  return line.spans.map((span) => span.text).join("");
}

/** Case-insensitive match ranges of `term` in the visible text of a line. */
export function matchRanges(plain: string, term: string): [number, number][] {
  if (term === "") return [];
  const haystack = plain.toLowerCase();
  const needle = term.toLowerCase();
  const ranges: [number, number][] = [];
  let from = haystack.indexOf(needle);
  while (from !== -1) {
    ranges.push([from, from + needle.length]);
    from = haystack.indexOf(needle, from + needle.length);
  }
  return ranges;
}

/** Number of case-insensitive matches of `term` in the visible text of a raw log. */
export function countMatches(raw: string, term: string): number {
  if (term === "" || raw === "") return 0;
  return matchRanges(raw.replace(ESCAPE, ""), term).length;
}

export function hasTimestamps(raw: string): boolean {
  return LEADING_TIMESTAMP.test(raw.slice(0, 64));
}

export interface MarkedSpan extends AnsiSpan {
  mark: boolean;
}

/** Splits spans at the match ranges so matches can be wrapped in <mark>. */
export function markSpans(spans: AnsiSpan[], ranges: [number, number][]): MarkedSpan[] {
  if (ranges.length === 0) return spans.map((span) => ({ ...span, mark: false }));
  const out: MarkedSpan[] = [];
  let offset = 0;
  for (const span of spans) {
    const end = offset + span.text.length;
    let cursor = offset;
    for (const [from, to] of ranges) {
      if (to <= cursor || from >= end) continue;
      const start = Math.max(from, cursor);
      const stop = Math.min(to, end);
      if (start > cursor) {
        out.push({ ...span, text: span.text.slice(cursor - offset, start - offset), mark: false });
      }
      out.push({ ...span, text: span.text.slice(start - offset, stop - offset), mark: true });
      cursor = stop;
    }
    if (cursor < end) out.push({ ...span, text: span.text.slice(cursor - offset), mark: false });
    offset = end;
  }
  return out;
}

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

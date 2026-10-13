import { Button } from "@primer/react";
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { fetchLog } from "../api/hooks";
import { lineText, markSpans, matchRanges, parseLog, type LogLine } from "../lib/ansi";
import type { LogStore } from "../lib/logStore";
import { Callout } from "./Feedback";
import styles from "./LogViewer.module.css";

/** Rendering a very long log line by line is slow; older lines stay one click away. */
export const LINE_WINDOW = 2000;

function timeOfDay(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toISOString().slice(11, 23);
}

function Row({ line, search, timestamps }: { line: LogLine; search: string; timestamps: boolean }) {
  const ranges = search === "" ? [] : matchRanges(lineText(line), search);
  const spans = markSpans(line.spans, ranges);
  return (
    <div className={styles.line} data-match={ranges.length > 0 ? "true" : undefined}>
      <span className={styles.number} aria-hidden="true">
        {line.number}
      </span>
      {timestamps && line.timestamp ? (
        <time className={styles.timestamp} dateTime={line.timestamp} title={line.timestamp}>
          {timeOfDay(line.timestamp)}
        </time>
      ) : null}
      <span className={styles.text}>
        {spans.map((span, index) => {
          const classes = [span.className, span.mark ? styles.mark : ""].filter(Boolean).join(" ");
          if (span.mark) {
            return (
              <mark key={index} className={classes}>
                {span.text}
              </mark>
            );
          }
          return classes ? (
            <span key={index} className={classes}>
              {span.text}
            </span>
          ) : (
            span.text
          );
        })}
      </span>
    </div>
  );
}

/**
 * The Actions-style step log: line numbers, optional timestamps, search highlights and
 * follow-the-tail while a step runs. Secrets are masked by the server; this only displays text.
 */
export function LogViewer({
  store,
  runId,
  stepId,
  running,
  stale,
  search = "",
  timestamps = false,
}: {
  store: LogStore;
  runId: number;
  stepId: number;
  running: boolean;
  stale: boolean;
  search?: string;
  timestamps?: boolean;
}) {
  const entry = useSyncExternalStore(store.subscribe, () => store.get(stepId));
  const scroller = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const [windowSize, setWindowSize] = useState(LINE_WINDOW);

  useEffect(() => {
    if (!store.get(stepId).complete) {
      void store.load(stepId, (id, offset) => fetchLog(runId, id, offset));
    }
  }, [store, runId, stepId]);

  const lines = useMemo(() => parseLog(entry.text), [entry.text]);
  const hidden = Math.max(0, lines.length - windowSize);
  const shown = hidden > 0 ? lines.slice(hidden) : lines;

  useEffect(() => {
    const element = scroller.current;
    if (element && following.current) element.scrollTop = element.scrollHeight;
  }, [entry.text]);

  const onScroll = () => {
    const element = scroller.current;
    if (!element) return;
    following.current = element.scrollHeight - element.scrollTop - element.clientHeight < 24;
  };

  const empty = lines.length === 0;
  return (
    <div className={styles.log} data-stale={stale && running ? "true" : "false"}>
      {entry.error ? (
        <Callout
          variant="danger"
          role="alert"
          actions={
            <Button
              size="small"
              onClick={() => {
                void store.load(stepId, (id, offset) => fetchLog(runId, id, offset));
              }}
            >
              Reload log
            </Button>
          }
        >
          {entry.error}
        </Callout>
      ) : null}
      {entry.truncated ? (
        <p className={styles.note}>Showing the most recent part of a very long log.</p>
      ) : null}
      {stale && running ? (
        <p className={styles.stale} role="status">
          Stale: live updates are disconnected, so new lines may be missing.
        </p>
      ) : null}
      <div
        ref={scroller}
        className={styles.scroll}
        role="log"
        aria-label="Step output"
        tabIndex={0}
        onScroll={onScroll}
      >
        {empty ? (
          <span className={styles.empty}>
            {entry.loading
              ? "Loading log…"
              : running
                ? "Waiting for output…"
                : "This step produced no output."}
          </span>
        ) : (
          <>
            {hidden > 0 ? (
              <div className={styles.earlier}>
                <Button
                  size="small"
                  variant="invisible"
                  onClick={() => {
                    setWindowSize((size) => size + LINE_WINDOW);
                  }}
                >
                  Show {Math.min(hidden, LINE_WINDOW)} earlier lines ({hidden} hidden)
                </Button>
              </div>
            ) : null}
            {shown.map((line) => (
              <Row key={line.number} line={line} search={search} timestamps={timestamps} />
            ))}
          </>
        )}
      </div>
    </div>
  );
}

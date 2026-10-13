import { AlertIcon } from "@primer/octicons-react";
import { useEffect, useMemo, useRef, useSyncExternalStore } from "react";
import { fetchLog } from "../api/hooks";
import { parseAnsi } from "../lib/ansi";
import type { LogStore } from "../lib/logStore";
import "./LogViewer.css";

/** Secrets are masked by the server; this component only displays what it is sent. */
export function LogViewer({
  store,
  runId,
  stepId,
  running,
  stale,
}: {
  store: LogStore;
  runId: number;
  stepId: number;
  running: boolean;
  stale: boolean;
}) {
  const entry = useSyncExternalStore(store.subscribe, () => store.get(stepId));
  const scroller = useRef<HTMLDivElement>(null);
  const following = useRef(true);

  useEffect(() => {
    if (!store.get(stepId).complete) {
      void store.load(stepId, (id, offset) => fetchLog(runId, id, offset));
    }
  }, [store, runId, stepId]);

  const spans = useMemo(() => parseAnsi(entry.text), [entry.text]);

  useEffect(() => {
    const element = scroller.current;
    if (element && following.current) element.scrollTop = element.scrollHeight;
  }, [entry.text]);

  const onScroll = () => {
    const element = scroller.current;
    if (!element) return;
    following.current = element.scrollHeight - element.scrollTop - element.clientHeight < 24;
  };

  const empty = entry.text === "";
  return (
    <div className="log" data-stale={stale && running ? "true" : "false"}>
      {entry.error ? (
        <div className="callout callout-danger" role="alert">
          <AlertIcon size={16} aria-hidden="true" />
          <div className="callout-body">
            <span>{entry.error}</span>
            <div className="callout-actions">
              <button
                type="button"
                className="btn"
                onClick={() => {
                  void store.load(stepId, (id, offset) => fetchLog(runId, id, offset));
                }}
              >
                Reload log
              </button>
            </div>
          </div>
        </div>
      ) : null}
      {entry.truncated ? (
        <p className="muted log-note">Showing the most recent part of a very long log.</p>
      ) : null}
      {stale && running ? (
        <p className="log-stale" role="status">
          Stale: live updates are disconnected, so new lines may be missing.
        </p>
      ) : null}
      <div
        ref={scroller}
        className="log-scroll"
        role="log"
        aria-label="Step output"
        tabIndex={0}
        onScroll={onScroll}
      >
        {empty ? (
          <span className="muted">
            {entry.loading
              ? "Loading log…"
              : running
                ? "Waiting for output…"
                : "This step produced no output."}
          </span>
        ) : (
          <pre>
            {spans.map((span, index) =>
              span.className ? (
                <span key={index} className={span.className}>
                  {span.text}
                </span>
              ) : (
                span.text
              ),
            )}
          </pre>
        )}
      </div>
    </div>
  );
}

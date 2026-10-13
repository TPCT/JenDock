import { PauseIcon, PlayIcon, SearchIcon } from "@primer/octicons-react";
import { Button, Checkbox, FormControl, Select, TextInput } from "@primer/react";
import { useEffect, useMemo, useRef, useState } from "react";
import { containerStreamUrl, useLogsOnce } from "../../api/containerHooks";
import type { ContainerView, LogLineView } from "../../api/containerTypes";
import type { EnvironmentName } from "../../api/types";
import { Callout } from "../../components/Feedback";
import logStyles from "../../components/LogViewer.module.css";
import { lineText, markSpans, matchRanges, parseAnsi } from "../../lib/ansi";
import box from "../../styles/box.module.css";

const MAX_LINES = 5000;
const TAILS = [100, 200, 500, 1000] as const;
type Status = "idle" | "connecting" | "live" | "disconnected" | "ended" | "error";

interface StreamState {
  lines: LogLineView[];
  dropped: number;
  status: Status;
  message: string | null;
}

/** Follows `GET .../logs/stream` while `enabled`; every new connection starts with the tail again. */
function useLogStream(url: string, enabled: boolean): StreamState {
  const [state, setState] = useState<StreamState>({
    lines: [],
    dropped: 0,
    status: "idle",
    message: null,
  });
  useEffect(() => {
    if (!enabled) return;
    let source: EventSource | null = null;
    let retry = 0;
    let stopped = false;
    const connect = () => {
      setState((current) => ({ ...current, status: "connecting", message: null }));
      const next = new EventSource(url, { withCredentials: true });
      source = next;
      next.addEventListener("open", () => {
        setState({ lines: [], dropped: 0, status: "live", message: null });
      });
      next.addEventListener("log", (event) => {
        if (stopped || next.readyState === 2) return;
        try {
          const data = JSON.parse((event as MessageEvent<string>).data) as {
            lines: LogLineView[];
            dropped: number;
          };
          setState((current) => ({
            ...current,
            status: "live",
            dropped: current.dropped + data.dropped,
            lines: [...current.lines, ...data.lines].slice(-MAX_LINES),
          }));
        } catch {
          /* a malformed event is ignored */
        }
      });
      next.addEventListener("end", () => {
        next.close();
        setState((current) => ({
          ...current,
          status: "ended",
          message: "The stream ended after one hour.",
        }));
      });
      next.addEventListener("error", (event) => {
        const data = (event as MessageEvent<string | undefined>).data;
        next.close();
        if (data) {
          let message = "The container is gone.";
          try {
            message = (JSON.parse(data) as { message?: string }).message ?? message;
          } catch {
            /* keep the default */
          }
          setState((current) => ({ ...current, status: "error", message }));
          return;
        }
        setState((current) => ({
          ...current,
          status: "disconnected",
          message: "The connection was lost. Reconnecting…",
        }));
        if (!stopped) retry = window.setTimeout(connect, 3000);
      });
    };
    connect();
    return () => {
      stopped = true;
      window.clearTimeout(retry);
      source?.close();
    };
  }, [url, enabled]);
  return state;
}

export function LogsPanel({
  slug,
  env,
  container,
}: {
  slug: string;
  env: EnvironmentName;
  container: ContainerView;
}) {
  const running = container.state === "running";
  const [tail, setTail] = useState<number>(200);
  const [following, setFollowing] = useState(running);
  const [search, setSearch] = useState("");
  const [timestamps, setTimestamps] = useState(false);
  const url = containerStreamUrl(slug, env, container.id, tail);
  const stream = useLogStream(url, running && following);
  const once = useLogsOnce(slug, env, container.id, tail, !running);
  const scroller = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);

  const lines = useMemo<LogLineView[]>(
    () => (running ? stream.lines : (once.data?.lines ?? [])),
    [running, stream.lines, once.data],
  );
  const rows = useMemo(
    () =>
      lines.map((line, index) => {
        const spans = parseAnsi(line.text);
        const ranges =
          search === "" ? [] : matchRanges(lineText({ number: 0, timestamp: null, spans }), search);
        return {
          index,
          timestamp: line.timestamp,
          spans: markSpans(spans, ranges),
          hit: ranges.length > 0,
        };
      }),
    [lines, search],
  );
  const hits = rows.filter((row) => row.hit).length;

  useEffect(() => {
    const element = scroller.current;
    if (element && pinned.current) element.scrollTop = element.scrollHeight;
  }, [rows.length]);

  return (
    <div className={box.stack}>
      <div className={box.cluster}>
        <FormControl>
          <FormControl.Label visuallyHidden>Lines to load</FormControl.Label>
          <Select
            value={String(tail)}
            onChange={(event) => {
              setTail(Number(event.target.value));
            }}
          >
            {TAILS.map((value) => (
              <Select.Option key={value} value={String(value)}>
                Last {value} lines
              </Select.Option>
            ))}
          </Select>
        </FormControl>
        <Button
          leadingVisual={following ? PauseIcon : PlayIcon}
          disabled={!running}
          onClick={() => {
            setFollowing((value) => !value);
          }}
        >
          {following && running ? "Pause" : "Follow"}
        </Button>
        <TextInput
          aria-label="Search logs"
          placeholder="Search logs"
          leadingVisual={SearchIcon}
          value={search}
          onChange={(event) => {
            setSearch(event.target.value);
          }}
        />
        <FormControl>
          <Checkbox
            checked={timestamps}
            onChange={(event) => {
              setTimestamps(event.target.checked);
            }}
          />
          <FormControl.Label>Show timestamps</FormControl.Label>
        </FormControl>
      </div>
      <p className="muted" role="status">
        {!running
          ? "The container is not running; this is its last output."
          : following
            ? stream.status === "live"
              ? "Live: new lines appear as they are written."
              : "Connecting…"
            : "Paused: nothing new is added. Press Follow to catch up."}
        {search !== "" ? ` ${String(hits)} matching ${hits === 1 ? "line" : "lines"}.` : ""}{" "}
        Passwords, tokens and keys are masked by the server before they reach this page.
      </p>
      {stream.message && running && following ? (
        <Callout variant={stream.status === "error" ? "danger" : "warning"} role="status">
          {stream.message}
        </Callout>
      ) : null}
      {stream.dropped > 0 ? (
        <Callout>
          {stream.dropped} lines were skipped because the container wrote faster than they could be
          sent.
        </Callout>
      ) : null}
      {!running && once.isError ? (
        <Callout variant="danger" role="alert">
          Could not load the logs.
        </Callout>
      ) : null}
      <div
        ref={scroller}
        className={logStyles.scroll}
        role="log"
        aria-label="Container output"
        tabIndex={0}
        onScroll={() => {
          const element = scroller.current;
          if (element)
            pinned.current = element.scrollHeight - element.scrollTop - element.clientHeight < 24;
        }}
      >
        {rows.length === 0 ? (
          <span className={logStyles.empty}>
            {running && following && stream.status !== "live" ? "Loading logs…" : "No output yet."}
          </span>
        ) : (
          rows.map((row) => (
            <div key={row.index} className={logStyles.line}>
              <span className={logStyles.number} aria-hidden="true">
                {row.index + 1}
              </span>
              {timestamps ? (
                <time className={logStyles.timestamp} dateTime={row.timestamp}>
                  {row.timestamp.slice(11, 23)}
                </time>
              ) : null}
              <span className={logStyles.text}>
                {row.spans.map((span, i) =>
                  span.mark ? (
                    <mark key={i} className={logStyles.mark}>
                      {span.text}
                    </mark>
                  ) : span.className ? (
                    <span key={i} className={span.className}>
                      {span.text}
                    </span>
                  ) : (
                    span.text
                  ),
                )}
              </span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

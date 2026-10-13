import { FitAddon } from "@xterm/addon-fit";
import { WebglAddon } from "@xterm/addon-webgl";
import { Terminal, type ITheme } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { Button } from "@primer/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { requestTerminalTicket } from "../api/containerHooks";
import type { TerminalExitReason, TerminalServerMessage } from "../api/containerTypes";
import { isApiError } from "../api/client";
import type { EnvironmentName } from "../api/types";
import { installStyleBridge, mirrorInlineStyles } from "../lib/styleBridge";
import box from "../styles/box.module.css";
import { Callout, errorMessage } from "./Feedback";
import styles from "./TerminalView.module.css";

/**
 * `graceMs` is how long the server keeps a shell after a disconnect (terminal_idle_seconds);
 * `retryMs` the pause between reattach attempts. Tests shorten them.
 */
export const terminalTimings = { graceMs: 120_000, retryMs: 2_000, pingMs: 20_000 };

export const EXIT_TEXT: Record<TerminalExitReason, string> = {
  exited: "The shell exited.",
  closed: "The terminal was closed.",
  idle_timeout: "The terminal was closed because it was idle for too long.",
  absolute_timeout: "The 30 minute limit for one terminal was reached, so it was closed.",
  shutdown: "The server is restarting, so the terminal was closed.",
};

type Status = "connecting" | "connected" | "reconnecting" | "ended";
interface Banner {
  tone: "default" | "warning" | "danger";
  text: string;
  signIn?: boolean;
}

/** Reads Primer's colour variables so the terminal follows light, dark and dark dimmed. */
export function readTheme(): ITheme {
  const css = getComputedStyle(document.documentElement);
  const get = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback;
  return {
    background: get("--bgColor-inset", "#010409"),
    foreground: get("--fgColor-default", "#e6edf3"),
    cursor: get("--fgColor-default", "#e6edf3"),
    selectionBackground: get("--bgColor-accent-muted", "#264f78"),
    black: get("--ansi-black", "#484f58"),
    red: get("--ansi-red", "#ff7b72"),
    green: get("--ansi-green", "#3fb950"),
    yellow: get("--ansi-yellow", "#d29922"),
    blue: get("--ansi-blue", "#58a6ff"),
    magenta: get("--ansi-magenta", "#bc8cff"),
    cyan: get("--ansi-cyan", "#39c5cf"),
    white: get("--ansi-white", "#b1bac4"),
    brightBlack: get("--ansi-blackBright", "#6e7681"),
    brightRed: get("--ansi-redBright", "#ffa198"),
    brightGreen: get("--ansi-greenBright", "#56d364"),
    brightYellow: get("--ansi-yellowBright", "#e3b341"),
    brightBlue: get("--ansi-blueBright", "#79c0ff"),
    brightMagenta: get("--ansi-magentaBright", "#d2a8ff"),
    brightCyan: get("--ansi-cyanBright", "#56d4dd"),
    brightWhite: get("--ansi-whiteBright", "#f0f6fc"),
  };
}

const KEYS = [
  { label: "Tab", data: "\t" },
  { label: "Esc", data: "\u001b" },
  { label: "Ctrl-C", data: "\u0003" },
  { label: "Ctrl-D", data: "\u0004" },
  { label: "↑", name: "Up", data: "\u001b[A" },
  { label: "↓", name: "Down", data: "\u001b[B" },
  { label: "←", name: "Left", data: "\u001b[D" },
  { label: "→", name: "Right", data: "\u001b[C" },
  { label: "Enter", data: "\r" },
] as const;

/**
 * xterm.js wired to the terminal protocol: a CSRF ticket POST, then a WebSocket that carries
 * keystrokes and output as binary frames and control messages as JSON text. After an unexpected
 * disconnect it reattaches to the same shell (`terminal_id`) for up to two minutes.
 */
export function TerminalView({
  slug,
  env,
  containerId,
}: {
  slug: string;
  env: EnvironmentName;
  containerId: string;
}) {
  const host = useRef<HTMLDivElement>(null);
  const term = useRef<Terminal | null>(null);
  const socket = useRef<WebSocket | null>(null);
  const terminalId = useRef<string | null>(null);
  const finished = useRef(false);
  const lostAt = useRef<number | null>(null);
  const generation = useRef(0);
  const reconnect = useRef<(reattach: boolean) => void>(() => undefined);
  const retryTimer = useRef(0);
  const pingTimer = useRef(0);
  const [status, setStatus] = useState<Status>("connecting");
  const [banner, setBanner] = useState<Banner | null>(null);
  const [attempt, setAttempt] = useState(0);

  const end = useCallback((next: Banner) => {
    finished.current = true;
    window.clearTimeout(retryTimer.current);
    window.clearInterval(pingTimer.current);
    setStatus("ended");
    setBanner(next);
  }, []);

  const connect = useCallback(
    (reattach: boolean) => {
      const terminal = term.current;
      const gen = generation.current;
      const stale = () => finished.current || gen !== generation.current;
      if (!terminal || stale()) return;
      setStatus(reattach ? "reconnecting" : "connecting");
      requestTerminalTicket(slug, env, containerId, reattach ? terminalId.current : null)
        .then((ticket) => {
          if (stale()) return;
          const scheme = window.location.protocol === "https:" ? "wss" : "ws";
          const ws = new WebSocket(`${scheme}://${window.location.host}${ticket.websocket_path}`);
          ws.binaryType = "arraybuffer";
          socket.current = ws;
          ws.onopen = () => {
            ws.send(
              JSON.stringify({
                type: "auth",
                ticket: ticket.ticket,
                rows: terminal.rows,
                cols: terminal.cols,
              }),
            );
          };
          ws.onmessage = (event: MessageEvent<string | ArrayBuffer>) => {
            if (typeof event.data !== "string") {
              terminal.write(new Uint8Array(event.data));
              return;
            }
            let message: TerminalServerMessage;
            try {
              message = JSON.parse(event.data) as TerminalServerMessage;
            } catch {
              return;
            }
            if (message.type === "ready") {
              terminalId.current = message.terminal_id;
              lostAt.current = null;
              setStatus("connected");
              setBanner(null);
              window.clearInterval(pingTimer.current);
              pingTimer.current = window.setInterval(() => {
                if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "ping" }));
              }, terminalTimings.pingMs);
              terminal.focus();
            } else if (message.type === "exit") {
              const code =
                message.code !== null && message.reason === "exited"
                  ? ` (exit code ${String(message.code)})`
                  : "";
              end({
                tone:
                  message.reason === "exited" || message.reason === "closed"
                    ? "default"
                    : "warning",
                text: `${EXIT_TEXT[message.reason]}${code}`,
              });
            } else if (message.type === "error") {
              setBanner({ tone: "danger", text: message.message });
            }
          };
          ws.onclose = (event) => {
            window.clearInterval(pingTimer.current);
            if (stale() || socket.current !== ws) return;
            if (event.code === 4401) {
              end({
                tone: "danger",
                text: "Your session ended (you signed out or it expired), so the terminal was closed. Sign in again to open a new one.",
                signIn: true,
              });
              return;
            }
            if (event.code === 1008) {
              end({
                tone: "danger",
                text: reattach
                  ? "The shell is no longer available. Open a new terminal."
                  : "The server refused the connection (the ticket was not accepted). Open the terminal again.",
              });
              return;
            }
            if (event.code === 1013) {
              end({
                tone: "warning",
                text: "The build worker is not available right now. Try again in a moment.",
              });
              return;
            }
            if (event.code === 1000) {
              end({ tone: "default", text: "The terminal was closed." });
              return;
            }
            if (terminalId.current === null) {
              end({ tone: "danger", text: "The connection could not be opened. Try again." });
              return;
            }
            lostAt.current ??= Date.now();
            retry();
          };
        })
        .catch((error: unknown) => {
          if (stale()) return;
          if (reattach && !(isApiError(error) && error.status >= 400 && error.status < 500)) {
            retry();
            return;
          }
          end({
            tone: "danger",
            text:
              isApiError(error) && error.status === 409
                ? "The container is not running, so there is nothing to open a terminal in."
                : errorMessage(error),
          });
        });

      function retry() {
        const lost = lostAt.current ?? Date.now();
        if (Date.now() - lost >= terminalTimings.graceMs) {
          end({
            tone: "warning",
            text: "The connection was lost for more than 2 minutes, so the shell was closed by the server. Open a new terminal.",
          });
          return;
        }
        setStatus("reconnecting");
        setBanner({
          tone: "warning",
          text: "Connection lost. Reconnecting to the same shell; the server keeps it for 2 minutes.",
        });
        window.clearTimeout(retryTimer.current);
        retryTimer.current = window.setTimeout(() => {
          reconnect.current(true);
        }, terminalTimings.retryMs);
      }
    },
    [slug, env, containerId, end],
  );

  useEffect(() => {
    reconnect.current = connect;
  }, [connect]);

  useEffect(() => {
    const element = host.current;
    if (!element) return;
    generation.current += 1;
    finished.current = false;
    terminalId.current = null;
    lostAt.current = null;
    const restore = installStyleBridge();
    const terminal = new Terminal({
      cursorBlink: true,
      fontSize: 14,
      scrollback: 5000,
      fontFamily:
        getComputedStyle(document.documentElement)
          .getPropertyValue("--fontStack-monospace")
          .trim() || "monospace",
      theme: readTheme(),
    });
    const fit = new FitAddon();
    terminal.loadAddon(fit);
    terminal.open(element);
    const unmirror = mirrorInlineStyles(element);
    try {
      // Canvas rendering needs no inline styles; without WebGL the DOM renderer is used.
      const webgl = new WebglAddon();
      webgl.onContextLoss(() => {
        webgl.dispose();
      });
      terminal.loadAddon(webgl);
    } catch {
      /* DOM renderer */
    }
    const refit = () => {
      if (element.clientWidth > 0 && element.clientHeight > 0) fit.fit();
    };
    refit();
    term.current = terminal;

    const input = terminal.onData((data) => {
      const ws = socket.current;
      if (ws?.readyState === WebSocket.OPEN) ws.send(new TextEncoder().encode(data));
    });
    const resized = terminal.onResize(({ rows, cols }) => {
      const ws = socket.current;
      if (ws?.readyState === WebSocket.OPEN)
        ws.send(JSON.stringify({ type: "resize", rows, cols }));
    });
    const observer = new ResizeObserver(refit);
    observer.observe(element);
    const themeWatcher = new MutationObserver(() => {
      terminal.options.theme = readTheme();
    });
    themeWatcher.observe(document.documentElement, { attributes: true });

    connect(false);
    return () => {
      generation.current += 1;
      finished.current = true;
      window.clearTimeout(retryTimer.current);
      window.clearInterval(pingTimer.current);
      const ws = socket.current;
      socket.current = null;
      ws?.close();
      input.dispose();
      resized.dispose();
      observer.disconnect();
      themeWatcher.disconnect();
      terminal.dispose();
      unmirror();
      term.current = null;
      restore();
    };
    // `attempt` restarts the whole session ("Open a new terminal").
  }, [connect, attempt]);

  const send = (data: string) => {
    term.current?.input(data, true);
    term.current?.focus();
  };

  return (
    <div className={box.stack}>
      {banner ? (
        <Callout
          variant={banner.tone === "default" ? "default" : banner.tone}
          role={banner.tone === "danger" ? "alert" : "status"}
          actions={
            status === "ended" ? (
              banner.signIn ? (
                <Button as={Link} to="/login">
                  Sign in
                </Button>
              ) : (
                <Button
                  onClick={() => {
                    setBanner(null);
                    setAttempt((value) => value + 1);
                  }}
                >
                  Open a new terminal
                </Button>
              )
            ) : null
          }
        >
          {banner.text}
        </Callout>
      ) : null}
      <div
        ref={host}
        className={styles.terminal}
        data-status={status}
        role="group"
        aria-label="Terminal"
      />
      <div className={styles.keys} role="toolbar" aria-label="Terminal keys">
        {KEYS.map((key) => (
          <Button
            key={key.label}
            size="small"
            disabled={status !== "connected"}
            aria-label={"name" in key ? `${key.name} arrow` : key.label}
            onClick={() => {
              send(key.data);
            }}
          >
            {key.label}
          </Button>
        ))}
        <Button
          size="small"
          variant="danger"
          disabled={status !== "connected"}
          onClick={() => {
            const ws = socket.current;
            if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "close" }));
          }}
        >
          End shell
        </Button>
      </div>
      <p className="muted" role="status">
        {status === "connecting" ? "Connecting…" : null}
        {status === "connected"
          ? "Connected. Commands and output are not stored; only who opened the terminal and when is recorded."
          : null}
        {status === "reconnecting" ? "Reconnecting…" : null}
      </p>
    </div>
  );
}

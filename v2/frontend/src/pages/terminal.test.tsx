import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { terminalTimings } from "../components/TerminalView";
import { MOCK_WEB_ID } from "../mocks/containers";
import { server } from "../mocks/server";
import { FakeTerminal } from "../test/fakeXterm";
import { FakeWebSocket } from "../test/fakeWebSocket";
import { renderApp } from "../test/render";

vi.mock("@xterm/xterm", async () => {
  const { FakeTerminal: Terminal } = await import("../test/fakeXterm");
  return { Terminal };
});
vi.mock("@xterm/addon-fit", async () => {
  const { FakeFitAddon: FitAddon } = await import("../test/fakeXterm");
  return { FitAddon };
});
vi.mock("@xterm/addon-webgl", async () => {
  const { FakeWebglAddon: WebglAddon } = await import("../test/fakeXterm");
  return { WebglAddon };
});
vi.mock("@xterm/xterm/css/xterm.css", () => ({}));

const PAGE = `/projects/shop-api/environments/development/containers/${MOCK_WEB_ID}/terminal`;
const CACHE_PAGE = `/projects/shop-api/environments/development/containers/${"000000a3".repeat(8)}/terminal`;
const saved = { ...terminalTimings };

let consoleError: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  FakeTerminal.instances = [];
  FakeWebSocket.install();
  consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
});
afterEach(() => {
  FakeWebSocket.uninstall();
  Object.assign(terminalTimings, saved);
  expect(consoleError).not.toHaveBeenCalled();
});

async function connected() {
  renderApp(PAGE);
  await waitFor(() => {
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
  const ws = FakeWebSocket.last;
  act(() => {
    ws.open();
  });
  return ws;
}

function ready(ws: FakeWebSocket, id = "term-1") {
  act(() => {
    ws.text({ type: "ready", terminal_id: id });
  });
}

describe("terminal protocol", () => {
  it("gets a ticket by POST, opens the WebSocket and authenticates with it", async () => {
    let body: unknown = null;
    server.use(
      http.post(
        "/api/projects/:slug/environments/:env/containers/:id/terminal/tickets",
        async ({ request }) => {
          body = await request.json();
          return HttpResponse.json({
            ticket: "one-time-ticket",
            expires_at: new Date(Date.now() + 30_000).toISOString(),
            websocket_path: "/api/terminal/ws",
            container_id: MOCK_WEB_ID,
          });
        },
      ),
    );
    const ws = await connected();
    expect(body).toEqual({});
    expect(ws.url).toBe(`ws://${window.location.host}/api/terminal/ws`);
    expect(ws.binaryType).toBe("arraybuffer");
    expect(ws.json[0]).toEqual({ type: "auth", ticket: "one-time-ticket", rows: 24, cols: 80 });
    expect(screen.getAllByText("Connecting…").length).toBeGreaterThan(0);
    ready(ws);
    expect(
      await screen.findByText(/Connected\. Commands and output are not stored/),
    ).toBeInTheDocument();
  });

  it("sends keystrokes as binary frames and writes binary output to xterm", async () => {
    const ws = await connected();
    ready(ws);
    act(() => {
      FakeTerminal.last.type("ls -la\r");
    });
    expect(ws.sent.some((item) => typeof item !== "string")).toBe(true);
    expect(ws.keystrokes).toBe("ls -la\r");
    act(() => {
      ws.binary("total 0\r\n");
    });
    expect(FakeTerminal.last.written).toEqual(["total 0\r\n"]);
  });

  it("sends resize messages and answers pings", async () => {
    const ws = await connected();
    ready(ws);
    act(() => {
      FakeTerminal.last.resizeTo(40, 120);
    });
    expect(ws.json).toContainEqual({ type: "resize", rows: 40, cols: 120 });
    act(() => {
      ws.text({ type: "pong" });
    });
  });

  it("has a key bar for phones: Tab, Esc, Ctrl-C, Ctrl-D, arrows, Enter", async () => {
    const user = userEvent.setup();
    const ws = await connected();
    ready(ws);
    const bar = screen.getByRole("toolbar", { name: "Terminal keys" });
    for (const name of [
      "Tab",
      "Esc",
      "Ctrl-C",
      "Ctrl-D",
      "Up arrow",
      "Down arrow",
      "Left arrow",
      "Right arrow",
      "Enter",
    ]) {
      await user.click(within(bar).getByRole("button", { name }));
    }
    expect(ws.keystrokes).toBe("\t\u001b\u0003\u0004\u001b[A\u001b[B\u001b[D\u001b[C\r");
    await user.click(within(bar).getByRole("button", { name: "End shell" }));
    expect(ws.json).toContainEqual({ type: "close" });
  });

  it("disables the keys until the shell is ready", async () => {
    await connected();
    expect(screen.getByRole("button", { name: "Ctrl-C" })).toBeDisabled();
  });
});

describe("terminal endings", () => {
  it.each([
    ["idle_timeout", /closed because it was idle for too long/],
    ["absolute_timeout", /30 minute limit/],
    ["shutdown", /server is restarting/],
    ["closed", /terminal was closed/],
  ] as const)("explains %s", async (reason, text) => {
    const ws = await connected();
    ready(ws);
    act(() => {
      ws.text({ type: "exit", code: null, reason });
    });
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open a new terminal" })).toBeInTheDocument();
  });

  it("reports the exit code of the shell", async () => {
    const ws = await connected();
    ready(ws);
    act(() => {
      ws.text({ type: "exit", code: 3, reason: "exited" });
    });
    expect(await screen.findByText("The shell exited. (exit code 3)")).toBeInTheDocument();
  });

  it("explains a revoked session (4401) and offers to sign in", async () => {
    const ws = await connected();
    ready(ws);
    act(() => {
      ws.serverClose(4401);
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("Your session ended");
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/login");
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it("explains a refused ticket (1008) and an unavailable worker (1013)", async () => {
    const ws = await connected();
    act(() => {
      ws.serverClose(1008);
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("refused the connection");
  });

  it("explains an unavailable worker (1013)", async () => {
    const ws = await connected();
    act(() => {
      ws.serverClose(1013);
    });
    expect(await screen.findByText(/build worker is not available/)).toBeInTheDocument();
  });

  it("starts a fresh session with a new ticket", async () => {
    const user = userEvent.setup();
    const ws = await connected();
    ready(ws, "first");
    act(() => {
      ws.text({ type: "exit", code: 0, reason: "idle_timeout" });
    });
    await user.click(await screen.findByRole("button", { name: "Open a new terminal" }));
    await waitFor(() => {
      expect(FakeWebSocket.instances).toHaveLength(2);
    });
    expect(FakeTerminal.instances.length).toBeGreaterThan(1);
  });

  it("shows server error messages without ending the session", async () => {
    const ws = await connected();
    ready(ws);
    act(() => {
      ws.text({ type: "error", message: "input is rate limited" });
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("input is rate limited");
    expect(screen.getByRole("button", { name: "Ctrl-C" })).toBeEnabled();
  });
});

describe("reconnecting within the grace period", () => {
  beforeEach(() => {
    terminalTimings.retryMs = 10;
  });

  it("reattaches to the same shell with its terminal_id", async () => {
    const bodies: unknown[] = [];
    server.use(
      http.post(
        "/api/projects/:slug/environments/:env/containers/:id/terminal/tickets",
        async ({ request }) => {
          bodies.push(await request.json());
          return HttpResponse.json({
            ticket: `t-${String(bodies.length)}`,
            expires_at: new Date(Date.now() + 30_000).toISOString(),
            websocket_path: "/api/terminal/ws",
            container_id: MOCK_WEB_ID,
          });
        },
      ),
    );
    const ws = await connected();
    ready(ws, "shell-42");
    act(() => {
      ws.serverClose(1006);
    });
    expect(
      await screen.findByText(/Reconnecting to the same shell; the server keeps it for 2 minutes/),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(FakeWebSocket.instances).toHaveLength(2);
    });
    expect(bodies[1]).toEqual({ terminal_id: "shell-42" });
    act(() => {
      FakeWebSocket.last.open();
      FakeWebSocket.last.text({ type: "ready", terminal_id: "shell-42" });
    });
    await waitFor(() => {
      expect(screen.queryByText(/Reconnecting to the same shell/)).not.toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: "Ctrl-C" })).toBeEnabled();
  });

  it("gives up after the grace period and says the shell was closed", async () => {
    terminalTimings.graceMs = 60;
    const ws = await connected();
    ready(ws);
    act(() => {
      ws.serverClose(1006);
    });
    await waitFor(
      () => {
        const last = FakeWebSocket.last;
        if (last.readyState !== 3)
          act(() => {
            last.serverClose(1006);
          });
        expect(screen.getByText(/more than 2 minutes/)).toBeInTheDocument();
      },
      { timeout: 3000 },
    );
    expect(screen.getByRole("button", { name: "Open a new terminal" })).toBeInTheDocument();
  });

  it("stops retrying when the shell is gone (refused on reattach)", async () => {
    const ws = await connected();
    ready(ws);
    act(() => {
      ws.serverClose(1006);
    });
    await waitFor(() => {
      expect(FakeWebSocket.instances).toHaveLength(2);
    });
    act(() => {
      FakeWebSocket.last.serverClose(1008);
    });
    expect(await screen.findByText(/shell is no longer available/)).toBeInTheDocument();
  });
});

describe("terminal page", () => {
  it("does not open a terminal for a stopped container", async () => {
    renderApp(CACHE_PAGE);
    expect(await screen.findByText(/A terminal needs a running container/)).toBeInTheDocument();
    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it("reports a ticket refusal (container stopped meanwhile)", async () => {
    server.use(
      http.post("/api/projects/:slug/environments/:env/containers/:id/terminal/tickets", () =>
        HttpResponse.json({ error: { code: "conflict", message: "stopped" } }, { status: 409 }),
      ),
    );
    renderApp(PAGE);
    expect(await screen.findByRole("alert")).toHaveTextContent("not running");
  });

  it("is reachable from the container page", async () => {
    const user = userEvent.setup();
    renderApp(`/projects/shop-api/environments/development/containers/${MOCK_WEB_ID}`);
    await user.click(await screen.findByRole("link", { name: "Terminal" }));
    expect(
      await screen.findByRole("heading", { name: "Terminal: shop-development-web-1" }),
    ).toBeInTheDocument();
  });
});

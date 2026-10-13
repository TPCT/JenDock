import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MOCK_WEB_ID } from "../mocks/containers";
import { server } from "../mocks/server";
import { FakeEventSource } from "../test/fakeEventSource";
import { renderApp } from "../test/render";

let consoleError: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
});
afterEach(() => {
  expect(consoleError).not.toHaveBeenCalled();
});

const LIST = "/projects/shop-api/environments/development/containers";
const WEB = `${LIST}/${MOCK_WEB_ID}`;
const CACHE_ID = "000000a3".repeat(8);
const apiError = (status: number, code: string, message: string) =>
  HttpResponse.json({ error: { code, message } }, { status });

describe("containers inventory", () => {
  it("lists service, image, commit, state, restarts, uptime and ports", async () => {
    renderApp(LIST);
    const table = await screen.findByRole("table", { name: "Development containers" });
    const web = within(table).getByRole("row", { name: /web/ });
    expect(
      within(web).getByText("jendock/shop-development-web:3f9c2ab41d0e-r12"),
    ).toBeInTheDocument();
    expect(within(web).getByText("3f9c2ab")).toBeInTheDocument();
    expect(within(web).getByText("running")).toBeInTheDocument();
    expect(within(web).getByText("healthy")).toBeInTheDocument();
    expect(within(web).getByText("26h 0m")).toBeInTheDocument();
    expect(within(web).getByText("127.0.0.1:20001 → 8080")).toBeInTheDocument();
    const worker = within(table).getByRole("row", { name: /worker/ });
    expect(within(worker).getByText("3")).toBeInTheDocument();
    expect(within(worker).getByText("no health check")).toBeInTheDocument();
    expect(
      within(within(table).getByRole("row", { name: /cache/ })).getByText("exited"),
    ).toBeInTheDocument();
  });

  it("shows host telemetry and an empty state", async () => {
    renderApp(LIST);
    const host = await screen.findByRole("region", { name: "Host telemetry" });
    expect(within(host).getByText(/CPU \(4 cores\)/)).toBeInTheDocument();
    expect(within(host).getByRole("progressbar", { name: "Memory used" })).toBeInTheDocument();
    expect(within(host).getByRole("img", { name: /Host CPU/ })).toBeInTheDocument();
  });

  it("has an empty state for an environment without containers", async () => {
    renderApp("/projects/internal-tools/environments/development/containers");
    expect(await screen.findByRole("heading", { name: "No containers yet" })).toBeInTheDocument();
  });

  it("puts the host card on the project overview", async () => {
    renderApp("/projects/shop-api");
    expect(await screen.findByRole("region", { name: "Host telemetry" })).toBeInTheDocument();
  });

  it("the Containers tab is current and keeps the environment", async () => {
    renderApp("/projects/shop-api/environments/production/containers");
    await screen.findByRole("heading", { name: "Production containers" });
    const tabs = screen.getByRole("navigation", { name: "Project" });
    expect(within(tabs).getByRole("link", { name: "Containers" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });
});

describe("lifecycle actions (two-step confirmation)", () => {
  it("shows exactly what will happen, then confirms with the server's token", async () => {
    const user = userEvent.setup();
    renderApp(WEB);
    await screen.findByRole("heading", { name: "shop-development-web-1" });
    expect(screen.getByRole("button", { name: "Start" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Restart" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Restart web?" });
    expect(dialog).toHaveTextContent("Stops and starts shop-development-web-1");
    expect(dialog).toHaveTextContent("valid for one minute");
    await user.click(within(dialog).getByRole("button", { name: "Restart container" }));
    expect(
      await screen.findByText(/Restarted shop-development-web-1\. It was running\./),
    ).toBeInTheDocument();
  });

  it("changes nothing when the dialog is cancelled", async () => {
    let confirmed = 0;
    server.use(
      http.post("/api/projects/:slug/environments/:env/containers/:id/actions/confirm", () => {
        confirmed += 1;
        return apiError(500, "internal_error", "should not be called");
      }),
    );
    const user = userEvent.setup();
    renderApp(WEB);
    await user.click(await screen.findByRole("button", { name: "Stop" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Stop web?" });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(confirmed).toBe(0);
    expect(screen.getAllByText("running").length).toBeGreaterThan(0);
  });

  it("stops the container and then offers Start", async () => {
    const user = userEvent.setup();
    renderApp(WEB);
    await user.click(await screen.findByRole("button", { name: "Stop" }));
    await user.click(
      within(await screen.findByRole("alertdialog", { name: "Stop web?" })).getByRole("button", {
        name: "Stop container",
      }),
    );
    await screen.findByText(/Stopped shop-development-web-1/);
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Start" })).toBeEnabled();
    });
    expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
  });

  it("explains an expired confirmation", async () => {
    server.use(
      http.post("/api/projects/:slug/environments/:env/containers/:id/actions/confirm", () =>
        apiError(422, "validation_error", "confirmation expired"),
      ),
    );
    const user = userEvent.setup();
    renderApp(WEB);
    await user.click(await screen.findByRole("button", { name: "Restart" }));
    await user.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", {
        name: "Restart container",
      }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "confirmation expired or no longer matches",
    );
  });

  it("reports a conflict from step one", async () => {
    server.use(
      http.post("/api/projects/:slug/environments/:env/containers/:id/actions", () =>
        apiError(409, "conflict", "the environment is busy with a deployment"),
      ),
    );
    const user = userEvent.setup();
    renderApp(WEB);
    await user.click(await screen.findByRole("button", { name: "Restart" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("busy with a deployment");
  });

  it("shows a stopped container as stopped and has no stats", async () => {
    renderApp(`${LIST}/${CACHE_ID}`);
    expect(
      await screen.findByRole("heading", { name: "shop-development-cache-1" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start" })).toBeEnabled();
    expect(
      screen.getByText(/Statistics are only recorded while the container is running/),
    ).toBeInTheDocument();
  });

  it("says when a container does not exist any more", async () => {
    renderApp(`${LIST}/${"0".repeat(64)}`);
    expect(await screen.findByRole("heading", { name: "Container not found" })).toBeInTheDocument();
  });
});

describe("container overview", () => {
  it("shows details and a CPU/memory sparkline from the telemetry history", async () => {
    renderApp(WEB);
    expect(await screen.findByRole("img", { name: /^CPU: / })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /^Memory: / })).toBeInTheDocument();
    expect(screen.getByText("shop-data → /data")).toBeInTheDocument();
    expect(screen.getByLabelText("Container ID")).toHaveTextContent(MOCK_WEB_ID);
  });
});

describe("logs", () => {
  async function openLogs() {
    renderApp(`${WEB}?tab=logs`);
    await screen.findByRole("log", { name: "Container output" });
    await waitFor(() => {
      expect(FakeEventSource.instances.length).toBeGreaterThan(0);
    });
    return FakeEventSource.last;
  }

  it("follows the live stream, searches and pauses", async () => {
    const user = userEvent.setup();
    const source = await openLogs();
    expect(source.url).toBe(
      `${WEB}/logs/stream?tail=200`.replace(
        LIST,
        "/api/projects/shop-api/environments/development/containers",
      ),
    );
    expect(source.withCredentials).toBe(true);
    act(() => {
      source.open();
      source.emit("log", {
        lines: [
          { timestamp: "2026-01-15T10:00:00.000Z", text: "listening on 8080" },
          { timestamp: "2026-01-15T10:00:01.000Z", text: "token=****** rejected" },
        ],
        dropped: 0,
      });
    });
    const log = screen.getByRole("log", { name: "Container output" });
    expect(log).toHaveTextContent("listening on 8080");
    expect(log).toHaveTextContent("token=******");
    expect(screen.getByText(/Passwords, tokens and keys are masked/)).toBeInTheDocument();

    await user.type(screen.getByRole("textbox", { name: "Search logs" }), "listening");
    expect(log.querySelectorAll("mark")).toHaveLength(1);
    expect(screen.getByText(/1 matching line\./)).toBeInTheDocument();

    expect(log.querySelector("time")).toBeNull();
    await user.click(screen.getByLabelText("Show timestamps"));
    expect(log.querySelector("time")).toHaveTextContent("10:00:00.000");

    await user.click(screen.getByRole("button", { name: "Pause" }));
    expect(source.readyState).toBe(2);
    act(() => {
      source.emit("log", {
        lines: [{ timestamp: "2026-01-15T10:00:02.000Z", text: "after pause" }],
        dropped: 0,
      });
    });
    expect(log).not.toHaveTextContent("after pause");
    expect(screen.getByText(/Paused/)).toBeInTheDocument();
  });

  it("reports skipped lines and a vanished container", async () => {
    const source = await openLogs();
    act(() => {
      source.open();
      source.emit("log", {
        lines: [{ timestamp: "2026-01-15T10:00:00.000Z", text: "x" }],
        dropped: 12,
      });
    });
    expect(screen.getByText(/12 lines were skipped/)).toBeInTheDocument();
    act(() => {
      source.emit("error", { message: "container vanished" });
    });
    expect(await screen.findByText("container vanished")).toBeInTheDocument();
  });

  it("reconnects after a lost connection and starts from the tail again", async () => {
    const source = await openLogs();
    act(() => {
      source.open();
      source.emit("log", {
        lines: [{ timestamp: "2026-01-15T10:00:00.000Z", text: "first" }],
        dropped: 0,
      });
      source.fail();
    });
    expect(await screen.findByText("The connection was lost. Reconnecting…")).toBeInTheDocument();
    await waitFor(
      () => {
        expect(FakeEventSource.instances.length).toBeGreaterThan(1);
      },
      { timeout: 4500 },
    );
    act(() => {
      FakeEventSource.last.open();
      FakeEventSource.last.emit("log", {
        lines: [{ timestamp: "2026-01-15T10:00:05.000Z", text: "second" }],
        dropped: 0,
      });
    });
    const log = screen.getByRole("log", { name: "Container output" });
    expect(log).toHaveTextContent("second");
    expect(log).not.toHaveTextContent("first");
  }, 10_000);

  it("loads the last output of a stopped container without a stream", async () => {
    renderApp(`${LIST}/${CACHE_ID}?tab=logs`);
    const log = await screen.findByRole("log", { name: "Container output" });
    await waitFor(() => {
      expect(log).toHaveTextContent("listening on 8080");
    });
    expect(FakeEventSource.instances).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Follow" })).toBeDisabled();
  });
});

describe("environment variables", () => {
  const ENV = `${WEB}?tab=environment`;

  it("masks values and reveals one only after an audit confirmation", async () => {
    const user = userEvent.setup();
    renderApp(ENV);
    const list = await screen.findByRole("list", { name: "Environment variables" });
    expect(within(list).getAllByText("********")).toHaveLength(4);
    expect(screen.queryByText(/value-of/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reveal DATABASE_URL" }));
    const confirm = await screen.findByRole("alertdialog", { name: "Reveal DATABASE_URL?" });
    expect(confirm).toHaveTextContent("recorded in the audit log");
    await user.click(within(confirm).getByRole("button", { name: "Reveal value" }));
    const dialog = await screen.findByRole("dialog", { name: "DATABASE_URL (revealed)" });
    expect(within(dialog).getByLabelText("Value of DATABASE_URL")).toHaveTextContent(
      "value-of-DATABASE_URL",
    );
    await user.click(within(dialog).getByRole("button", { name: "Hide and close" }));
    expect(screen.queryByText("value-of-DATABASE_URL")).not.toBeInTheDocument();
  });

  it("stages an edit, reviews key names only and applies it to the service", async () => {
    const user = userEvent.setup();
    renderApp(ENV);
    await screen.findByRole("list", { name: "Environment variables" });
    await user.click(screen.getByRole("button", { name: "Edit PORT" }));
    const edit = await screen.findByRole("dialog", { name: "New value for PORT" });
    await user.type(within(edit).getByLabelText(/Value/), "supersecret-9090");
    await user.click(within(edit).getByRole("button", { name: "Stage change" }));
    expect(await screen.findByText("new value staged")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Review 1 change" }));
    const review = await screen.findByRole("alertdialog", { name: "Apply changes to web?" });
    expect(within(review).getByRole("list", { name: "Changed variables" })).toHaveTextContent(
      "Set or change PORT",
    );
    expect(review.textContent).not.toContain("supersecret-9090");
    expect(review).toHaveTextContent("new container ID");
    await user.click(within(review).getByRole("button", { name: "Apply and restart service" }));
    expect(
      await screen.findByText(/The web service was recreated with the new values/),
    ).toBeInTheDocument();
    // the container id changed, so the page moved to the replacement
    await waitFor(() => {
      expect(screen.getByRole("list", { name: "Environment variables" })).toBeInTheDocument();
    });
    expect(await screen.findByText("overridden")).toBeInTheDocument();
  });

  it("explains that a failed apply restored the previous configuration", async () => {
    const user = userEvent.setup();
    renderApp(ENV);
    await screen.findByRole("list", { name: "Environment variables" });
    await user.click(screen.getByRole("button", { name: "Add variable" }));
    const add = await screen.findByRole("dialog", { name: "Add a variable" });
    await user.type(within(add).getByLabelText(/Name/), "BAD_VALUE");
    await user.type(within(add).getByLabelText(/Value/), "BREAK");
    await user.click(within(add).getByRole("button", { name: "Stage change" }));
    await user.click(screen.getByRole("button", { name: "Review 1 change" }));
    const review = await screen.findByRole("alertdialog", { name: "Apply changes to web?" });
    await user.click(within(review).getByRole("button", { name: "Apply and restart service" }));
    const alert = await within(review).findByRole("alert");
    expect(alert).toHaveTextContent("Nothing changed");
    expect(alert).toHaveTextContent("previous configuration was restored");
  });

  it("validates a new variable name and can discard staged changes", async () => {
    const user = userEvent.setup();
    renderApp(ENV);
    await screen.findByRole("list", { name: "Environment variables" });
    expect(screen.getByRole("button", { name: /^Review 0 changes$/ })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Add variable" }));
    const add = await screen.findByRole("dialog", { name: "Add a variable" });
    await user.type(within(add).getByLabelText(/Name/), "1bad");
    await user.click(within(add).getByRole("button", { name: "Stage change" }));
    expect(await within(add).findByRole("alert")).toHaveTextContent("does not start with a digit");
    await user.clear(within(add).getByLabelText(/Name/));
    await user.type(within(add).getByLabelText(/Name/), "GOOD");
    await user.click(within(add).getByRole("button", { name: "Stage change" }));
    await user.click(await screen.findByRole("button", { name: "Discard staged changes" }));
    expect(screen.getByRole("button", { name: /^Review 0 changes$/ })).toBeDisabled();
  });
});

describe("files", () => {
  const FILES = `${WEB}?tab=files`;

  it("browses read only with a breadcrumb path, shows text and refuses binaries", async () => {
    const user = userEvent.setup();
    renderApp(FILES);
    await user.click(await screen.findByRole("button", { name: /^app/ }));
    const nav = screen.getByRole("navigation", { name: "Folder path" });
    expect(within(nav).getByText("app")).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: /package\.json/ }));
    expect(await screen.findByLabelText("File content")).toHaveTextContent('"name": "shop-api"');
    await user.click(screen.getByRole("button", { name: /logo\.png/ }));
    expect(await screen.findByText(/This is a binary file/)).toBeInTheDocument();
    await user.click(within(nav).getByRole("link", { name: "/ (root)" }));
    expect(await screen.findByRole("button", { name: /^etc/ })).toBeInTheDocument();
    expect(screen.getByText(/credentials .* are hidden/)).toBeInTheDocument();
  });

  it("offers more of a truncated file up to the limit, and notes truncated folders", async () => {
    const user = userEvent.setup();
    renderApp(FILES);
    await user.click(await screen.findByRole("button", { name: /^app/ }));
    await user.click(await screen.findByRole("button", { name: /server\.js/ }));
    await screen.findByLabelText("File content");
    // 2600 bytes fit into the default 64 KiB: no "load more"
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Folder path" });
    await user.click(within(nav).getByRole("link", { name: "/ (root)" }));
    await user.click(await screen.findByRole("button", { name: /^data/ }));
    expect(await screen.findByText(/Only the first 500 entries are shown/)).toBeInTheDocument();
    expect(screen.getByText("This folder is empty.")).toBeInTheDocument();
  });

  it("shows the server's reason when a path may not be read", async () => {
    server.use(
      http.get("/api/projects/:slug/environments/:env/containers/:id/files/content", () =>
        apiError(422, "validation_error", "that file may hold credentials and cannot be shown"),
      ),
    );
    const user = userEvent.setup();
    renderApp(FILES);
    await user.click(await screen.findByRole("button", { name: /^app/ }));
    await user.click(await screen.findByRole("button", { name: /package\.json/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("may hold credentials");
  });

  it("needs a running container", async () => {
    renderApp(`${LIST}/${CACHE_ID}?tab=files`);
    expect(
      await screen.findByText(/only be browsed while the container is running/),
    ).toBeInTheDocument();
  });
});

describe("limits", () => {
  const LIMITS = `${WEB}?tab=limits`;

  it("saves CPU and memory within the server maximum", async () => {
    const user = userEvent.setup();
    renderApp(LIMITS);
    const cpus = await screen.findByLabelText("CPUs");
    expect(cpus).toHaveValue("1");
    await user.clear(cpus);
    await user.type(cpus, "2");
    await user.type(screen.getByLabelText("Memory"), "1g");
    await user.click(screen.getByRole("button", { name: "Save limits" }));
    expect(await screen.findByText("Limits updated.")).toBeInTheDocument();
    expect(screen.getByText("2 CPUs")).toBeInTheDocument();
    expect(screen.getByText("1.0 GiB")).toBeInTheDocument();
  });

  it("shows the server's maximum when a value is too high, and checks the format first", async () => {
    const user = userEvent.setup();
    renderApp(LIMITS);
    const cpus = await screen.findByLabelText("CPUs");
    await user.clear(cpus);
    await user.type(cpus, "99");
    await user.click(screen.getByRole("button", { name: "Save limits" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("above the server maximum of 8");
    await user.clear(cpus);
    await user.type(cpus, "1");
    await user.type(screen.getByLabelText("Memory"), "lots");
    await user.click(screen.getByRole("button", { name: "Save limits" }));
    expect(await screen.findByText("Memory looks like 512m or 2g.")).toBeInTheDocument();
  });
});

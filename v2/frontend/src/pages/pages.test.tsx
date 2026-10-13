import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setMockAuthenticated } from "../mocks/handlers";
import {
  detailFor,
  MOCK_LOCKED_USERNAME,
  logText,
  MOCK_PASSWORD,
  MOCK_USERNAME,
} from "../mocks/data";
import { server } from "../mocks/server";
import { FakeEventSource } from "../test/fakeEventSource";
import { renderApp } from "../test/render";

let consoleError: ReturnType<typeof vi.spyOn>;
let consoleWarn: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
  consoleWarn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
});
afterEach(() => {
  expect(consoleError).not.toHaveBeenCalled();
  expect(consoleWarn).not.toHaveBeenCalled();
});

const apiError = (status: number, code: string, message: string) =>
  HttpResponse.json({ error: { code, message } }, { status });

describe("every mock state renders without console errors", () => {
  const routes: [string, string | RegExp][] = [
    ["/projects", "Projects"],
    ["/projects/shop-api", "Shop API"],
    ["/projects/missing", "Project not found"],
    ["/projects/internal-tools/environments/production/runs", "No runs yet"],
    ["/projects/shop-api/environments/development/runs", "Shop API · Development"],
    ["/projects/shop-api/environments/staging/runs", "Unknown environment"],
    ["/runs", "Runs"],
    ["/runs/101", /Run #42/],
    ["/runs/102", /Run #17/],
    ["/runs/103", /Run #8/],
    ["/runs/104", /Run #5/],
    ["/runs/105", /Run #3/],
    ["/runs/106", /Run #9/],
    ["/runs/99999", "Run not found"],
    ["/runs/abc", "Run not found"],
    ["/settings", "Settings are coming soon"],
    ["/nowhere", "Page not found"],
  ];
  it.each(routes)("%s", async (route, heading) => {
    renderApp(route);
    expect(
      await screen.findByRole("heading", { name: heading, level: undefined }),
    ).toBeInTheDocument();
  });
});

describe("login", () => {
  beforeEach(() => {
    setMockAuthenticated(false);
  });

  it("focuses the first invalid field and shows field-specific errors", async () => {
    const user = userEvent.setup();
    renderApp("/login");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    const username = screen.getByLabelText("Username");
    expect(username).toHaveFocus();
    expect(username).toHaveAccessibleDescription("Enter your username.");
    expect(screen.getByLabelText("Password")).toHaveAccessibleDescription("Enter your password.");
    expect(screen.queryByText("*")).not.toBeInTheDocument();
    await user.type(username, MOCK_USERNAME);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(screen.getByLabelText("Password")).toHaveFocus();
  });

  it("shows one generic error for wrong credentials", async () => {
    const user = userEvent.setup();
    renderApp("/login");
    await user.type(screen.getByLabelText("Username"), "someone");
    await user.type(screen.getByLabelText("Password"), "wrong-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The username or password is incorrect.",
    );
  });

  it("explains rate limiting with a retry time", async () => {
    const user = userEvent.setup();
    renderApp("/login");
    await user.type(screen.getByLabelText("Username"), MOCK_LOCKED_USERNAME);
    await user.type(screen.getByLabelText("Password"), "whatever");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Too many sign-in attempts. Try again in 7 minutes/,
    );
  });

  it("signs in and returns to the requested page", async () => {
    const user = userEvent.setup();
    renderApp("/login?next=%2Fruns");
    await user.type(screen.getByLabelText("Username"), MOCK_USERNAME);
    await user.type(screen.getByLabelText("Password"), MOCK_PASSWORD);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("heading", { name: "Runs" })).toBeInTheDocument();
  });

  it("redirects unauthenticated visitors to the login page", async () => {
    renderApp("/runs/101");
    expect(
      await screen.findByRole("heading", { name: "Sign in to Shiftcodes" }),
    ).toBeInTheDocument();
  });
});

describe("session expiry", () => {
  it("redirects to login when a later request returns 401", async () => {
    renderApp("/projects");
    await screen.findByRole("heading", { name: "Projects" });
    server.use(http.get("/api/runs", () => apiError(401, "unauthenticated", "expired")));
    await userEvent.setup().click(screen.getByRole("link", { name: "Runs" }));
    expect(
      await screen.findByRole("heading", { name: "Sign in to Shiftcodes" }),
    ).toBeInTheDocument();
  });
});

describe("shell", () => {
  it("toggles the theme and signs out", async () => {
    const user = userEvent.setup();
    renderApp("/projects");
    await screen.findByRole("heading", { name: "Projects" });
    await user.click(screen.getByRole("button", { name: /Switch to (dark|light) theme/ }));
    expect(["light", "dark"]).toContain(document.documentElement.getAttribute("data-theme"));
    document.documentElement.removeAttribute("data-theme");
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(
      await screen.findByRole("heading", { name: "Sign in to Shiftcodes" }),
    ).toBeInTheDocument();
  });
});

describe("projects", () => {
  it("lists each project once with both environments", async () => {
    renderApp("/projects");
    await screen.findByRole("heading", { name: "Projects" });
    const shop = await screen.findByRole("article", { name: "Shop API" });
    expect(
      within(shop)
        .getAllByRole("heading", { level: 3 })
        .map((h) => h.textContent),
    ).toEqual(["Development", "Production"]);
    expect(within(shop).getByText("Auto-deploy on")).toBeInTheDocument();
    expect(within(shop).getByText("Running")).toBeInTheDocument();
    expect(within(shop).getByText("Succeeded")).toBeInTheDocument();
    expect(screen.getAllByRole("article")).toHaveLength(4);
    const empty = screen.getByRole("article", { name: "Internal tools" });
    expect(within(empty).getAllByText(/No runs yet/)).toHaveLength(2);
  });

  it("explains what to do when there are no projects", async () => {
    server.use(http.get("/api/projects", () => HttpResponse.json([])));
    renderApp("/projects");
    expect(await screen.findByRole("heading", { name: "No projects yet" })).toBeInTheDocument();
    expect(screen.getByText(/ask an administrator/)).toBeInTheDocument();
  });

  it("shows an error with retry", async () => {
    server.use(
      http.get("/api/projects", () => apiError(500, "internal_error", "internal server error")),
    );
    renderApp("/projects");
    expect(await screen.findByRole("alert")).toHaveTextContent("internal server error");
    server.resetHandlers();
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("article", { name: "Shop API" })).toBeInTheDocument();
  });

  it("shows a loading state first", async () => {
    renderApp("/projects");
    expect(await screen.findByText("Loading projects…")).toBeInTheDocument();
  });
});

describe("runs list", () => {
  it("pages through runs", async () => {
    const user = userEvent.setup();
    renderApp("/projects/shop-api/environments/development/runs");
    expect(await screen.findByText("1-20 of 42")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Older" }));
    expect(await screen.findByText("21-40 of 42")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Newer" })).toBeEnabled();
  });

  it("starts a manual run only after previewing the commit", async () => {
    const user = userEvent.setup();
    renderApp("/projects/shop-api/environments/production/runs");
    await user.click(await screen.findByRole("button", { name: "Run pipeline" }));
    const dialog = await screen.findByRole("dialog", { name: "Run pipeline on Production" });
    const run = within(dialog).getByRole("button", { name: "Run pipeline" });
    expect(run).toBeDisabled();
    await user.click(within(dialog).getByRole("button", { name: "Look up latest commit" }));
    expect(await within(dialog).findByText("Fix rounding of order totals")).toBeInTheDocument();
    expect(run).toBeEnabled();
    await user.click(run);
    expect(await screen.findByRole("heading", { name: /Run #18/ })).toBeInTheDocument();
  });

  it("reports an unknown branch in the dialog", async () => {
    const user = userEvent.setup();
    renderApp("/projects/shop-api/environments/production/runs?run=new");
    const dialog = await screen.findByRole("dialog");
    const input = within(dialog).getByLabelText("Branch");
    await user.clear(input);
    await user.type(input, "missing");
    await user.click(within(dialog).getByRole("button", { name: "Look up latest commit" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      'branch "missing" was not found',
    );
  });
});

describe("run detail", () => {
  it("draws sequential and parallel stages and the steps", async () => {
    renderApp("/runs/101");
    const graph = await screen.findByRole("list", { name: "Pipeline stages" });
    expect(
      within(graph).getByRole("list", { name: "Stages that run in parallel" }),
    ).toBeInTheDocument();
    expect(
      within(graph).getByRole("button", { name: /Build image, Succeeded/ }),
    ).toBeInTheDocument();
    expect(within(graph).getByRole("button", { name: /Unit tests, Running/ })).toBeInTheDocument();
    expect(within(graph).getByRole("button", { name: /Deploy, Queued/ })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /npm test/ })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("shows the commit left-to-right with a copy button", async () => {
    const user = userEvent.setup();
    renderApp("/runs/102");
    await screen.findByRole("heading", { name: /Run #17/ });
    const copy = screen.getByRole("button", { name: /Copy full commit SHA/ });
    await user.click(copy);
    expect(await screen.findByText("Commit SHA copied")).toBeInTheDocument();
  });

  it("loads the log of a finished step in chunks", async () => {
    const user = userEvent.setup();
    renderApp("/runs/102");
    await screen.findByRole("heading", { name: /Run #17/ });
    await user.click(screen.getByRole("button", { name: /dockerBuild app/ }));
    const log = await screen.findByRole("log");
    await waitFor(() => {
      expect(log).toHaveTextContent("All tests passed");
    });
    expect(log).toHaveTextContent("tests/unit/case-24.test.ts");
  });

  it("opens the failed step automatically and shows its exit code", async () => {
    renderApp("/runs/103");
    expect(await screen.findByText("Exited with code 1.")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("log")).toHaveTextContent("AssertionError");
    });
  });

  it("offers cancel only while queued or running, with confirmation", async () => {
    const user = userEvent.setup();
    renderApp("/runs/101");
    await user.click(await screen.findByRole("button", { name: "Cancel run" }));
    const dialog = screen.getByRole("dialog", { name: "Cancel run #42?" });
    await user.click(within(dialog).getByRole("button", { name: "Keep running" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel run" }));
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel run" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(
      await screen.findByText(/Cancel requested/, { selector: ".callout-body" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel requested" })).toBeDisabled();
  });

  it.each(["/runs/102", "/runs/103", "/runs/104", "/runs/106"])(
    "has no cancel on finished run %s",
    async (route) => {
      renderApp(route);
      await screen.findByRole("heading", { name: /Run #/ });
      expect(screen.queryByRole("button", { name: "Cancel run" })).not.toBeInTheDocument();
    },
  );

  it("explains a queued run", async () => {
    renderApp("/runs/105");
    expect(await screen.findByText(/waiting in the queue/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel run" })).toBeInTheDocument();
  });

  it("does not open an event stream for finished runs", async () => {
    renderApp("/runs/102");
    await screen.findByRole("heading", { name: /Run #17/ });
    expect(FakeEventSource.instances).toHaveLength(0);
  });

  it("streams events, marks the view stale when disconnected and recovers", async () => {
    renderApp("/runs/101");
    await screen.findByRole("heading", { name: /Run #42/ });
    await waitFor(() => {
      expect(FakeEventSource.instances).toHaveLength(1);
    });
    const source = FakeEventSource.last;
    expect(source.url).toBe("/api/runs/101/events");
    expect(source.withCredentials).toBe(true);
    await waitFor(() => {
      expect(screen.getByRole("log")).toHaveTextContent("vitest run");
    });
    const offset = new TextEncoder().encode(logText(101010, "running")).length;
    act(() => {
      source.open();
      source.emit("log", {
        step_id: 101010,
        offset,
        text: "live line 1\n",
      });
    });
    // step 101010 is the running step of mock run 101 (stage 1, step 0)
    expect(screen.getByRole("log")).toHaveTextContent("live line 1");
    expect(screen.queryByText(/Live updates are disconnected/)).not.toBeInTheDocument();

    act(() => {
      source.fail();
    });
    expect(await screen.findByText("Live updates are disconnected")).toBeInTheDocument();
    expect(screen.getByText(/Stale: live updates are disconnected/)).toBeInTheDocument();

    await userEvent.setup().click(screen.getByRole("button", { name: "Reconnect now" }));
    await waitFor(() => {
      expect(FakeEventSource.instances).toHaveLength(2);
    });
    act(() => {
      FakeEventSource.last.open();
    });
    await waitFor(() => {
      expect(screen.queryByText("Live updates are disconnected")).not.toBeInTheDocument();
    });
  });

  it("applies status events and stops streaming on end", async () => {
    renderApp("/runs/101");
    await screen.findByRole("heading", { name: /Run #42/ });
    await waitFor(() => {
      expect(FakeEventSource.instances).toHaveLength(1);
    });
    const source = FakeEventSource.last;
    act(() => {
      const run = detailFor(101);
      if (!run) throw new Error("mock run missing");
      source.emit("status", { ...run, status: "succeeded", finished_at: new Date().toISOString() });
    });
    expect(await screen.findByText("Succeeded", { selector: ".status-label" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel run" })).not.toBeInTheDocument();
    act(() => {
      source.emit("end", { status: "succeeded" });
    });
    expect(source.readyState).toBe(2);
  });

  it("shows an error state with retry when the run cannot be loaded", async () => {
    server.use(
      http.get("/api/runs/:id", () => apiError(500, "internal_error", "internal server error")),
    );
    renderApp("/runs/101");
    expect(await screen.findByRole("alert")).toHaveTextContent("internal server error");
  });
});

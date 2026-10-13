import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { auditQuery } from "../api/accountHooks";
import { EMPTY_AUDIT_FILTERS } from "../api/accountTypes";
import { QrCode } from "../components/QrCode";
import { setMockDashboard } from "../mocks/account";
import { server } from "../mocks/server";
import { renderApp } from "../test/render";

let consoleError: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
});
afterEach(() => {
  expect(consoleError).not.toHaveBeenCalled();
});

describe("audit log", () => {
  it("lists events in words, newest first, with details behind a disclosure", async () => {
    renderApp("/audit");
    const list = await screen.findByRole("list", { name: "Audit events" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(50);
    expect(rows[0]).toHaveTextContent("Revealed the secret DATABASE_URL");
    expect(rows[0]).toHaveTextContent("admin");
    const details = within(rows[0] as HTMLElement).getByText("Details");
    expect(details).toBeInTheDocument();
    expect(screen.getByText("Showing 50 events")).toBeInTheDocument();
  });

  it("pages with a cursor until the end of the log", async () => {
    const user = userEvent.setup();
    renderApp("/audit");
    await screen.findByRole("list", { name: "Audit events" });
    await user.click(screen.getByRole("button", { name: "Load older events" }));
    expect(await screen.findByText("End of the log")).toBeInTheDocument();
    const rows = within(screen.getByRole("list", { name: "Audit events" })).getAllByRole(
      "listitem",
    );
    expect(rows.length).toBeGreaterThan(50);
    expect(rows[rows.length - 1]).toHaveTextContent("Signed in");
  });

  it("filters by search text and by action, and clears the filters", async () => {
    const user = userEvent.setup();
    renderApp("/audit");
    await screen.findByRole("list", { name: "Audit events" });
    await user.type(screen.getByLabelText("Search"), "DATABASE_URL");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => {
      expect(
        within(screen.getByRole("list", { name: "Audit events" })).getAllByRole("listitem"),
      ).toHaveLength(2);
    });

    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    await waitFor(() => {
      expect(
        within(screen.getByRole("list", { name: "Audit events" })).getAllByRole("listitem").length,
      ).toBe(50);
    });
    await user.selectOptions(screen.getByLabelText("Action"), "secret.*");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => {
      expect(
        within(screen.getByRole("list", { name: "Audit events" })).getAllByRole("listitem"),
      ).toHaveLength(2);
    });
  });

  it("filters by actor type", async () => {
    const user = userEvent.setup();
    renderApp("/audit");
    await screen.findByRole("list", { name: "Audit events" });
    await user.selectOptions(screen.getByLabelText("Actor"), "system");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => {
      expect(
        within(screen.getByRole("list", { name: "Audit events" })).getAllByRole("listitem"),
      ).toHaveLength(1);
    });
  });

  it("says so when nothing matches", async () => {
    const user = userEvent.setup();
    renderApp("/audit");
    await user.type(await screen.findByLabelText("Search"), "no-such-thing");
    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByRole("heading", { name: "No events match" })).toBeInTheDocument();
    await user.click(
      within(screen.getByRole("main")).getAllByRole("button", {
        name: "Clear filters",
      })[1] as HTMLElement,
    );
    expect(await screen.findByRole("list", { name: "Audit events" })).toBeInTheDocument();
  });

  it("rejects an inverted date range before searching", async () => {
    const user = userEvent.setup();
    renderApp("/audit");
    await screen.findByRole("list", { name: "Audit events" });
    await user.type(screen.getByLabelText("From"), "2026-10-12");
    await user.type(screen.getByLabelText("To"), "2026-10-01");
    expect(screen.getByText("The end date is before the start date.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Search" })).toBeDisabled();
  });

  it("exports the current filters as CSV", async () => {
    const user = userEvent.setup();
    renderApp("/audit");
    const link = await screen.findByRole("link", { name: "Export CSV" });
    expect(link).toHaveAttribute("href", "/api/audit/export.csv");
    await user.type(screen.getByLabelText("Search"), "token");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => {
      expect(screen.getByRole("link", { name: "Export CSV" })).toHaveAttribute(
        "href",
        "/api/audit/export.csv?q=token",
      );
    });
  });

  it("explains a failure and offers a retry", async () => {
    server.use(
      http.get("/api/audit", () =>
        HttpResponse.json({ error: { code: "internal", message: "boom" } }, { status: 500 }),
      ),
    );
    renderApp("/audit");
    expect(await screen.findByText("Could not load the audit log")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("turns the filters into the API query", () => {
    expect(auditQuery(EMPTY_AUDIT_FILTERS)).toBe("");
    const query = new URLSearchParams(
      auditQuery({
        ...EMPTY_AUDIT_FILTERS,
        q: "x y",
        project: "shop",
        action: "secret.*",
        since: "2026-10-01",
        until: "2026-10-02",
      }),
    );
    expect(query.get("q")).toBe("x y");
    expect(query.get("project")).toBe("shop");
    expect(query.get("action")).toBe("secret.*");
    expect(Date.parse(query.get("until") ?? "") - Date.parse(query.get("since") ?? "")).toBe(
      2 * 86_400_000,
    );
  });
});

describe("home dashboard", () => {
  it("shows every card with links to the details", async () => {
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "Home" })).toBeInTheDocument();

    const attention = await screen.findByRole("region", { name: "Needs attention" });
    expect(within(attention).getByText("Running")).toBeInTheDocument();
    expect(within(attention).getByText("Failed")).toBeInTheDocument();
    expect(within(attention).getByRole("link", { name: "Run #13" })).toHaveAttribute(
      "href",
      "/runs/31",
    );
    expect(within(attention).getByText("Stage Build failed")).toBeInTheDocument();

    const deployments = screen.getByRole("region", { name: "Recent deployments" });
    const rows = within(deployments).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("Deployed");
    expect(rows[0]).toHaveTextContent("a1b2c3d");
    expect(rows[0]).toHaveTextContent("Push by admin");
    expect(rows[0]).toHaveTextContent("took 1m 23s");
    expect(
      within(rows[0] as HTMLElement).getByRole("link", { name: "Shop API / production" }),
    ).toHaveAttribute("href", "/runs/30");
    expect(rows[1]).toHaveTextContent("Rolled back");
    expect(rows[1]).toHaveTextContent("Manual run by token CI deploy");

    const manual = screen.getByRole("region", { name: "Pushes waiting for a manual deploy" });
    expect(manual).toHaveTextContent("Blog / development");
    expect(manual).toHaveTextContent("3 pushes to development");
    expect(within(manual).getByRole("link", { name: "Start a run" })).toHaveAttribute(
      "href",
      "/projects/blog/environments/development/runs",
    );
    expect(within(manual).getByRole("link", { name: "Turn on auto-deploy" })).toHaveAttribute(
      "href",
      "/projects/blog/settings/environments",
    );

    const certificates = screen.getByRole("region", { name: "Certificates expiring soon" });
    expect(within(certificates).getByRole("link", { name: "shop.example.com" })).toHaveAttribute(
      "href",
      "/projects/shop-api/environments/production/domains",
    );
    expect(certificates).toHaveTextContent("Expires in 9 days");

    const host = screen.getByRole("region", { name: "Host" });
    expect(within(host).getByLabelText("Memory used")).toBeInTheDocument();
    expect(host).toHaveTextContent("CPU (8 cores)");
    expect(host).toHaveTextContent("38%");
    expect(host).toHaveTextContent("80%");
  });

  it("tells a new user what to do first", async () => {
    setMockDashboard((dashboard) => {
      dashboard.project_count = 0;
      dashboard.domain_count = 0;
      dashboard.deployments = [];
      dashboard.attention = [];
      dashboard.manual_deploys = [];
      dashboard.certificates = [];
    });
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "Welcome to Jendock" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Create your first project" })).toHaveAttribute(
      "href",
      "/projects/new",
    );
    expect(screen.getByRole("region", { name: "Host" })).toBeInTheDocument();
  });

  it("has an empty state per card once projects exist", async () => {
    setMockDashboard((dashboard) => {
      dashboard.deployments = [];
      dashboard.attention = [];
      dashboard.manual_deploys = [];
      dashboard.certificates = [];
      dashboard.domain_count = 0;
    });
    renderApp("/");
    const attention = await screen.findByRole("region", { name: "Needs attention" });
    expect(attention).toHaveTextContent("Nothing needs you right now");
    expect(screen.getByRole("region", { name: "Recent deployments" })).toHaveTextContent(
      "Nothing has been deployed yet",
    );
    expect(
      screen.getByRole("region", { name: "Pushes waiting for a manual deploy" }),
    ).toHaveTextContent("No pushes are waiting");
    expect(screen.getByRole("region", { name: "Certificates expiring soon" })).toHaveTextContent(
      "No domains yet",
    );
  });

  it("says when certificates exist but none expires soon", async () => {
    setMockDashboard((dashboard) => {
      dashboard.certificates = [];
    });
    renderApp("/");
    expect(
      await screen.findByRole("region", { name: "Certificates expiring soon" }),
    ).toHaveTextContent("No certificate expires in the next 30 days");
  });

  it("flags an expired certificate", async () => {
    setMockDashboard((dashboard) => {
      const first = dashboard.certificates[0];
      if (first) {
        first.expired = true;
        first.not_after = new Date(Date.now() - 86_400_000).toISOString();
      }
    });
    renderApp("/");
    expect(
      await screen.findByRole("region", { name: "Certificates expiring soon" }),
    ).toHaveTextContent("Expired");
  });

  it("keeps the page when the host cannot be read", async () => {
    setMockDashboard((dashboard) => {
      dashboard.host = null;
      dashboard.host_error = "Host telemetry is not available right now.";
    });
    renderApp("/");
    const host = await screen.findByRole("region", { name: "Host" });
    expect(host).toHaveTextContent("Host telemetry is not available right now.");
    expect(screen.getByRole("region", { name: "Recent deployments" })).toBeInTheDocument();
  });

  it("is reachable from the navigation and the brand link", async () => {
    renderApp("/projects");
    await screen.findByRole("heading", { name: "Projects" });
    expect(screen.getByRole("link", { name: "Home" })).toHaveAttribute("href", "/");
    expect(screen.getByRole("link", { name: "Audit log" })).toHaveAttribute("href", "/audit");
  });
});

describe("QR code", () => {
  it("draws the modules as SVG without images or styles", () => {
    const { container } = render(
      <QrCode value="otpauth://totp/Jendock:ada?secret=ABC" label="QR" />,
    );
    const svg = screen.getByRole("img", { name: "QR" });
    expect(svg.tagName.toLowerCase()).toBe("svg");
    const path = container.querySelector("path");
    expect(path?.getAttribute("d")).toMatch(/^M\d+ \d+h\d+v1h-\d+z/);
    expect(container.querySelector("image, style, foreignObject")).toBeNull();
    expect(svg.getAttribute("viewBox")).toMatch(/^0 0 (\d+) \1$/);
  });
});

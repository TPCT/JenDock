import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { enableBulkMockData } from "../mocks/data";
import { server } from "../mocks/server";
import { renderApp } from "../test/render";

const apiError = (status: number, code: string, message: string) =>
  HttpResponse.json({ error: { code, message } }, { status });

describe("paged project list (shared list view)", () => {
  it("shows the first page, the total and the pagination", async () => {
    enableBulkMockData();
    renderApp("/projects");
    expect(await screen.findByText("Showing 1-25 of 68")).toBeInTheDocument();
    expect(screen.getByText("68 projects")).toBeInTheDocument();
    expect(
      within(screen.getByRole("list", { name: "Projects" })).getAllByRole("listitem"),
    ).toHaveLength(25);
    const nav = screen.getByRole("navigation", { name: "Pagination" });
    expect(within(nav).getByRole("link", { name: "Next Page" })).toBeInTheDocument();
  });

  it("keeps the page in the URL and loads it", async () => {
    enableBulkMockData();
    const user = userEvent.setup();
    const { router } = renderApp("/projects");
    await screen.findByText("Showing 1-25 of 68");
    await user.click(screen.getByRole("link", { name: "Next Page" }));
    expect(await screen.findByText("Showing 26-50 of 68")).toBeInTheDocument();
    expect(router.state.location.search).toBe("?page=2");
    await user.click(screen.getByRole("link", { name: "Previous Page" }));
    expect(await screen.findByText("Showing 1-25 of 68")).toBeInTheDocument();
    expect(router.state.location.search).toBe("");
  });

  it("opens straight on the page named in the link", async () => {
    enableBulkMockData();
    renderApp("/projects?page=3");
    expect(await screen.findByText("Showing 51-68 of 68")).toBeInTheDocument();
  });

  it("goes to the last page when the link points past the end", async () => {
    enableBulkMockData();
    const { router } = renderApp("/projects?page=99");
    expect(await screen.findByText("Showing 51-68 of 68")).toBeInTheDocument();
    expect(router.state.location.search).toBe("?page=3");
  });

  it("searches on the server, writes q to the URL and resets the page", async () => {
    enableBulkMockData();
    const user = userEvent.setup();
    const { router } = renderApp("/projects?page=2");
    await screen.findByText("Showing 26-50 of 68");
    await user.type(screen.getByRole("searchbox", { name: "Search projects" }), "atlas");
    await waitFor(() => {
      expect(router.state.location.search).toBe("?q=atlas");
    });
    // Wait on the count itself, not with findByText: a whole-page text query costs ~50ms on this
    // Primer page, and polling it every 50ms starves React's transition render of the new URL.
    const count = screen.getByText(/^\d+ projects( found)?$/);
    await waitFor(() => {
      expect(count).toHaveTextContent("3 projects found");
    });
    const list = screen.getByRole("list", { name: "Projects" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);
    expect(within(list).getByRole("link", { name: "Atlas API" })).toBeInTheDocument();
  });

  it("explains an empty search and clears it", async () => {
    enableBulkMockData();
    const user = userEvent.setup();
    const { router } = renderApp("/projects?q=nothing-like-this");
    expect(await screen.findByRole("heading", { name: "No projects match" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear search and filters" }));
    expect(await screen.findByText("Showing 1-25 of 68")).toBeInTheDocument();
    expect(router.state.location.search).toBe("");
    expect(screen.getByRole("searchbox", { name: "Search projects" })).toHaveValue("");
  });

  it("sorts from the sort menu and keeps it in the URL", async () => {
    enableBulkMockData();
    const user = userEvent.setup();
    const { router } = renderApp("/projects");
    await screen.findByText("Showing 1-25 of 68");
    await user.click(screen.getByRole("button", { name: "Sort" }));
    await user.click(await screen.findByRole("menuitemradio", { name: "Oldest" }));
    await waitFor(() => {
      expect(router.state.location.search).toBe("?sort=oldest");
    });
    const first = within(screen.getByRole("list", { name: "Projects" })).getAllByRole(
      "listitem",
    )[0];
    expect(first).toHaveTextContent("Shop API");
  });

  it("filters runs from a menu and shows only matching rows", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/runs");
    await screen.findByText(/Showing 1-/);
    await user.click(screen.getByRole("button", { name: "Status filter" }));
    await user.click(await screen.findByRole("menuitemradio", { name: "Failed" }));
    await waitFor(() => {
      expect(router.state.location.search).toBe("?status=failed");
    });
    await waitFor(() => {
      const rows = within(screen.getByRole("list", { name: "Runs" })).getAllByRole("listitem");
      for (const row of rows) expect(row).toHaveTextContent("Failed");
    });
  });

  it("shows a skeleton while the first page loads", async () => {
    renderApp("/projects");
    expect(await screen.findByText("Loading projects…")).toBeInTheDocument();
    await screen.findByText(/Showing 1-/);
  });

  it("shows an error banner with a retry", async () => {
    server.use(
      http.get("/api/projects", () => apiError(500, "internal_error", "internal server error")),
    );
    const user = userEvent.setup();
    renderApp("/projects");
    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent("Could not load projects");
    expect(banner).toHaveTextContent("internal server error");
    server.resetHandlers();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("article", { name: "Shop API" })).toBeInTheDocument();
  });

  it("shows the empty state with its primary action when nothing exists", async () => {
    server.use(
      http.get("/api/projects", () =>
        HttpResponse.json({ items: [], total: 0, page: 1, per_page: 25, pages: 1 }),
      ),
    );
    renderApp("/projects");
    expect(await screen.findByRole("heading", { name: "No projects yet" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Create your first project" })).toHaveAttribute(
      "href",
      "/projects/new",
    );
    expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
  });
});

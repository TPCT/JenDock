import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { seedBulkDeployments } from "../mocks/deployments";
import { renderApp } from "../test/render";

const ROUTE = "/projects/shop-api/environments/development/deployments";

describe("deployments page", () => {
  it("lists the history with status labels and a tab in the project header", async () => {
    renderApp(ROUTE);
    expect(await screen.findByRole("heading", { name: "Development deployments" })).toBeVisible();
    const list = await screen.findByRole("list", { name: "Deployments" });
    expect(within(list).getByText("Deploying")).toBeInTheDocument();
    expect(within(list).getAllByText("Succeeded").length).toBeGreaterThan(0);
    expect(within(list).getByText("Rolled back")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^Deployments/ })).toBeInTheDocument();
  });

  it("filters by status and pages a long history through the URL", async () => {
    seedBulkDeployments();
    const user = userEvent.setup();
    const { router } = renderApp(ROUTE);
    await screen.findByText("Showing 1-25 of 66");
    await user.click(screen.getByRole("link", { name: "Next Page" }));
    await screen.findByText("Showing 26-50 of 66");
    expect(router.state.location.search).toBe("?page=2");
    await user.click(screen.getByRole("button", { name: "Status filter" }));
    await user.click(await screen.findByRole("menuitemradio", { name: "Failed" }));
    await waitFor(() => {
      expect(router.state.location.search).toBe("?status=failed");
    });
    await waitFor(() => {
      const rows = within(screen.getByRole("list", { name: "Deployments" })).getAllByText("Failed");
      expect(rows.length).toBeGreaterThan(0);
    });
  });

  it("asks before redeploying and then opens the queued run", async () => {
    const user = userEvent.setup();
    const { router } = renderApp(ROUTE);
    const buttons = await screen.findAllByRole("button", { name: /^Redeploy deployment/ });
    const first = buttons[0];
    expect(first).toBeDefined();
    await user.click(first as HTMLElement);
    const dialog = await screen.findByRole("alertdialog", { name: /Redeploy deployment #/ });
    expect(within(dialog).getByText(/Nothing is built again/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    await user.click(first as HTMLElement);
    await user.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Redeploy" }),
    );
    await waitFor(() => {
      expect(router.state.location.pathname).toMatch(/^\/runs\/\d+$/);
    });
  });

  it("shows an empty state for an environment without deployments", async () => {
    renderApp("/projects/shop-api/environments/production/deployments");
    expect(await screen.findByRole("heading", { name: "Nothing deployed yet" })).toBeVisible();
    expect(screen.getByRole("link", { name: "Go to runs" })).toBeInTheDocument();
  });
});

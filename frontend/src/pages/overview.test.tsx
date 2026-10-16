import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import userEvent from "@testing-library/user-event";
import { renderApp } from "../test/render";

describe("home page", () => {
  it("also lists the newest runs and quick links", async () => {
    renderApp("/");
    const runs = await screen.findByRole("region", { name: "Recent runs" });
    expect(await within(runs).findByRole("list", { name: "Runs" })).toBeVisible();
    expect(screen.getByRole("link", { name: "Settings and API tokens" })).toHaveAttribute(
      "href",
      "/settings",
    );
  });
});

describe("project overview", () => {
  it("shows last deploy, containers and domains of the environments", async () => {
    renderApp("/projects/shop-api");
    expect(await screen.findAllByText("Last deploy")).toHaveLength(2);
    expect(await screen.findAllByText(/\d+ of \d+ running/)).not.toHaveLength(0);
  });

  it("shows environment power controls and gates production stop on the project slug", async () => {
    const user = userEvent.setup();
    renderApp("/projects/shop-api");
    expect(await screen.findByText("Stopped")).toBeVisible();
    expect((await screen.findAllByText("Running")).length).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: "Stop" }));
    const dialog = await screen.findByRole("dialog", { name: "Stop production?" });
    const stop = within(dialog).getByRole("button", { name: "Stop environment" });
    expect(stop).toBeDisabled();
    await user.type(
      within(dialog).getByRole("textbox", { name: "Confirm project slug" }),
      "shop-api",
    );
    expect(stop).toBeEnabled();
  });
});

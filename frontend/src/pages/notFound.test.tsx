import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { renderApp } from "../test/render";

describe("unknown addresses", () => {
  it.each([
    "/nothing-here",
    "/projects/shop-api/access",
    "/projects/shop-api/settings/nothing",
    "/settings/nothing",
  ])("%s shows the 404 page with a way back", async (route) => {
    renderApp(route);
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to Projects" })).toHaveAttribute(
      "href",
      "/projects",
    );
  });

  it("explains an unknown project", async () => {
    renderApp("/projects/no-such-project");
    expect(await screen.findByRole("heading", { name: "Project not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to projects" })).toBeInTheDocument();
  });

  it("explains an unknown run", async () => {
    renderApp("/runs/999999");
    expect(await screen.findByRole("heading", { name: "Run not found" })).toBeInTheDocument();
  });
});

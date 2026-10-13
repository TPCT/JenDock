import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { renderApp } from "../test/render";

type Listener = () => void;

/** matchMedia whose result can be flipped to simulate resizing across the breakpoint. */
function mockViewport(mobile: boolean) {
  const listeners = new Set<Listener>();
  let isMobile = mobile;
  window.matchMedia = (query: string) =>
    ({
      get matches() {
        return query.includes("max-width") ? isMobile : false;
      },
      media: query,
      addEventListener: (_: string, listener: Listener) => listeners.add(listener),
      removeEventListener: (_: string, listener: Listener) => listeners.delete(listener),
    }) as unknown as MediaQueryList;
  return (next: boolean) => {
    isMobile = next;
    listeners.forEach((listener) => {
      listener();
    });
  };
}

describe("navigation drawer", () => {
  it("is absent on desktop where the NavList sits in the left pane", async () => {
    mockViewport(false);
    renderApp("/projects");
    await screen.findByRole("heading", { name: "Projects" });
    expect(screen.queryByRole("button", { name: "Open navigation" })).not.toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
  });

  it("opens as a dialog on mobile, closes with Escape and returns focus", async () => {
    mockViewport(true);
    const user = userEvent.setup();
    renderApp("/projects");
    await screen.findByRole("heading", { name: "Projects" });
    expect(screen.queryByRole("navigation", { name: "Main" })).not.toBeInTheDocument();
    const open = screen.getByRole("button", { name: "Open navigation" });
    expect(open).toHaveAttribute("aria-expanded", "false");
    await user.click(open);
    const dialog = await screen.findByRole("dialog", { name: "Navigation" });
    expect(within(dialog).getByRole("navigation", { name: "Main" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(open).toHaveFocus();
  });

  it("closes when a destination is chosen", async () => {
    mockViewport(true);
    const user = userEvent.setup();
    renderApp("/projects");
    await screen.findByRole("heading", { name: "Projects" });
    await user.click(screen.getByRole("button", { name: "Open navigation" }));
    const dialog = await screen.findByRole("dialog", { name: "Navigation" });
    await user.click(within(dialog).getByRole("link", { name: "Runs" }));
    expect(await screen.findByRole("heading", { name: "Runs" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("disappears and stays closed when the viewport grows with the drawer open", async () => {
    const resize = mockViewport(true);
    const user = userEvent.setup();
    renderApp("/projects");
    await screen.findByRole("heading", { name: "Projects" });
    await user.click(screen.getByRole("button", { name: "Open navigation" }));
    await screen.findByRole("dialog", { name: "Navigation" });
    act(() => {
      resize(false);
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open navigation" })).not.toBeInTheDocument();
    act(() => {
      resize(true);
    });
    expect(screen.getByRole("button", { name: "Open navigation" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

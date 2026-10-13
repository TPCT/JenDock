import { act, screen } from "@testing-library/react";
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

describe("drawer controls", () => {
  it("are absent on desktop where the sidebar is always visible", async () => {
    mockViewport(false);
    renderApp("/projects");
    await screen.findByRole("heading", { name: "Projects" });
    expect(screen.queryByRole("button", { name: "Open navigation" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Close navigation" })).not.toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
  });

  it("open and close the drawer on mobile with correct aria and focus", async () => {
    mockViewport(true);
    const user = userEvent.setup();
    renderApp("/projects");
    await screen.findByRole("heading", { name: "Projects" });
    const open = screen.getByRole("button", { name: "Open navigation" });
    expect(open).toHaveAttribute("aria-expanded", "false");
    await user.click(open);
    expect(open).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: "Close navigation" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(open).toHaveAttribute("aria-expanded", "false");
    expect(open).toHaveFocus();
  });

  it("disappear and stay closed when the viewport grows with the drawer open", async () => {
    const resize = mockViewport(true);
    const user = userEvent.setup();
    renderApp("/projects");
    await screen.findByRole("heading", { name: "Projects" });
    await user.click(screen.getByRole("button", { name: "Open navigation" }));
    act(() => {
      resize(false);
    });
    expect(screen.queryByRole("button", { name: "Open navigation" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Close navigation" })).not.toBeInTheDocument();
    act(() => {
      resize(true);
    });
    expect(screen.getByRole("button", { name: "Open navigation" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });
});

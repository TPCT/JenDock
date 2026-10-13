import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { saveAsSomeoneElse } from "../mocks/pipelines";
import { renderApp } from "../test/render";
import { editorViews } from "./CodeEditor";

const DEV = "/projects/shop-api/environments/development/pipeline";
beforeAll(async () => {
  await import("../pages/Pipeline");
}, 60_000);

let consoleError: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
});
afterEach(() => {
  expect(consoleError).not.toHaveBeenCalled();
});

function stageNames(): string[] {
  return screen.getAllByLabelText("Stage name").map((input) => (input as HTMLInputElement).value);
}

async function openText(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("tab", { name: "Text" }));
  await waitFor(() => {
    expect(editorViews).toHaveLength(1);
  });
  const view = editorViews[0];
  if (!view) throw new Error("no editor");
  return view;
}

function setDoc(view: (typeof editorViews)[number], text: string) {
  act(() => {
    view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } });
  });
}

async function ready() {
  await screen.findByRole("heading", { name: "Development pipeline" });
  await waitFor(() => {
    expect(screen.getAllByLabelText("Stage name").length).toBeGreaterThan(0);
  });
}

describe("visual editor", () => {
  it("shows stages, parallel branches and settings from the server model", async () => {
    renderApp(DEV);
    await ready();
    expect(stageNames()).toEqual(["Build image", "Checks", "Unit tests", "Lint", "Deploy"]);
    expect(screen.getByRole("tab", { name: "Visual" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("These branches run at the same time:")).toBeInTheDocument();
  });

  it("generates step forms from the catalogue with labels and help", async () => {
    const user = userEvent.setup();
    renderApp(DEV);
    await ready();
    const deploy = screen
      .getAllByRole("listitem")
      .find((li) => li.getAttribute("data-card") === "stages-2");
    if (!deploy) throw new Error("deploy card missing");
    await user.click(within(deploy).getAllByRole("button", { name: "Add step" })[0] as HTMLElement);
    await user.click(await screen.findByRole("menuitem", { name: /Build an image/ }));
    expect(within(deploy).getAllByLabelText("dockerfile").length).toBeGreaterThan(0);
    expect(within(deploy).getAllByText(/A name for the image/).length).toBeGreaterThan(0);
  });

  it("offers built images and secret names, never values", async () => {
    const user = userEvent.setup();
    renderApp(DEV);
    await ready();
    const deploy = screen
      .getAllByRole("listitem")
      .find((li) => li.getAttribute("data-card") === "stages-2");
    if (!deploy) throw new Error("deploy card missing");
    const image = within(deploy).getAllByLabelText("image")[0] as HTMLSelectElement;
    expect(
      within(image).getByRole("option", { name: "Built in this pipeline: web" }),
    ).toBeInTheDocument();
    await user.click(screen.getByText("Variables"));
    const secret = screen.getByLabelText("Secret");
    expect(
      within(secret)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["DB_PASSWORD", "REGISTRY_TOKEN"]);
  });

  it("round-trips every change through the server renderer into the text", async () => {
    const user = userEvent.setup();
    renderApp(DEV);
    await ready();
    const first = screen.getAllByLabelText("Stage name")[0] as HTMLInputElement;
    await user.clear(first);
    await user.type(first, "Compile");
    expect(await screen.findByText("Unsaved changes")).toBeInTheDocument();
    const view = await openText(user);
    expect(view.state.doc.toString()).toContain("stage('Compile')");
    expect(view.state.doc.toString()).not.toContain("Build image");
  });

  it("reorders stages with buttons and with Alt+Arrow keys, keeping focus", async () => {
    const user = userEvent.setup();
    renderApp(DEV);
    await ready();
    const up = screen.getByRole("button", { name: "Move stage Checks up" });
    up.focus();
    await user.keyboard("{Enter}");
    expect(stageNames().slice(0, 2)).toEqual(
      ["Checks", "Build image"].slice(0, 2).length ? ["Checks", "Unit tests"] : [],
    );
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Move stage Checks down" })).toHaveFocus();
    });
    const name = screen.getAllByLabelText("Stage name")[0] as HTMLElement;
    name.focus();
    await user.keyboard("{Alt>}{ArrowDown}{/Alt}");
    expect(stageNames()[0]).toBe("Build image");
  });

  it("keeps the text canonical after reordering", async () => {
    const user = userEvent.setup();
    renderApp(DEV);
    await ready();
    await user.click(screen.getByRole("button", { name: "Move stage Deploy up" }));
    const view = await openText(user);
    await waitFor(() => {
      const text = view.state.doc.toString();
      expect(text.indexOf("stage('Deploy')")).toBeLessThan(text.indexOf("stage('Checks')"));
    });
  });

  it("confirms before replacing a stage body", async () => {
    const user = userEvent.setup();
    renderApp(DEV);
    await ready();
    await user.click(screen.getAllByLabelText("Run branches in parallel")[0] as HTMLElement);
    const dialog = await screen.findByRole("alertdialog", {
      name: "Replace the contents of this stage?",
    });
    await user.click(within(dialog).getByRole("button", { name: "Keep it" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.getAllByLabelText("Run steps")[0]).toBeChecked();
  });
});

describe("text editor", () => {
  it("opens CodeMirror with the Jenkinsfile and an accessible name", async () => {
    const user = userEvent.setup();
    renderApp(DEV);
    await ready();
    const view = await openText(user);
    expect(view.state.doc.toString()).toContain("pipeline {");
    expect(view.contentDOM.getAttribute("aria-label")).toBe("Jenkinsfile");
    expect(screen.getByRole("tab", { name: "Text" })).toHaveAttribute("aria-selected", "true");
  });

  it("shows server diagnostics and jumps to the position when selected", async () => {
    const user = userEvent.setup();
    renderApp(DEV);
    await ready();
    const view = await openText(user);
    const text = view.state.doc.toString();
    const lines = text.split("\n");
    lines[2] = "    FIXME " + (lines[2] ?? "");
    setDoc(view, lines.join("\n"));
    const entry = await screen.findByRole(
      "button",
      { name: /3:5.*FIXME is not allowed here/ },
      { timeout: 3000 },
    );
    await user.click(entry);
    const expected = view.state.doc.line(3).from + 4;
    expect(view.state.selection.main.head).toBe(expected);
  });

  it("explains that the visual editor is blocked while the text has syntax errors", async () => {
    const user = userEvent.setup();
    renderApp(DEV);
    await ready();
    const view = await openText(user);
    const original = view.state.doc.toString();
    setDoc(view, original + "\nSYNTAX ERROR\n");
    await user.click(screen.getByRole("tab", { name: "Visual" }));
    expect(
      await screen.findByText("The visual editor cannot open this pipeline yet"),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Stage name")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /\d+:1.*unexpected token/ })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open the Text tab" }));
    const again = await waitFor(() => {
      const found = editorViews[0];
      if (!found) throw new Error("no editor");
      return found;
    });
    setDoc(again, original);
    await user.click(screen.getByRole("tab", { name: "Visual" }));
    await waitFor(() => {
      expect(screen.getAllByLabelText("Stage name").length).toBeGreaterThan(0);
    });
  });
});

describe("saving", () => {
  async function editAndSave(user: ReturnType<typeof userEvent.setup>) {
    const first = screen.getAllByLabelText("Stage name")[0] as HTMLInputElement;
    await user.clear(first);
    await user.type(first, "Compile");
    const save = screen.getByRole("button", { name: "Save pipeline" });
    await waitFor(() => {
      expect(save).toBeEnabled();
    });
    await user.click(save);
  }

  it("saves with the base version and shows the new version in the history", async () => {
    const user = userEvent.setup();
    renderApp(DEV);
    await ready();
    expect(await screen.findByText("Version 3")).toBeInTheDocument();
    await editAndSave(user);
    expect(await screen.findByText("Saved")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "View version 4" })).toBeInTheDocument();
  });

  it("handles a version conflict without overwriting", async () => {
    const user = userEvent.setup();
    renderApp(DEV);
    await ready();
    saveAsSomeoneElse("shop-api", "development", "pipeline { agent any } // theirs\n");
    await editAndSave(user);
    const alert = await screen.findByText("Someone else saved this pipeline first");
    expect(alert).toBeInTheDocument();
    expect(
      screen.getByText(/You started from version 3, but version 4 was saved by sam/),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Look at their version" }));
    expect(screen.getByLabelText("Their Jenkinsfile")).toHaveTextContent("// theirs");
    // keep mine: next save is based on version 4 and succeeds
    await user.click(screen.getByRole("button", { name: "Keep my changes on top of version 4" }));
    await user.click(screen.getByRole("button", { name: "Save pipeline" }));
    expect(await screen.findByText("Saved")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "View version 5" })).toBeInTheDocument();
  });

  it("can load their version instead", async () => {
    const user = userEvent.setup();
    renderApp(DEV);
    await ready();
    saveAsSomeoneElse("shop-api", "development", "pipeline { agent any } // theirs\n");
    await editAndSave(user);
    await user.click(
      await screen.findByRole("button", { name: /Load version 4 and discard my changes/ }),
    );
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
    const view = await openText(user);
    expect(view.state.doc.toString()).toContain("// theirs");
  });

  it("shows validation diagnostics returned on save", async () => {
    const user = userEvent.setup();
    renderApp(DEV);
    await ready();
    const view = await openText(user);
    setDoc(view, view.state.doc.toString() + "INVALID\n");
    await user.click(screen.getByRole("button", { name: "Save pipeline" }));
    expect(await screen.findByText(/Not saved: the pipeline is not valid/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /3:5.*INVALID is not a known step/ }),
    ).toBeInTheDocument();
  });

  it("guards unsaved changes when navigating away", async () => {
    const user = userEvent.setup();
    const { router } = renderApp(DEV);
    await ready();
    const first = screen.getAllByLabelText("Stage name")[0] as HTMLInputElement;
    await user.type(first, "!");
    await screen.findByText("Unsaved changes");
    await user.click(
      within(screen.getByRole("navigation", { name: "Main" })).getByRole("link", { name: "Runs" }),
    );
    const dialog = await screen.findByRole("alertdialog", { name: "Leave without saving?" });
    await user.click(within(dialog).getByRole("button", { name: "Stay and keep editing" }));
    expect(router.state.location.pathname).toBe(DEV);
    await user.click(
      within(screen.getByRole("navigation", { name: "Main" })).getByRole("link", { name: "Runs" }),
    );
    await user.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", {
        name: "Leave and discard changes",
      }),
    );
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/runs");
    });
  });

  it("starts from a rendered starter when the environment has no pipeline", async () => {
    const user = userEvent.setup();
    renderApp("/projects/billing-worker/environments/development/pipeline");
    expect(await screen.findByText("This environment has no pipeline yet.")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Save pipeline" })).toBeEnabled();
    });
    const view = await openText(user);
    expect(view.state.doc.toString()).toContain("stage('Build')");
  });
});

describe("read-only mode (Jenkinsfile in the repository)", () => {
  const REPO = "/projects/marketing-site/environments/development/pipeline";

  it("explains where to edit, disables every control and keeps history visible", async () => {
    const user = userEvent.setup();
    renderApp(REPO);
    expect(
      await screen.findByText(
        "This pipeline is defined by the Jenkinsfile in acme/marketing-site@main; edit it in the repository.",
      ),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getAllByLabelText("Stage name").length).toBeGreaterThan(0);
    });
    for (const input of screen.getAllByLabelText("Stage name")) expect(input).toBeDisabled();
    for (const button of screen.getAllByRole("button", { name: /^Move stage/ }))
      expect(button).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Save pipeline" })).not.toBeInTheDocument();
    expect(await screen.findByText("Version 2")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "View version 2" }));
    const dialog = await screen.findByRole("dialog", { name: "Version 2" });
    expect(
      within(dialog).queryByRole("button", { name: "Restore into the editor" }),
    ).not.toBeInTheDocument();
    const closers = within(dialog).getAllByRole("button", { name: "Close" });
    await user.click(closers[closers.length - 1] as HTMLElement);
    const view = await openText(user);
    expect(view.state.readOnly).toBe(true);
    expect(view.contentDOM.getAttribute("contenteditable")).toBe("false");
  });
});

describe("restore from history", () => {
  it("puts an old version into the editor as an unsaved change", async () => {
    const user = userEvent.setup();
    renderApp(DEV);
    await ready();
    await user.click(await screen.findByRole("button", { name: "View version 1" }));
    const dialog = await screen.findByRole("dialog", { name: "Version 1" });
    await user.click(
      await within(dialog).findByRole("button", { name: "Restore into the editor" }),
    );
    expect(await screen.findByText("Unsaved changes")).toBeInTheDocument();
    await waitFor(() => {
      expect(stageNames()).toEqual(["Build"]);
    });
  });
});

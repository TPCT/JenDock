import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { allows } from "../lib/permissions";
import { MOCK_ONE_TIME_PASSWORD, mockSession, setMockPersona, type Persona } from "../mocks/access";
import { MOCK_PASSWORD, MOCK_USERNAME } from "../mocks/data";
import { MOCK_WEB_ID } from "../mocks/containers";
import { setMockAuthenticated } from "../mocks/handlers";
import { renderApp } from "../test/render";

let consoleError: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
});
afterEach(() => {
  expect(consoleError).not.toHaveBeenCalled();
});

const DEV_RUNS = "/projects/shop-api/environments/development/runs";
const PROD_RUNS = "/projects/shop-api/environments/production/runs";
const WEB = `/projects/shop-api/environments/development/containers/${MOCK_WEB_ID}`;

function row(list: HTMLElement, name: RegExp | string) {
  const found = within(list)
    .getAllByRole("listitem")
    .find((item) => within(item).queryAllByText(name).length > 0);
  if (!found) throw new Error(`no row for ${String(name)}`);
  return found;
}

describe("allows (the permission summary)", () => {
  const summary = (persona: Persona) => {
    setMockPersona(persona);
    return mockSession().permissions;
  };

  it("lets owners and admins do everything", () => {
    const owner = summary("owner");
    expect(allows(owner, "user.manage")).toBe(true);
    expect(allows(owner, "domain.manage", { project: "anything", env: "production" })).toBe(true);
    expect(allows(summary("admin"), "access.manage", { project: "shop-api" })).toBe(true);
  });

  it("gives a member only what the grants give", () => {
    const viewer = summary("viewer");
    expect(allows(viewer, "run.read", { project: "shop-api", env: "development" })).toBe(true);
    expect(allows(viewer, "run.create", { project: "shop-api", env: "development" })).toBe(false);
    expect(allows(viewer, "user.manage")).toBe(false);
    const developer = summary("developer");
    expect(allows(developer, "run.create", { project: "shop-api", env: "development" })).toBe(true);
    expect(allows(developer, "run.create", { project: "shop-api", env: "production" })).toBe(false);
    expect(allows(developer, "run.create", { project: "other" })).toBe(false);
    const maintainer = summary("maintainer");
    expect(allows(maintainer, "run.create", { project: "shop-api", env: "production" })).toBe(true);
    expect(
      allows(maintainer, "container.terminal", { project: "shop-api", env: "development" }),
    ).toBe(true);
    expect(allows(maintainer, "secret.write", { project: "shop-api", env: "development" })).toBe(
      false,
    );
  });

  it("knows container-only grants", () => {
    const container = summary("container");
    const scope = { project: "shop-api", env: "development" as const };
    expect(allows(container, "container.logs", { ...scope, service: "web" })).toBe(true);
    expect(allows(container, "container.logs", { ...scope, service: "worker" })).toBe(false);
    expect(allows(container, "container.logs", scope)).toBe(false);
    expect(allows(container, "container.action", { ...scope, service: "web" })).toBe(false);
    expect(allows(undefined, "run.read", scope)).toBe(false);
  });
});

describe("Settings → Members", () => {
  it("lists members with their role and state", async () => {
    renderApp("/settings/members");
    const list = await screen.findByRole("list", { name: "Members" });
    expect(within(row(list, /Olivia Admin/)).getByText("Admin")).toBeInTheDocument();
    const dave = row(list, /Dave Dev/);
    expect(within(dave).getByText("Disabled")).toBeInTheDocument();
    expect(within(row(list, /admin$/)).getByText("You")).toBeInTheDocument();
    expect(within(row(list, /admin$/)).getByText("Owner")).toBeInTheDocument();
    expect(screen.getByText("4 members")).toBeInTheDocument();
  });

  it("creates a member and shows the one-time password exactly once", async () => {
    const user = userEvent.setup();
    renderApp("/settings/members");
    await screen.findByRole("list", { name: "Members" });
    await user.click(screen.getByRole("button", { name: "Add member" }));
    const dialog = await screen.findByRole("dialog", { name: "Add a member" });
    await user.type(within(dialog).getByLabelText(/Username/), "Not Valid!");
    await user.click(within(dialog).getByRole("button", { name: "Create member" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/3 to 64 lowercase/);
    await user.clear(within(dialog).getByLabelText(/Username/));
    await user.type(within(dialog).getByLabelText(/Username/), "nina");
    await user.type(within(dialog).getByLabelText("Name"), "Nina North");
    await user.click(within(dialog).getByRole("button", { name: "Create member" }));

    const shown = await screen.findByRole("dialog", { name: "Copy the one-time password" });
    expect(within(shown).getByText("This is the only time you will see it")).toBeInTheDocument();
    expect(within(shown).getByLabelText("One-time password of nina")).toHaveTextContent(
      MOCK_ONE_TIME_PASSWORD,
    );
    await user.click(within(shown).getByRole("button", { name: "I have copied it" }));
    expect(screen.queryByText(MOCK_ONE_TIME_PASSWORD)).not.toBeInTheDocument();
    const list = await screen.findByRole("list", { name: "Members" });
    const nina = row(list, /Nina North/);
    expect(within(nina).getByText("Must change password")).toBeInTheDocument();
    expect(within(nina).getByText("Member")).toBeInTheDocument();
  });

  it("offers the owner role to owners only", async () => {
    const user = userEvent.setup();
    renderApp("/settings/members");
    await screen.findByRole("list", { name: "Members" });
    await user.click(screen.getByRole("button", { name: "Add member" }));
    const dialog = await screen.findByRole("dialog", { name: "Add a member" });
    expect(within(dialog).getByRole("option", { name: "Owner" })).toBeInTheDocument();
  });

  it("hides the owner role from admins and their actions on owners", async () => {
    setMockPersona("admin");
    const user = userEvent.setup();
    renderApp("/settings/members");
    const list = await screen.findByRole("list", { name: "Members" });
    expect(within(row(list, /admin$/)).queryByRole("button")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add member" }));
    const dialog = await screen.findByRole("dialog", { name: "Add a member" });
    expect(within(dialog).queryByRole("option", { name: "Owner" })).not.toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: "Admin" })).toBeInTheDocument();
  });

  it("edits a member's role", async () => {
    const user = userEvent.setup();
    renderApp("/settings/members");
    const list = await screen.findByRole("list", { name: "Members" });
    await user.click(within(row(list, /Maria Lopez/)).getByRole("button", { name: /Actions for/ }));
    await user.click(await screen.findByRole("menuitem", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit maria" });
    await user.selectOptions(within(dialog).getByLabelText("Role"), "admin");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => {
      expect(within(row(list, /Maria Lopez/)).getByText("Admin")).toBeInTheDocument();
    });
  });

  it("disables and enables a member after a confirmation", async () => {
    const user = userEvent.setup();
    renderApp("/settings/members");
    const list = await screen.findByRole("list", { name: "Members" });
    await user.click(within(row(list, /Maria Lopez/)).getByRole("button", { name: /Actions for/ }));
    await user.click(await screen.findByRole("menuitem", { name: "Disable" }));
    const confirm = await screen.findByRole("alertdialog", { name: "Disable maria?" });
    expect(within(confirm).getByText(/signed out at once/)).toBeInTheDocument();
    await user.click(within(confirm).getByRole("button", { name: "Disable" }));
    await waitFor(() => {
      expect(within(row(list, /Maria Lopez/)).getByText("Disabled")).toBeInTheDocument();
    });
    await user.click(within(row(list, /Dave Dev/)).getByRole("button", { name: /Actions for/ }));
    await user.click(await screen.findByRole("menuitem", { name: "Enable" }));
    await user.click(
      await within(await screen.findByRole("alertdialog")).findByRole("button", { name: "Enable" }),
    );
    await waitFor(() => {
      expect(within(row(list, /Dave Dev/)).queryByText("Disabled")).not.toBeInTheDocument();
    });
  });

  it("resets a password and shows the new one-time password", async () => {
    const user = userEvent.setup();
    renderApp("/settings/members");
    const list = await screen.findByRole("list", { name: "Members" });
    await user.click(within(row(list, /Maria Lopez/)).getByRole("button", { name: /Actions for/ }));
    await user.click(await screen.findByRole("menuitem", { name: "Reset password" }));
    await user.click(
      await within(await screen.findByRole("alertdialog")).findByRole("button", {
        name: "Reset password",
      }),
    );
    const shown = await screen.findByRole("dialog", { name: "Copy the one-time password" });
    expect(within(shown).getByLabelText("One-time password of maria")).toHaveTextContent(
      MOCK_ONE_TIME_PASSWORD,
    );
  });

  it("deletes a member and never offers deleting or disabling yourself", async () => {
    const user = userEvent.setup();
    renderApp("/settings/members");
    const list = await screen.findByRole("list", { name: "Members" });
    await user.click(within(row(list, /admin$/)).getByRole("button", { name: /Actions for/ }));
    expect(await screen.findByRole("menuitem", { name: "Edit" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Delete" })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Disable" })).not.toBeInTheDocument();
    await user.keyboard("{Escape}");
    await user.click(within(row(list, /Dave Dev/)).getByRole("button", { name: /Actions for/ }));
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));
    await user.click(
      await within(await screen.findByRole("alertdialog")).findByRole("button", {
        name: "Delete member",
      }),
    );
    await waitFor(() => {
      expect(screen.queryByText(/Dave Dev/)).not.toBeInTheDocument();
    });
  });

  it("shows what a member can access", async () => {
    const user = userEvent.setup();
    renderApp("/settings/members");
    const list = await screen.findByRole("list", { name: "Members" });
    await user.click(within(row(list, /Maria Lopez/)).getByRole("button", { name: /Actions for/ }));
    await user.click(await screen.findByRole("menuitem", { name: "View access" }));
    const dialog = await screen.findByRole("dialog", { name: "Access of maria" });
    const grants = await within(dialog).findByRole("list", { name: "Grants of maria" });
    expect(within(grants).getByText(/All environments/)).toBeInTheDocument();
    expect(within(grants).getByText("logs")).toBeInTheDocument();
  });

  it("is not offered to members", async () => {
    setMockPersona("maintainer");
    renderApp("/settings/tokens");
    await screen.findByRole("heading", { name: "API tokens" });
    expect(screen.queryByRole("link", { name: "Members" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Account" })).toBeInTheDocument();
  });

  it("hides Integrations and Notifications from members but not from admins", async () => {
    setMockPersona("maintainer");
    const member = renderApp("/settings/tokens");
    await screen.findByRole("heading", { name: "API tokens" });
    expect(screen.queryByRole("link", { name: "Integrations" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Notifications" })).not.toBeInTheDocument();
    member.unmount();
    setMockPersona("admin");
    renderApp("/settings/tokens");
    expect(await screen.findByRole("link", { name: "Integrations" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Notifications" })).toBeInTheDocument();
  });

  it("is offered to owners", async () => {
    renderApp("/settings/tokens");
    expect(await screen.findByRole("link", { name: "Members" })).toBeInTheDocument();
  });
});

describe("project Settings → Access", () => {
  const ACCESS = "/projects/shop-api/settings/access";

  it("lists owners and admins and the members' grants", async () => {
    renderApp(ACCESS);
    const full = await screen.findByRole("list", { name: "Owners and admins" });
    expect(within(full).getByText("Olivia Admin (olivia)")).toBeInTheDocument();
    expect(within(full).getByText("Owner")).toBeInTheDocument();
    expect(await screen.findByText("Maria Lopez (maria)")).toBeInTheDocument();
    expect(screen.getByText("Entire project (all environments)")).toBeInTheDocument();
    expect(screen.getByText("Container web · Development")).toBeInTheDocument();
    expect(
      screen.getByRole("combobox", {
        name: "Role of maria on Entire project (all environments)",
      }),
    ).toHaveValue("viewer");
  });

  it("changes a role from the list", async () => {
    const user = userEvent.setup();
    renderApp(ACCESS);
    const select = await screen.findByRole("combobox", {
      name: "Role of maria on Entire project (all environments)",
    });
    await user.selectOptions(select, "maintainer");
    await waitFor(() => {
      expect(
        screen.getByRole("combobox", {
          name: "Role of maria on Entire project (all environments)",
        }),
      ).toHaveValue("maintainer");
    });
  });

  it("adds a role on one environment and removes it again", async () => {
    const user = userEvent.setup();
    renderApp(ACCESS);
    await screen.findByText("Maria Lopez (maria)");
    await user.click(screen.getByRole("button", { name: "Add people" }));
    const dialog = await screen.findByRole("dialog", { name: "Add people" });
    await user.selectOptions(
      within(dialog).getByLabelText(/Member/),
      "maria (Maria Lopez)".length ? "3" : "",
    );
    await user.click(within(dialog).getByLabelText("One environment"));
    await user.selectOptions(within(dialog).getByLabelText("Environment"), "production");
    await user.selectOptions(within(dialog).getByLabelText("Role"), "developer");
    expect(within(dialog).getByText(/Viewer, plus start, cancel and retry/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Add to project" }));
    const added = await screen.findByRole("combobox", {
      name: "Role of maria on Production environment",
    });
    expect(added).toHaveValue("developer");
    await user.click(
      screen.getByRole("button", { name: "Remove maria from Production environment" }),
    );
    await user.click(
      await within(await screen.findByRole("alertdialog")).findByRole("button", {
        name: "Remove access",
      }),
    );
    await waitFor(() => {
      expect(
        screen.queryByRole("combobox", { name: "Role of maria on Production environment" }),
      ).not.toBeInTheDocument();
    });
  });

  it("adds container permissions for one service", async () => {
    const user = userEvent.setup();
    renderApp(ACCESS);
    await screen.findByText("Maria Lopez (maria)");
    await user.click(screen.getByRole("button", { name: "Add people" }));
    const dialog = await screen.findByRole("dialog", { name: "Add people" });
    await user.selectOptions(within(dialog).getByLabelText(/Member/), "3");
    await user.click(within(dialog).getByLabelText(/One container service/));
    await user.selectOptions(within(dialog).getByLabelText("Environment"), "production");
    await user.type(within(dialog).getByLabelText(/Service/), "worker");
    await user.click(within(dialog).getByLabelText("terminal"));
    await user.click(within(dialog).getByRole("button", { name: "Add to project" }));
    expect(await screen.findByText("Container worker · Production")).toBeInTheDocument();
  });

  it("explains missing input before asking the server", async () => {
    const user = userEvent.setup();
    renderApp(ACCESS);
    await screen.findByText("Maria Lopez (maria)");
    await user.click(screen.getByRole("button", { name: "Add people" }));
    const dialog = await screen.findByRole("dialog", { name: "Add people" });
    await user.click(within(dialog).getByRole("button", { name: "Add to project" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Choose a member.");
  });

  it("is not in the project settings of a member", async () => {
    setMockPersona("maintainer");
    renderApp("/projects/shop-api");
    await screen.findByRole("heading", { name: "Shop API" });
    const tabs = screen.getByRole("navigation", { name: "Project" });
    expect(within(tabs).getByRole("link", { name: /Runs/ })).toBeInTheDocument();
    expect(within(tabs).queryByRole("link", { name: /Settings/ })).not.toBeInTheDocument();
  });

  it("is listed with the other settings for admins", async () => {
    renderApp("/projects/shop-api/settings");
    expect(await screen.findByRole("link", { name: "Access" })).toBeInTheDocument();
  });
});

describe("the UI hides what the user cannot do", () => {
  it("shows project creation to admins only", async () => {
    const { unmount } = renderApp("/projects");
    expect(await screen.findByRole("link", { name: "New project" })).toBeInTheDocument();
    unmount();
    setMockPersona("maintainer");
    renderApp("/projects");
    await screen.findByRole("heading", { name: "Projects" });
    await screen.findByRole("list", { name: "Projects" });
    expect(screen.queryByRole("link", { name: "New project" })).not.toBeInTheDocument();
  });

  it("hides Run pipeline from viewers", async () => {
    setMockPersona("viewer");
    renderApp(DEV_RUNS);
    await screen.findByRole("heading", { name: "Development runs" });
    expect(screen.queryByRole("button", { name: "Run pipeline" })).not.toBeInTheDocument();
  });

  it("lets a developer run development but not production", async () => {
    setMockPersona("developer");
    const first = renderApp(DEV_RUNS);
    expect(await screen.findByRole("button", { name: "Run pipeline" })).toBeInTheDocument();
    first.unmount();
    renderApp(PROD_RUNS);
    await screen.findByRole("heading", { name: "Production runs" });
    expect(screen.queryByRole("button", { name: "Run pipeline" })).not.toBeInTheDocument();
  });

  it("lets a maintainer run production", async () => {
    setMockPersona("maintainer");
    renderApp(PROD_RUNS);
    expect(await screen.findByRole("button", { name: "Run pipeline" })).toBeInTheDocument();
  });

  it("hides re-run and cancel from viewers on a run", async () => {
    setMockPersona("viewer");
    const running = renderApp("/runs/101");
    await screen.findByRole("heading", { name: /Run #/ });
    expect(screen.queryByRole("button", { name: "Cancel run" })).not.toBeInTheDocument();
    running.unmount();
    renderApp("/runs/102");
    await screen.findByRole("heading", { name: /Run #/ });
    expect(screen.queryByRole("button", { name: "Re-run" })).not.toBeInTheDocument();
  });

  it("shows re-run and cancel to maintainers", async () => {
    setMockPersona("maintainer");
    const running = renderApp("/runs/101");
    expect(await screen.findByRole("button", { name: "Cancel run" })).toBeInTheDocument();
    running.unmount();
    renderApp("/runs/102");
    expect(await screen.findByRole("button", { name: "Re-run" })).toBeInTheDocument();
  });

  it("hides container actions from viewers and shows them to maintainers", async () => {
    setMockPersona("viewer");
    const first = renderApp(WEB);
    await screen.findByRole("heading", { name: /web/ });
    expect(screen.queryByRole("button", { name: "Restart" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Terminal" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Logs" })).not.toBeInTheDocument();
    expect(await screen.findByRole("region", { name: "Resources" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit limits" })).not.toBeInTheDocument();
    first.unmount();
    setMockPersona("maintainer");
    renderApp(WEB);
    expect(await screen.findByRole("button", { name: "Restart" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Terminal" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Logs" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Edit limits" })).toBeInTheDocument();
  });

  it("gives a container-level grant exactly its permission", async () => {
    setMockPersona("container");
    renderApp(WEB);
    await screen.findByRole("heading", { name: /web/ });
    expect(screen.getByRole("tab", { name: "Logs" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Environment" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Files" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Restart" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Terminal" })).not.toBeInTheDocument();
  });

  it("hides domain and database configuration from viewers", async () => {
    setMockPersona("viewer");
    const domains = renderApp("/projects/shop-api/environments/development/domains");
    await screen.findByRole("heading", { name: "Development domains" });
    expect(screen.queryByRole("button", { name: "Add domain" })).not.toBeInTheDocument();
    domains.unmount();
    renderApp("/projects/shop-api/environments/development/databases");
    await screen.findByRole("heading", { name: "Development databases" });
    expect(screen.queryByRole("button", { name: "Add connection" })).not.toBeInTheDocument();
  });

  it("shows domain management to maintainers", async () => {
    setMockPersona("maintainer");
    renderApp("/projects/shop-api/environments/development/domains");
    expect(await screen.findByRole("button", { name: "Add domain" })).toBeInTheDocument();
  });

  it("makes a viewer's pipeline read-only", async () => {
    setMockPersona("viewer");
    renderApp("/projects/shop-api/environments/development/pipeline");
    expect(
      await screen.findByText("You can look at this pipeline but not change it."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save pipeline" })).not.toBeInTheDocument();
  });

  it("hides the pipeline shortcut when the user may not read it", async () => {
    setMockPersona("container");
    renderApp("/projects");
    await screen.findByRole("list", { name: "Projects" });
    expect(screen.queryByRole("link", { name: /pipeline of Shop API/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /runs of Shop API/ })).not.toBeInTheDocument();
  });
});

describe("forced password change", () => {
  const NEW = "a brand new passphrase";

  async function fill(user: ReturnType<typeof userEvent.setup>, values: [string, string, string]) {
    await user.type(screen.getByLabelText("Current password"), values[0]);
    await user.type(screen.getByLabelText("New password"), values[1]);
    await user.type(screen.getByLabelText("Repeat the new password"), values[2]);
    await user.click(screen.getByRole("button", { name: "Change password" }));
  }

  it("sends a signed-in user with a one-time password to the change screen", async () => {
    setMockPersona("pending");
    renderApp("/projects");
    expect(
      await screen.findByRole("heading", { name: "Choose a new password" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/gave you a one-time password/)).toBeInTheDocument();
  });

  it("checks the entries, then lands on the projects", async () => {
    setMockPersona("pending");
    const user = userEvent.setup();
    const { router } = renderApp("/projects");
    await screen.findByRole("heading", { name: "Choose a new password" });
    await fill(user, [MOCK_ONE_TIME_PASSWORD, "short", "short"]);
    expect(await screen.findByRole("alert")).toHaveTextContent(/at least 12 characters/);
    await user.clear(screen.getByLabelText("New password"));
    await user.clear(screen.getByLabelText("Repeat the new password"));
    await user.type(screen.getByLabelText("New password"), NEW);
    await user.type(screen.getByLabelText("Repeat the new password"), `${NEW}!`);
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/do not match/);
    await user.clear(screen.getByLabelText("Repeat the new password"));
    await user.type(screen.getByLabelText("Repeat the new password"), NEW);
    await user.click(screen.getByRole("button", { name: "Change password" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/projects");
    });
    expect(await screen.findByRole("heading", { name: "Projects" })).toBeInTheDocument();
  });

  it("shows the server's refusal", async () => {
    setMockPersona("pending");
    const user = userEvent.setup();
    renderApp("/change-password");
    await screen.findByRole("heading", { name: "Choose a new password" });
    await fill(user, ["not the one-time one", NEW, NEW]);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "the current password is not correct",
    );
  });

  it("is where a login with a one-time password leads", async () => {
    setMockAuthenticated(false);
    setMockPersona("pending");
    const user = userEvent.setup();
    const { router } = renderApp("/login");
    await user.type(await screen.findByLabelText("Username"), MOCK_USERNAME);
    await user.type(screen.getByLabelText("Password"), MOCK_PASSWORD);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/change-password");
    });
  });

  it("is also available as Settings → Account", async () => {
    const user = userEvent.setup();
    renderApp("/settings/account");
    await screen.findByRole("heading", { name: "Account" });
    await fill(user, [MOCK_PASSWORD, NEW, NEW]);
    expect(await screen.findByRole("status", { name: "" })).toHaveTextContent(
      "Your password was changed.",
    );
  });
});

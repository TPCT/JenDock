import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderApp } from "../test/render";
import { slugFrom } from "./NewProject";

let consoleError: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
});
afterEach(() => {
  expect(consoleError).not.toHaveBeenCalled();
});

type User = ReturnType<typeof userEvent.setup>;

async function continueTo(user: User) {
  await user.click(screen.getByRole("button", { name: "Continue" }));
}

describe("project creation wizard", () => {
  it("derives a short name from the project name", () => {
    expect(slugFrom("Shop API v2!")).toBe("shop-api-v2");
  });

  it("walks through six steps and creates the project with its starter pipeline", async () => {
    const user = userEvent.setup();
    renderApp("/projects/new");
    await screen.findByRole("heading", { name: "Create a project" });
    expect(screen.getByRole("list", { name: "Steps" })).toBeInTheDocument();

    // 1 repository (name and slug follow the repository until edited)
    await user.type(screen.getByLabelText(/GitHub repository/), "acme/new-app");
    expect(screen.getByLabelText(/Project name/)).toHaveValue("New App");
    expect(screen.getByLabelText(/Short name/)).toHaveValue("new-app");
    await continueTo(user);

    // 2 GitHub: deploy key + webhook instructions with the exact values
    expect(
      await screen.findByRole("heading", { name: /How should Jendock reach/ }),
    ).toBeInTheDocument();
    expect(screen.getByText("Allow write access", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByLabelText("Payload URL")).toHaveTextContent(
      `${window.location.origin}/api/github/webhook`,
    );
    expect(screen.getByLabelText("Content type")).toHaveTextContent("application/json");
    expect(screen.getByText("Just the push event")).toBeInTheDocument();
    await user.click(screen.getByLabelText(/HTTPS \(public repositories only\)/));
    expect(
      screen.queryByText("Allow write access", { selector: "strong" }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByLabelText(/SSH deploy key/));
    await continueTo(user);

    // 3 branches must differ
    const production = await screen.findByLabelText(/Production branch/);
    await user.clear(production);
    await user.type(production, "develop");
    await continueTo(user);
    expect(
      await screen.findByText("Development and Production must track different branches."),
    ).toBeInTheDocument();
    await user.clear(production);
    await user.type(production, "main");
    await continueTo(user);

    // 4 template with preview
    await user.click(await screen.findByLabelText(/^Laravel/));
    expect(await screen.findByLabelText("Jenkinsfile of Laravel")).toHaveTextContent("pipeline {");
    await continueTo(user);

    // 5 domain (optional): nothing chosen here
    expect(
      await screen.findByRole("heading", { name: /Give it a web address/ }),
    ).toBeInTheDocument();
    await continueTo(user);

    // 6 review and create
    expect(await screen.findByRole("heading", { name: "Review and create" })).toBeInTheDocument();
    expect(screen.getByText("None yet")).toBeInTheDocument();
    expect(screen.getByText("acme/new-app")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create project" }));
    expect(await screen.findByRole("heading", { name: "GitHub connection" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "New App" })).toBeInTheDocument();
  });

  it("explains an invalid repository and a taken short name", async () => {
    const user = userEvent.setup();
    renderApp("/projects/new");
    await screen.findByRole("heading", { name: "Create a project" });
    await continueTo(user);
    expect(await screen.findByText(/Write it as owner\/name/)).toBeInTheDocument();
    await user.type(screen.getByLabelText(/GitHub repository/), "acme/shop-api");
    await continueTo(user);
    await continueTo(user);
    await continueTo(user);
    await continueTo(user);
    await continueTo(user);
    await user.click(await screen.findByRole("button", { name: "Create project" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already exists");
  });
});

describe("project settings", () => {
  it("lists settings pages in a NavList and shows the GitHub instructions", async () => {
    const user = userEvent.setup();
    renderApp("/projects/shop-api/settings");
    await screen.findByRole("heading", { name: "General" });
    const nav = screen.getByRole("navigation", { name: "Project settings" });
    await user.click(within(nav).getByRole("link", { name: "GitHub connection" }));
    expect(await screen.findByLabelText("Payload URL")).toBeInTheDocument();
  });

  it("changes the access method and has a danger zone", async () => {
    const user = userEvent.setup();
    renderApp("/projects/shop-api/settings");
    await screen.findByRole("heading", { name: "General" });
    expect(screen.getByRole("heading", { name: "Danger zone" })).toBeInTheDocument();
    await user.click(screen.getByLabelText(/HTTPS \(public repositories only\)/));
    await user.click(screen.getByRole("button", { name: "Save access method" }));
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
  });

  it("turns automatic deploy on with its consequence and refuses equal branches", async () => {
    const user = userEvent.setup();
    renderApp("/projects/shop-api/settings/environments");
    const group = await screen.findByRole("group", { name: "Production environment" });
    const auto = within(group).getByRole("button", { name: "Automatic deploy" });
    expect(auto).toHaveAttribute("aria-pressed", "false");
    expect(
      within(group).getByText(/nothing runs until you press Run pipeline/),
    ).toBeInTheDocument();
    await user.click(auto);
    await waitFor(() => {
      expect(
        within(screen.getByRole("group", { name: "Production environment" })).getByRole("button", {
          name: "Automatic deploy",
        }),
      ).toHaveAttribute("aria-pressed", "true");
    });
    expect(
      within(screen.getByRole("group", { name: "Production environment" })).getByText(
        /builds and deploys production/,
      ),
    ).toBeInTheDocument();
    const branch = within(screen.getByRole("form", { name: "Production branch" })).getByLabelText(
      "Production branch",
    );
    await user.clear(branch);
    await user.type(branch, "develop");
    await user.click(screen.getByRole("button", { name: "Save production branch" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("different branches");
  });

  it("warns when automatic deploy is on but nothing is configured to deploy", async () => {
    renderApp("/projects/internal-tools/settings/environments");
    const group = await screen.findByRole("group", { name: "Development environment" });
    await userEvent.setup().click(within(group).getByRole("button", { name: "Automatic deploy" }));
    expect(await within(group).findByText("Not ready to deploy yet")).toBeInTheDocument();
  });
});

describe("secrets", () => {
  it("sets, reveals with an audit notice, rotates and deletes a secret", async () => {
    const user = userEvent.setup();
    renderApp("/projects/shop-api/settings/secrets");
    const list = await screen.findByRole("list", { name: "Development secrets" });
    expect(within(list).getByText("DB_PASSWORD")).toBeInTheDocument();
    expect(screen.queryByText(/value-of/)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "New secret" }));
    let dialog = await screen.findByRole("dialog", { name: "New Development secret" });
    await user.type(within(dialog).getByLabelText(/Name/), "bad name");
    await user.type(within(dialog).getByLabelText(/Value/), "s3cret");
    await user.click(within(dialog).getByRole("button", { name: "Add secret" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("capital letters");
    const name = within(dialog).getByLabelText(/Name/);
    await user.clear(name);
    await user.type(name, "API_KEY");
    await user.click(within(dialog).getByRole("button", { name: "Add secret" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(await screen.findByText("API_KEY")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Reveal API_KEY" }));
    const confirm = await screen.findByRole("alertdialog", { name: "Reveal API_KEY?" });
    expect(confirm).toHaveTextContent("recorded in the audit log");
    await user.click(within(confirm).getByRole("button", { name: "Reveal value" }));
    dialog = await screen.findByRole("dialog", { name: "API_KEY (revealed)" });
    expect(within(dialog).getByLabelText("Value of API_KEY")).toHaveTextContent("value-of-API_KEY");
    await user.click(within(dialog).getByRole("button", { name: "Hide and close" }));
    expect(screen.queryByText("value-of-API_KEY")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Rotate API_KEY" }));
    dialog = await screen.findByRole("dialog", { name: "Rotate API_KEY" });
    expect(within(dialog).getByLabelText(/Name/)).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/Value/), "new");
    await user.click(within(dialog).getByRole("button", { name: "Save new value" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: "Delete API_KEY" }));
    await user.click(
      within(await screen.findByRole("alertdialog", { name: "Delete API_KEY?" })).getByRole(
        "button",
        {
          name: "Delete secret",
        },
      ),
    );
    await waitFor(() => {
      expect(screen.queryByText("API_KEY")).not.toBeInTheDocument();
    });
  });
});

describe("API tokens", () => {
  it("lists tokens and shows a new token exactly once", async () => {
    const user = userEvent.setup();
    renderApp("/settings");
    expect(await screen.findByRole("heading", { name: "API tokens" })).toBeInTheDocument();
    const list = await screen.findByRole("list", { name: "API tokens" });
    expect(within(list).getByText("CI deploy")).toBeInTheDocument();
    expect(within(list).getByText("Revoked")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "New token" }));
    const dialog = await screen.findByRole("dialog", { name: "New API token" });
    await user.click(within(dialog).getByRole("button", { name: "Create token" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Give the token a name");
    await user.type(within(dialog).getByLabelText(/Name/), "Claude agent");
    await user.click(within(dialog).getByLabelText(/^run/));
    await user.click(within(dialog).getByRole("button", { name: "Create token" }));

    const shown = await screen.findByRole("dialog", { name: "Copy your new token" });
    expect(shown).toHaveTextContent("only time you will see it");
    const token = within(shown).getByLabelText("Token Claude agent").textContent;
    expect(token).toMatch(/^jdt_/);
    await user.click(within(shown).getByRole("button", { name: "I have copied it" }));
    expect(screen.queryByText(token)).not.toBeInTheDocument();
    const row = within(await screen.findByRole("list", { name: "API tokens" })).getByText(
      "Claude agent",
    );
    expect(row).toBeInTheDocument();
  });

  it("revokes a token after confirmation", async () => {
    const user = userEvent.setup();
    renderApp("/settings/tokens");
    await user.click(await screen.findByRole("button", { name: "Revoke CI deploy" }));
    await user.click(
      within(await screen.findByRole("alertdialog", { name: "Revoke CI deploy?" })).getByRole(
        "button",
        {
          name: "Revoke token",
        },
      ),
    );
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Revoke CI deploy" })).not.toBeInTheDocument();
    });
  });
});

describe("domains", () => {
  const DOMAINS = "/projects/shop-api/environments/development/domains";

  it("shows status, certificate and check results in plain words", async () => {
    renderApp(DOMAINS);
    const list = await screen.findByRole("list", { name: "Domains" });
    const shop = within(list).getByRole("listitem", { name: "shop.example.com" });
    expect(within(shop).getByText("Active")).toBeInTheDocument();
    expect(within(shop).getByText("Certificate valid, 62 days left")).toBeInTheDocument();
    expect(within(shop).getByText(/Checks passed/)).toBeInTheDocument();
    const api = within(list).getByRole("listitem", { name: "api.example.com" });
    expect(within(api).getByText("Last change failed")).toBeInTheDocument();
    expect(within(api).getByText("Certificate expires in 2 days: act now")).toBeInTheDocument();
    expect(within(api).getByText(/configtest failed, change reverted/)).toBeInTheDocument();
    expect(within(list).getByText("Draft: not on the server yet")).toBeInTheDocument();
  });

  it("previews DNS and the vhost diff, then applies with the plan", async () => {
    const user = userEvent.setup();
    renderApp(DOMAINS);
    await user.click(
      await screen.findByRole("button", { name: "Preview and apply staging.example.com" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Apply staging.example.com" });
    expect(
      await within(dialog).findByText(/Jendock will create this DNS record/),
    ).toBeInTheDocument();
    const diff = within(dialog).getByLabelText("Web server configuration changes");
    expect(diff).toHaveTextContent("+<VirtualHost *:80>");
    expect(within(dialog).getByText("DNS: create")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Apply changes" }));
    expect(await within(dialog).findByText(/Applied\./)).toBeInTheDocument();
    const closers = within(dialog).getAllByRole("button", { name: "Close" });
    await user.click(closers[closers.length - 1] as HTMLElement);
    const row = await screen.findByRole("listitem", { name: "staging.example.com" });
    await waitFor(() => {
      expect(within(row).getByText("Active")).toBeInTheDocument();
    });
    expect(within(row).getByText("Certificate valid, 89 days left")).toBeInTheDocument();
  });

  it("adds a domain, and a foreign DNS record blocks the apply in plain English", async () => {
    const user = userEvent.setup();
    renderApp(DOMAINS);
    await user.click(await screen.findByRole("button", { name: "Add domain" }));
    const add = await screen.findByRole("dialog", { name: "Add a domain to Development" });
    await user.click(within(add).getByRole("button", { name: "Add domain" }));
    expect(await within(add).findByRole("alert")).toHaveTextContent("Enter a host name such as");
    await user.type(within(add).getByLabelText(/Host name/), "Taken.Example.com");
    await user.click(within(add).getByRole("button", { name: "Add domain" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    await user.click(
      await screen.findByRole("button", { name: "Preview and apply taken.example.com" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Apply taken.example.com" });
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("not created by Jendock");
    expect(alert).toHaveTextContent("never overwrites a record it does not own");
    expect(within(dialog).getByRole("button", { name: "Apply changes" })).toBeDisabled();
  });

  it("refuses a duplicate host name from the server", async () => {
    const user = userEvent.setup();
    renderApp(DOMAINS);
    await user.click(await screen.findByRole("button", { name: "Add domain" }));
    const add = await screen.findByRole("dialog", { name: "Add a domain to Development" });
    await user.type(within(add).getByLabelText(/Host name/), "shop.example.com");
    await user.click(within(add).getByRole("button", { name: "Add domain" }));
    expect(await within(add).findByRole("alert")).toHaveTextContent("already used");
  });

  it("runs checks and reports a failing DNS check", async () => {
    const user = userEvent.setup();
    renderApp(DOMAINS);
    await user.click(
      await screen.findByRole("button", { name: "Run checks for shop.example.com" }),
    );
    await waitFor(() => {
      expect(screen.getAllByText(/Checks passed/).length).toBeGreaterThan(0);
    });
  });

  it("removes an active domain after previewing the removal", async () => {
    const user = userEvent.setup();
    renderApp(DOMAINS);
    await user.click(
      await screen.findByRole("button", { name: "More actions for shop.example.com" }),
    );
    await user.click(await screen.findByRole("menuitem", { name: /Remove from server/ }));
    const dialog = await screen.findByRole("dialog", { name: "Remove shop.example.com" });
    expect(
      await within(dialog).findByText(/Jendock will delete the DNS record it created/),
    ).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Remove from server" }));
    expect(await within(dialog).findByText(/Removed\./)).toBeInTheDocument();
  });

  it("deletes a draft after confirmation and issues a certificate", async () => {
    const user = userEvent.setup();
    renderApp(DOMAINS);
    await user.click(
      await screen.findByRole("button", { name: "More actions for staging.example.com" }),
    );
    await user.click(await screen.findByRole("menuitem", { name: /Delete draft/ }));
    await user.click(
      within(
        await screen.findByRole("alertdialog", { name: "Delete staging.example.com?" }),
      ).getByRole("button", {
        name: "Delete draft",
      }),
    );
    await waitFor(() => {
      expect(
        screen.queryByRole("listitem", { name: "staging.example.com" }),
      ).not.toBeInTheDocument();
    });
    await user.click(screen.getByRole("button", { name: "More actions for shop.example.com" }));
    await user.click(await screen.findByRole("menuitem", { name: /Issue or renew certificate/ }));
    expect(await screen.findByText("Certificate for shop.example.com issued.")).toBeInTheDocument();
  });

  it("switches environment and shows an empty state", async () => {
    const user = userEvent.setup();
    renderApp(DOMAINS);
    await screen.findByRole("heading", { name: "Development domains" });
    await user.click(screen.getByRole("button", { name: "Production" }));
    expect(await screen.findByRole("heading", { name: "No domains yet" })).toBeInTheDocument();
  });

  it("the Domains tab leads here", async () => {
    renderApp("/projects/shop-api/domains");
    expect(await screen.findByRole("heading", { name: "Development domains" })).toBeInTheDocument();
  });
});

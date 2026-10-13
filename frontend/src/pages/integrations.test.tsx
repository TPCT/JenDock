import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { integrationsState, setIntegrationsMock } from "../mocks/integrations";
import { server } from "../mocks/server";
import { renderApp } from "../test/render";

let consoleError: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
});
afterEach(() => {
  expect(consoleError).not.toHaveBeenCalled();
});

const SECRET = "cf-very-secret-token-0123456789";

describe("Connect an AI agent", () => {
  it("is live: no coming-soon copy, the endpoint and a placeholder snippet", async () => {
    renderApp("/settings/agents");
    expect(await screen.findByLabelText("MCP endpoint")).toHaveTextContent(
      `${window.location.origin}/mcp`,
    );
    expect(screen.queryByText(/coming soon/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/not available yet/i)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Claude Code command")).toHaveTextContent(
      `claude mcp add --transport http jendock ${window.location.origin}/mcp --header "Authorization: Bearer <paste your token here>"`,
    );
    expect(screen.getByLabelText("Generic MCP JSON configuration")).toHaveTextContent(
      '"mcpServers"',
    );
  });

  it("explains each preset and lists exactly the tools its scopes open up", async () => {
    const user = userEvent.setup();
    renderApp("/settings/agents");
    const tools = await screen.findByRole("list", { name: "Available tools" });
    await waitFor(() => {
      expect(within(tools).getByText("list_projects")).toBeInTheDocument();
    });
    expect(within(tools).queryByText("trigger_run")).not.toBeInTheDocument();
    const permissions = screen.getByRole("list", { name: "Permissions in this token" });
    expect(within(permissions).getByText("read")).toBeInTheDocument();

    await user.click(screen.getByLabelText(/^Run pipelines/));
    await waitFor(() => {
      expect(
        within(screen.getByRole("list", { name: "Available tools" })).getByText("trigger_run"),
      ).toBeInTheDocument();
    });
    expect(within(permissions).getByText("run")).toBeInTheDocument();
    expect(
      within(screen.getByRole("list", { name: "Available tools" })).queryByText("redeploy"),
    ).not.toBeInTheDocument();

    await user.click(screen.getByLabelText(/^Full admin/));
    await waitFor(() => {
      expect(
        within(screen.getByRole("list", { name: "Available tools" })).getByText("apply_domain"),
      ).toBeInTheDocument();
    });
  });

  it("creates a token, shows it once and fills it into the snippets", async () => {
    const user = userEvent.setup();
    renderApp("/settings/agents");
    await screen.findByLabelText("MCP endpoint");
    await user.click(screen.getByLabelText(/^Deploy/));
    await user.click(screen.getByRole("button", { name: "Create token" }));
    const token = await screen.findByLabelText("Token Claude agent");
    const value = token.textContent;
    expect(value).not.toContain("<paste");
    expect(value.length).toBeGreaterThan(16);
    expect(screen.getByText(/you will not see it again/)).toBeInTheDocument();
    expect(screen.getByLabelText("Claude Code command")).toHaveTextContent(`Bearer ${value}`);
    expect(screen.getByLabelText("Generic MCP JSON configuration")).toHaveTextContent(value);
  });

  it("states the deliberate limits in plain words", async () => {
    renderApp("/settings/agents");
    const limits = await screen.findByRole("list", { name: "Limits of agent access" });
    expect(within(limits).getByText("No secrets.")).toBeInTheDocument();
    expect(within(limits).getByText("No terminal.")).toBeInTheDocument();
  });
});

describe("Integrations: Cloudflare", () => {
  it("keeps the token write-only: saved, cleared from the field and never shown", async () => {
    const user = userEvent.setup();
    setIntegrationsMock({ tokenConfigured: false });
    renderApp("/settings/integrations");
    const form = await screen.findByRole("form", { name: "Cloudflare API token" });
    expect(screen.getByText("No token stored")).toBeInTheDocument();
    const field = within(form).getByLabelText("Cloudflare API token");
    expect(field).toHaveAttribute("type", "password");
    await user.type(field, SECRET);
    await user.click(within(form).getByRole("button", { name: "Save token" }));
    expect(await screen.findByText("Token stored")).toBeInTheDocument();
    expect(integrationsState.tokensSent).toEqual([SECRET]);
    expect(within(form).getByLabelText("Replace the token")).toHaveValue("");
    expect(document.body.textContent).not.toContain(SECRET);
  });

  it("tests the connection and lists the zone names only", async () => {
    const user = userEvent.setup();
    renderApp("/settings/integrations");
    await user.click(await screen.findByRole("button", { name: "Test connection" }));
    const zones = await screen.findByRole("list", { name: "Zones the token can edit" });
    expect(
      within(zones)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["acme.dev", "example.com"]);
    expect(screen.getByText("Cloudflare accepted the token")).toBeInTheDocument();
  });

  it("says plainly when Cloudflare rejects the token", async () => {
    const user = userEvent.setup();
    setIntegrationsMock({ tokenRejected: true });
    renderApp("/settings/integrations");
    await user.click(await screen.findByRole("button", { name: "Test connection" }));
    expect(await screen.findByText("The connection test failed")).toBeInTheDocument();
    expect(screen.getByText(/Cloudflare rejected the API token/)).toBeInTheDocument();
  });

  it("removes the token after confirmation", async () => {
    const user = userEvent.setup();
    renderApp("/settings/integrations");
    await user.click(await screen.findByRole("button", { name: "Remove token" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Remove the Cloudflare token?" });
    await user.click(within(dialog).getByRole("button", { name: "Remove token" }));
    expect(await screen.findByText("No token stored")).toBeInTheDocument();
  });

  it("shows why ingress cannot work and the host helper install commands", async () => {
    setIntegrationsMock({ helperReachable: false, enabled: false });
    renderApp("/settings/integrations");
    expect(await screen.findByText("Not ready yet")).toBeInTheDocument();
    const missing = screen.getByRole("list", { name: "What is missing" });
    expect(within(missing).getByText(/compose\.ingress\.yml overlay/)).toBeInTheDocument();
    expect(within(missing).getByText(/does not exist in the worker/)).toBeInTheDocument();
    expect(screen.getByText("Host helper unreachable")).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByText("How to install the host helper"));
    const commands = screen.getByLabelText("Host helper installation commands");
    expect(commands).toHaveTextContent("systemctl enable --now jendock-host-helper");
    expect(commands).toHaveTextContent("compose.ingress.yml up -d");
  });

  it("is ready when the helper, the token and the settings are in place", async () => {
    renderApp("/settings/integrations");
    expect(await screen.findByText("Ready to apply domains")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "What is missing" })).not.toBeInTheDocument();
    expect(screen.getByText(/Set an ACME contact email/)).toBeInTheDocument();
  });

  it("saves the ACME email and addresses with the environment default as hint", async () => {
    const user = userEvent.setup();
    renderApp("/settings/integrations");
    const form = await screen.findByRole("form", { name: "Certificate and DNS settings" });
    expect(within(form).getByLabelText("Server IPv4 address")).toHaveAttribute(
      "placeholder",
      "203.0.113.7",
    );
    expect(within(form).getByText(/the server default 203\.0\.113\.7 applies/)).toBeInTheDocument();
    await user.type(within(form).getByLabelText("ACME contact email"), "ops@example.com");
    await user.selectOptions(within(form).getByLabelText("Validation challenge"), "http-01");
    await user.click(within(form).getByRole("button", { name: "Save settings" }));
    expect(await within(form).findByText("Saved.")).toBeInTheDocument();
    expect(integrationsState.stored.acme_email).toBe("ops@example.com");
    expect(integrationsState.stored.acme_challenge).toBe("http-01");
    await waitFor(() => {
      expect(screen.queryByText(/Set an ACME contact email/)).not.toBeInTheDocument();
    });
  });

  it("explains a refused value", async () => {
    const user = userEvent.setup();
    renderApp("/settings/integrations");
    const form = await screen.findByRole("form", { name: "Certificate and DNS settings" });
    await user.type(within(form).getByLabelText("ACME contact email"), "nope");
    await user.click(within(form).getByRole("button", { name: "Save settings" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent("valid email");
  });
});

describe("Integrations: GitHub", () => {
  it("shows the webhook URL, the secret as yes/no and the deliveries", async () => {
    renderApp("/settings/integrations");
    expect(await screen.findByLabelText("Payload URL")).toHaveTextContent(
      `${window.location.origin}/api/github/webhook`,
    );
    expect(screen.getByText("Configured")).toBeInTheDocument();
    expect(screen.getByText("The value is never shown.")).toBeInTheDocument();
    expect(screen.getByText("Run queued: 14")).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Recent webhook deliveries" });
    expect(within(table).getAllByRole("row")).toHaveLength(4);
  });

  it("warns when the worker has no webhook secret", async () => {
    setIntegrationsMock({ webhookSecret: false });
    renderApp("/settings/integrations");
    expect(await screen.findByText("Not configured")).toBeInTheDocument();
    expect(screen.getByText("Deliveries will be rejected")).toBeInTheDocument();
  });

  it("shows public keys and creates a missing deploy key", async () => {
    const user = userEvent.setup();
    renderApp("/settings/integrations");
    const list = await screen.findByRole("list", { name: "Deploy keys" });
    const shop = within(list).getByRole("listitem", { name: "Shop API" });
    expect(within(shop).getByText("Deploy key exists")).toBeInTheDocument();
    expect(within(shop).getByLabelText("Public key of Shop API")).toHaveTextContent("ssh-ed25519");
    const billing = within(list).getByRole("listitem", { name: "Billing worker" });
    expect(within(billing).getByText("No deploy key")).toBeInTheDocument();
    await user.click(
      within(billing).getByRole("button", { name: "Create deploy key for Billing worker" }),
    );
    expect(await within(billing).findByLabelText("Public key of Billing worker")).toHaveTextContent(
      "ssh-ed25519",
    );
    expect(within(billing).getByText("Deploy key exists")).toBeInTheDocument();
  });

  it("reports an error from the server", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/integrations/github/projects/:slug/deploy-key", () =>
        HttpResponse.json(
          { error: { code: "conflict", message: "a deploy key already exists" } },
          { status: 409 },
        ),
      ),
    );
    renderApp("/settings/integrations");
    await user.click(
      await screen.findByRole("button", { name: "Create deploy key for Billing worker" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("already exists");
  });
});

describe("Domains page without Cloudflare", () => {
  const DOMAINS = "/projects/shop-api/environments/development/domains";

  it("shows a Banner linking to Integrations instead of failing", async () => {
    setIntegrationsMock({ tokenConfigured: false });
    renderApp(DOMAINS);
    const banner = await screen.findByRole("region", { name: "Cloudflare is not set up yet" });
    expect(within(banner).getByText(/No Cloudflare API token is stored yet/)).toBeInTheDocument();
    for (const link of within(banner).getAllByRole("link", { name: "Open Integrations" })) {
      expect(link).toHaveAttribute("href", "/settings/integrations");
    }
    expect(await screen.findByRole("list", { name: "Domains" })).toBeInTheDocument();
  });

  it("shows nothing when ingress is ready", async () => {
    renderApp(DOMAINS);
    await screen.findByRole("list", { name: "Domains" });
    expect(screen.queryByRole("region", { name: /Cloudflare is not set up yet/ })).toBeNull();
    expect(screen.queryByText("Domains cannot be applied yet")).not.toBeInTheDocument();
  });

  it("names the helper problem when the token is fine", async () => {
    setIntegrationsMock({ helperReachable: false });
    renderApp(DOMAINS);
    expect(await screen.findByText("Domains cannot be applied yet")).toBeInTheDocument();
    expect(screen.getByText(/Host helper: the socket/)).toBeInTheDocument();
  });
});

describe("Add domain with the Cloudflare zone picker", () => {
  it("builds the host name from a zone and a subdomain, with the environment's services", async () => {
    const user = userEvent.setup();
    renderApp("/projects/shop-api/environments/development/domains?add=1");
    const dialog = await screen.findByRole("dialog", { name: "Add a domain to Development" });
    const zone = await within(dialog).findByLabelText(/Domain \(Cloudflare zone\)/);
    await user.selectOptions(zone, "example.com");
    await user.type(within(dialog).getByLabelText("Subdomain"), "app");
    expect(within(dialog).getByText("app.example.com")).toBeInTheDocument();
    expect(within(dialog).getByLabelText(/^Service/).tagName).toBe("SELECT");
    await user.click(within(dialog).getByRole("button", { name: "Add domain" }));
    expect(await screen.findByRole("listitem", { name: "app.example.com" })).toBeInTheDocument();
  });

  it("explains that Cloudflare is not connected but still saves a draft", async () => {
    setIntegrationsMock({ tokenConfigured: false });
    renderApp("/projects/shop-api/environments/development/domains?add=1");
    const dialog = await screen.findByRole("dialog", { name: "Add a domain to Development" });
    expect(await within(dialog).findByText("Cloudflare is not connected yet")).toBeInTheDocument();
    expect(
      within(dialog).getByRole("link", { name: "Settings, Integrations" }),
    ).toBeInTheDocument();
    expect(within(dialog).getByLabelText(/^Host name/)).toBeInTheDocument();
  });

  it("the project overview has an Add domain entry point per environment", async () => {
    const user = userEvent.setup();
    renderApp("/projects/shop-api");
    await user.click(
      await screen.findByRole("link", { name: "Add a domain to production of Shop API" }),
    );
    expect(
      await screen.findByRole("dialog", { name: "Add a domain to Production" }),
    ).toBeInTheDocument();
  });
});

describe("project wizard: domain step", () => {
  async function toDomainStep(user: ReturnType<typeof userEvent.setup>) {
    renderApp("/projects/new");
    await screen.findByRole("heading", { name: "Create a project" });
    await user.type(screen.getByLabelText(/GitHub repository/), "acme/new-app");
    for (let i = 0; i < 4; i += 1) {
      await user.click(screen.getByRole("button", { name: "Continue" }));
    }
    return screen.findByRole("heading", { name: /Give it a web address/ });
  }

  it("links a domain: zone, subdomain, service, port, proxy; then lands on the Domains tab", async () => {
    const user = userEvent.setup();
    await toDomainStep(user);
    await user.click(await screen.findByLabelText("Give Development a domain"));
    const group = screen.getByRole("group", { name: "Development" });
    await user.selectOptions(
      await within(group).findByLabelText(/Domain \(Cloudflare zone\)/),
      "example.com",
    );
    await user.type(within(group).getByLabelText("Subdomain"), "app");
    const service = within(group).getByLabelText(/^Service/);
    await user.clear(service);
    await user.type(service, "api");
    const port = within(group).getByLabelText(/Container port/);
    await user.clear(port);
    await user.type(port, "3000");
    await user.click(within(group).getByLabelText("Proxy through Cloudflare"));
    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(await screen.findByRole("heading", { name: "Review and create" })).toBeInTheDocument();
    expect(screen.getByText(/app\.example\.com → api:3000 \(proxied\)/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create project" }));

    expect(await screen.findByRole("heading", { name: "Development domains" })).toBeInTheDocument();
    const banner = await screen.findByRole("region", {
      name: "Project created. Next step: put your domain online",
    });
    expect(within(banner).getByText(/app\.example\.com is saved as a draft/)).toBeInTheDocument();
    const draft = await screen.findByRole("listitem", { name: "app.example.com" });
    expect(within(draft).getByText("Draft: not on the server yet")).toBeInTheDocument();
    expect(
      within(draft).getByRole("button", { name: "Preview and apply app.example.com" }),
    ).toBeInTheDocument();
  });

  it("validates the domain before continuing", async () => {
    const user = userEvent.setup();
    await toDomainStep(user);
    await user.click(await screen.findByLabelText("Give Production a domain"));
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Enter a host name such as");
    expect(screen.getByRole("heading", { name: /Give it a web address/ })).toBeInTheDocument();
  });

  it("explains and links to Integrations when Cloudflare is not configured, without blocking", async () => {
    const user = userEvent.setup();
    setIntegrationsMock({ tokenConfigured: false });
    await toDomainStep(user);
    expect(await screen.findByText("Cloudflare is not connected yet")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Settings, Integrations" });
    expect(link).toHaveAttribute("href", "/settings/integrations");
    expect(screen.queryByLabelText("Give Development a domain")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("heading", { name: "Review and create" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create project" }));
    expect(await screen.findByRole("heading", { name: "GitHub connection" })).toBeInTheDocument();
  });

  it("still creates the project when the server refuses the domain, and says so", async () => {
    const user = userEvent.setup();
    await toDomainStep(user);
    await user.click(await screen.findByLabelText("Give Development a domain"));
    const group = screen.getByRole("group", { name: "Development" });
    await within(group).findByLabelText(/Domain \(Cloudflare zone\)/);
    await user.type(within(group).getByLabelText(/^Host name/), "shop.example.com");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(await screen.findByRole("button", { name: "Create project" }));
    expect(
      await screen.findByRole("region", { name: "shop.example.com was not saved" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "New App" })).toBeInTheDocument();
  });
});

describe("automatic deploy visibility", () => {
  it("offers the bulk action and lists what will be enabled", async () => {
    const user = userEvent.setup();
    renderApp("/projects");
    await screen.findByRole("article", { name: "Shop API" });
    await user.click(
      screen.getByRole("button", {
        name: "Turn on automatic deploy for all configured environments",
      }),
    );
    const dialog = await screen.findByRole("alertdialog", { name: "Turn on automatic deploy?" });
    const items = within(
      within(dialog).getByRole("list", { name: "Environments that will deploy automatically" }),
    ).getAllByRole("listitem");
    // Manual and configured: Shop API and Billing worker production. Internal tools has no
    // compose settings or pipeline, so it is not offered.
    expect(items.map((item) => item.textContent)).toEqual([
      "Shop API · Production · main",
      "Billing worker · Production · main",
    ]);
    await user.click(within(dialog).getByRole("button", { name: "Turn on for 2" }));
    expect(
      await screen.findByText(/Automatic deploy is on for 2 environments/),
    ).toBeInTheDocument();
    const shop = await screen.findByRole("article", { name: "Shop API" });
    await waitFor(() => {
      expect(within(shop).getAllByText("Auto-deploy on")).toHaveLength(2);
    });
    expect(
      screen.getByRole("button", {
        name: "Turn on automatic deploy for all configured environments",
      }),
    ).toBeDisabled();
  });

  it("cancelling changes nothing", async () => {
    const user = userEvent.setup();
    renderApp("/projects");
    await screen.findByRole("article", { name: "Shop API" });
    await user.click(
      screen.getByRole("button", {
        name: "Turn on automatic deploy for all configured environments",
      }),
    );
    const dialog = await screen.findByRole("alertdialog", { name: "Turn on automatic deploy?" });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    const shop = screen.getByRole("article", { name: "Shop API" });
    expect(within(shop).getAllByText("Auto-deploy on")).toHaveLength(1);
  });

  it("shows the label on the project overview too", async () => {
    renderApp("/projects/shop-api");
    expect(await screen.findByText("Auto-deploy off")).toBeInTheDocument();
    expect(screen.getByText("Auto-deploy on")).toBeInTheDocument();
  });
});

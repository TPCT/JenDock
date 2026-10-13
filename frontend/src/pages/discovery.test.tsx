import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setMockPersona } from "../mocks/access";
import { setDiscoveryMock } from "../mocks/discovery";
import { setIntegrationsMock } from "../mocks/integrations";
import { renderApp } from "../test/render";

let consoleError: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
});
afterEach(() => {
  expect(consoleError).not.toHaveBeenCalled();
});

async function listed(): Promise<HTMLElement> {
  return screen.findByRole("list", { name: "Discovered domains" });
}

describe("the global Domains page", () => {
  it("is in the left navigation for owners and lists what Cloudflare and Apache serve", async () => {
    renderApp("/domains");
    expect(await screen.findByRole("heading", { name: "Domains", level: 1 })).toBeInTheDocument();
    expect(
      within(screen.getByRole("navigation", { name: "Main" })).getByText("Domains"),
    ).toBeVisible();
    const list = await listed();
    const rows = within(list).getAllByRole("listitem");
    expect(rows.length).toBeGreaterThan(10);
    const shop = within(list).getByRole("listitem", { name: "shop.example.com" });
    expect(within(shop).getByText("Linked")).toBeInTheDocument();
    expect(within(shop).getByText("Managed")).toBeInTheDocument();
    expect(within(shop).getByRole("link", { name: /shop-api \/ Development/ })).toBeInTheDocument();
    const app = within(list).getByRole("listitem", { name: "app.example.com" });
    expect(within(app).getByText("Suggested")).toBeInTheDocument();
    expect(
      within(app).getByText(/published port of web of shop-api\/production/),
    ).toBeInTheDocument();
    expect(within(app).getByText(/Apache proxies to 127.0.0.1:20021, HTTPS/)).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "" })).toBeDefined();
  });

  it("searches, filters by status and by zone, and clears", async () => {
    const user = userEvent.setup();
    renderApp("/domains");
    await listed();
    await user.type(screen.getByRole("searchbox", { name: "Search domains" }), "marketing");
    await waitFor(() => {
      // marketing.* by name and www.* by its suggested project
      expect(
        within(screen.getByRole("list", { name: "Discovered domains" })).getAllByRole("listitem"),
      ).toHaveLength(2);
    });
    await user.click(screen.getByRole("button", { name: "Clear search" }));

    await user.click(await screen.findByRole("button", { name: "Status filter" }));
    await user.click(await screen.findByRole("menuitemradio", { name: "No match" }));
    await waitFor(() => {
      expect(screen.queryByRole("listitem", { name: "app.example.com" })).not.toBeInTheDocument();
    });
    expect(screen.getByRole("listitem", { name: "blog.example.com" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Zone filter" }));
    await user.click(await screen.findByRole("menuitemradio", { name: "example.net" }));
    await waitFor(() => {
      expect(screen.queryByRole("listitem", { name: "blog.example.com" })).not.toBeInTheDocument();
    });
    expect(screen.getByRole("listitem", { name: "grafana.example.net" })).toBeInTheDocument();
  });

  it("adopts one domain: the project's Domains tab shows it as adopted, without apply buttons", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/domains");
    const app = await screen.findByRole("listitem", { name: "app.example.com" });
    await user.click(within(app).getByRole("button", { name: "Adopt app.example.com" }));
    const dialog = await screen.findByRole("dialog", { name: "Adopt app.example.com" });
    expect(
      within(dialog).getByText(/DNS record and Apache site stay exactly as they are/),
    ).toBeInTheDocument();
    expect(within(dialog).getByLabelText(/Container port/)).toHaveValue(8080);
    expect(within(dialog).getByLabelText(/Published port/)).toHaveValue(20021);
    await user.click(within(dialog).getByRole("button", { name: "Adopt domain" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    const after = await screen.findByRole("listitem", { name: "app.example.com" });
    expect(within(after).getByText("Adopted")).toBeInTheDocument();
    expect(within(after).queryByRole("button", { name: /Adopt app/ })).not.toBeInTheDocument();

    await act(async () => {
      await router.navigate("/projects/shop-api/environments/production/domains");
    });
    await screen.findByRole("heading", { name: "Production domains" });
    const row = await screen.findByRole("listitem", { name: "app.example.com" });
    expect(within(row).getByText("Adopted")).toBeInTheDocument();
    expect(within(row).getByText(/managed outside Jendock/)).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: /Preview/ })).not.toBeInTheDocument();
    await user.click(within(row).getByRole("button", { name: "Run checks for app.example.com" }));
    expect(await within(row).findByText(/Apache site app.conf found/)).toBeInTheDocument();
    expect(
      within(row).getByText(/A record found: 203.0.113.7, proxied by Cloudflare/),
    ).toBeInTheDocument();
    await user.click(within(row).getByRole("button", { name: "More actions for app.example.com" }));
    expect(
      await screen.findByRole("menuitem", { name: /Unlink from Jendock/ }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /Remove from server/ })).not.toBeInTheDocument();
  });

  it("asks for the service and port when the match is by name only", async () => {
    const user = userEvent.setup();
    renderApp("/domains?q=tools");
    const row = await screen.findByRole("listitem", { name: "tools.example.net" });
    await user.click(within(row).getByRole("button", { name: "Adopt tools.example.net" }));
    const dialog = await screen.findByRole("dialog", { name: "Adopt tools.example.net" });
    await user.click(within(dialog).getByRole("button", { name: "Adopt domain" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Both ports must be numbers",
    );
  });

  it("adopts every certain suggestion at once and leaves the weak ones", async () => {
    const user = userEvent.setup();
    renderApp("/domains");
    await user.click(await screen.findByRole("button", { name: /Adopt all suggested \(5\)/ }));
    const dialog = await screen.findByRole("alertdialog", { name: /Adopt 5 suggested domains/ });
    expect(
      within(dialog).getByText(/Nothing changes in DNS or on the web server/),
    ).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Adopt all suggested" }));
    expect(await screen.findByText("Adopted 5 domains")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Adopt all suggested/ })).not.toBeInTheDocument();
    const weak = await screen.findByRole("listitem", { name: "shop-admin.example.com" });
    expect(within(weak).getByText("Suggested")).toBeInTheDocument();
  });

  it("reports what bulk adoption skipped", async () => {
    const user = userEvent.setup();
    setDiscoveryMock({ failBulkOn: "pay.example.net" });
    renderApp("/domains");
    await user.click(await screen.findByRole("button", { name: /Adopt all suggested/ }));
    await user.click(await screen.findByRole("button", { name: "Adopt all suggested" }));
    expect(await screen.findByText("Adopted 4 domains")).toBeInTheDocument();
    expect(
      screen.getByText(/pay.example.net \(service is not part of the environment/),
    ).toBeInTheDocument();
  });

  it("refreshes from Cloudflare", async () => {
    const user = userEvent.setup();
    setDiscoveryMock({ refreshedAt: new Date(Date.now() - 3 * 86_400_000).toISOString() });
    renderApp("/domains");
    const summary = (pattern: RegExp) => (_: string, element: Element | null) =>
      element?.tagName === "P" && pattern.test(element.textContent);
    expect(await screen.findByText(summary(/Refreshed 3 days ago/))).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Refresh from Cloudflare" }));
    await waitFor(() => {
      expect(screen.queryByText(summary(/Refreshed 3 days ago/))).not.toBeInTheDocument();
    });
    expect(screen.getByText(summary(/Refreshed .*(now|second|minute)/))).toBeInTheDocument();
  });

  it("without a Cloudflare token it says so and leads to Settings, Integrations", async () => {
    setIntegrationsMock({ tokenConfigured: false });
    renderApp("/domains");
    expect(
      await screen.findByRole("heading", { name: "Connect Cloudflare to find your domains" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Settings, Integrations" })).toHaveAttribute(
      "href",
      "/settings/integrations",
    );
    expect(screen.queryByRole("list", { name: "Discovered domains" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh from Cloudflare" })).toBeDisabled();
  });

  it("without the host helper it still lists the records and says to install it", async () => {
    setDiscoveryMock({ helper: "missing" });
    renderApp("/domains");
    expect(
      await screen.findByText("Install the host helper to see Apache routing"),
    ).toBeInTheDocument();
    const list = await listed();
    const row = within(list).getByRole("listitem", { name: "app.example.com" });
    expect(within(row).getByText(/Apache not read/)).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Adopt app.example.com" })).toBeInTheDocument();
  });

  it("is not offered to members", async () => {
    setMockPersona("maintainer");
    renderApp("/");
    await screen.findByRole("navigation", { name: "Main" });
    expect(
      within(screen.getByRole("navigation", { name: "Main" })).queryByText("Domains"),
    ).toBeNull();
  });
});

describe("a new domain for a project", () => {
  const DOMAINS = "/projects/shop-api/environments/development/domains";

  it("picks a zone, previews the full name, warns about an existing record and a taken name", async () => {
    const user = userEvent.setup();
    renderApp(DOMAINS);
    await user.click(await screen.findByRole("button", { name: "Add domain" }));
    const add = await screen.findByRole("dialog", { name: "Add a domain to Development" });
    await user.selectOptions(
      await within(add).findByLabelText(/Domain \(Cloudflare zone\)/),
      "example.com",
    );
    await user.type(within(add).getByLabelText("Subdomain"), "blog");
    expect(within(add).getByText("blog.example.com", { selector: "strong" })).toBeInTheDocument();
    const warning = await within(add).findByRole("alert");
    expect(warning).toHaveTextContent("A DNS record for blog.example.com already exists");
    expect(warning).toHaveTextContent("never overwrites a record it did not create");

    await user.clear(within(add).getByLabelText("Subdomain"));
    await user.type(within(add).getByLabelText("Subdomain"), "brand-new");
    expect(
      await within(add).findByText(/brand-new.example.com is free in the example.com zone/),
    ).toBeInTheDocument();

    await user.clear(within(add).getByLabelText("Subdomain"));
    await user.type(within(add).getByLabelText("Subdomain"), "shop");
    expect(await within(add).findByRole("alert")).toHaveTextContent(
      "shop.example.com is already used",
    );
  });

  it("creates the draft and opens the preview of DNS, Apache and the certificate", async () => {
    const user = userEvent.setup();
    renderApp(DOMAINS);
    await user.click(await screen.findByRole("button", { name: "Add domain" }));
    const add = await screen.findByRole("dialog", { name: "Add a domain to Development" });
    await user.selectOptions(
      await within(add).findByLabelText(/Domain \(Cloudflare zone\)/),
      "example.org",
    );
    await user.type(within(add).getByLabelText("Subdomain"), "launch");
    await user.click(within(add).getByRole("button", { name: "Add domain" }));
    const plan = await screen.findByRole("dialog", { name: "Apply launch.example.org" });
    expect(
      await within(plan).findByText(/Jendock will create this DNS record/),
    ).toBeInTheDocument();
    await user.click(within(plan).getByRole("button", { name: "Apply changes" }));
    expect(await within(plan).findByText(/Applied\./)).toBeInTheDocument();
  });

  it("shows the apply error clearly and leaves the draft", async () => {
    const user = userEvent.setup();
    renderApp(DOMAINS);
    await user.click(await screen.findByRole("button", { name: "Add domain" }));
    const add = await screen.findByRole("dialog", { name: "Add a domain to Development" });
    await user.type(await within(add).findByLabelText(/Host name/), "fail.example.com");
    await user.click(within(add).getByRole("button", { name: "Add domain" }));
    const plan = await screen.findByRole("dialog", { name: "Apply fail.example.com" });
    await user.click(await within(plan).findByRole("button", { name: "Apply changes" }));
    expect(await within(plan).findByRole("alert")).toHaveTextContent(
      "Cloudflare rejected the API token",
    );
  });
});

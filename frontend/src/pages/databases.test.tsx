import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { server } from "../mocks/server";
import { editorViews } from "../pipeline/CodeEditor";
import { renderApp } from "../test/render";
import { hexLines } from "../components/ResultTable";
import { explainTestError } from "./Databases";

beforeAll(async () => {
  await import("./DatabaseConsole");
}, 60_000);

let consoleError: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
});
afterEach(() => {
  expect(consoleError).not.toHaveBeenCalled();
});

const DBS = "/projects/shop-api/environments/development/databases";
const apiError = (status: number, code: string, message: string) =>
  HttpResponse.json({ error: { code, message } }, { status });

describe("connections", () => {
  it("lists connections with engine, address, TLS and last test", async () => {
    renderApp(DBS);
    const list = await screen.findByRole("list", { name: "Database connections" });
    const main = within(list).getByRole("listitem", { name: "main" });
    expect(within(main).getByText("PostgreSQL")).toBeInTheDocument();
    expect(within(main).getByText("reader@db:5432/shop")).toBeInTheDocument();
    expect(within(main).getByText("last test passed")).toBeInTheDocument();
    expect(within(main).getByText(/not encrypted/)).toBeInTheDocument();
    const tabs = screen.getByRole("navigation", { name: "Project" });
    expect(within(tabs).getByRole("link", { name: "Database" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("marks production clearly, with the audit and read-only-user advice", async () => {
    renderApp("/projects/shop-api/environments/production/databases");
    expect(await screen.findByText("Production data")).toBeInTheDocument();
    // the advice appears once on a production page: in the banner, not again under the card
    expect(screen.getAllByText(/only has SELECT privileges/)).toHaveLength(1);
    expect(screen.getByText(/every access is recorded in the audit log/)).toBeInTheDocument();
  });

  it("explains the empty state with the read-only recommendation", async () => {
    renderApp("/projects/internal-tools/environments/development/databases");
    expect(await screen.findByRole("heading", { name: "No connections yet" })).toBeInTheDocument();
    expect(screen.getAllByText(/only has SELECT privileges/).length).toBeGreaterThan(0);
  });

  it("adds a connection (password stored server side) and validates the form", async () => {
    const user = userEvent.setup();
    renderApp(DBS);
    await user.click(await screen.findByRole("button", { name: "Add connection" }));
    const dialog = await screen.findByRole("dialog", { name: "Add a database connection" });
    expect(dialog).toHaveTextContent("only has SELECT privileges");
    await user.clear(within(dialog).getByLabelText(/^Name/));
    await user.type(within(dialog).getByLabelText(/^Name/), "reporting");
    await user.type(within(dialog).getByLabelText(/^Database/), "reports");
    await user.type(within(dialog).getByLabelText(/^User/), "ro_user");
    await user.click(within(dialog).getByRole("button", { name: "Add connection" }));
    expect(await within(dialog).findByText("Enter the password.")).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText(/^Password/), "s3cret");
    await user.selectOptions(within(dialog).getByLabelText("Engine"), "mysql");
    expect(within(dialog).getByLabelText(/^Port/)).toHaveValue(3306);
    await user.click(within(dialog).getByRole("button", { name: "Add connection" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    const item = await screen.findByRole("listitem", { name: "reporting" });
    expect(within(item).getByText("MySQL")).toBeInTheDocument();
    expect(within(item).getByText("not tested")).toBeInTheDocument();
    expect(screen.queryByText("s3cret")).not.toBeInTheDocument();
  });

  it("refuses a loopback host and a duplicate name with the server's message", async () => {
    const user = userEvent.setup();
    renderApp(DBS);
    await user.click(await screen.findByRole("button", { name: "Add connection" }));
    const dialog = await screen.findByRole("dialog", { name: "Add a database connection" });
    await user.clear(within(dialog).getByLabelText(/^Host/));
    await user.type(within(dialog).getByLabelText(/^Host/), "localhost");
    await user.type(within(dialog).getByLabelText(/^Database/), "x");
    await user.type(within(dialog).getByLabelText(/^User/), "u");
    await user.type(within(dialog).getByLabelText(/^Password/), "p");
    await user.click(within(dialog).getByRole("button", { name: "Add connection" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("loopback");
    await user.clear(within(dialog).getByLabelText(/^Host/));
    await user.type(within(dialog).getByLabelText(/^Host/), "db");
    await user.click(within(dialog).getByRole("button", { name: "Add connection" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("already exists");
  });

  it("edits without re-entering the password", async () => {
    const user = userEvent.setup();
    renderApp(DBS);
    await user.click(await screen.findByRole("button", { name: "Edit main" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit main" });
    expect(within(dialog).getByText(/Leave empty to keep the stored password/)).toBeInTheDocument();
    const user_ = within(dialog).getByLabelText(/^User/);
    await user.clear(user_);
    await user.type(user_, "reader2");
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(await screen.findByText("reader2@db:5432/shop")).toBeInTheDocument();
  });

  it("tests a connection and shows the server details", async () => {
    const user = userEvent.setup();
    renderApp(DBS);
    await user.click(await screen.findByRole("button", { name: "Test connection main" }));
    expect(await screen.findByText("Connection works")).toBeInTheDocument();
    expect(screen.getByText(/PostgreSQL 16\.3, database shop as reader/)).toBeInTheDocument();
    expect(screen.getByText(/The session is read-only, as intended/)).toBeInTheDocument();
  });

  it.each([
    ["connect_failed", "database did not accept the connection"],
    ["network_denied", "outside the environment's private network"],
    ["network_unavailable", "Deploy the environment first"],
    ["secret_missing", "enter the password again"],
    ["helper_unavailable", "could not be started"],
    ["timeout", "within 30 seconds"],
  ])("explains the test failure %s in plain English", (code, text) => {
    expect(explainTestError(code, "detail")).toContain(text);
  });

  it("shows a failed test as an alert", async () => {
    server.use(
      http.post("/api/projects/:slug/environments/:env/databases/:name/test", () =>
        HttpResponse.json({
          ok: false,
          server: null,
          network: "x_default",
          error: { code: "network_denied", message: "172.17.0.1 is the gateway" },
        }),
      ),
    );
    const user = userEvent.setup();
    renderApp(DBS);
    await user.click(await screen.findByRole("button", { name: "Test connection main" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "outside the environment's private network",
    );
  });

  it("deletes after confirmation", async () => {
    const user = userEvent.setup();
    renderApp(DBS);
    await user.click(await screen.findByRole("button", { name: "Delete main" }));
    await user.click(
      within(await screen.findByRole("alertdialog", { name: "Delete main?" })).getByRole("button", {
        name: "Delete connection",
      }),
    );
    expect(await screen.findByRole("heading", { name: "No connections yet" })).toBeInTheDocument();
  });
});

describe("schema and table browser", () => {
  it("lists schemas and tables with estimated row counts, then shows structure", async () => {
    const user = userEvent.setup();
    renderApp(`${DBS}/main`);
    const nav = await screen.findByRole("navigation", { name: "Tables" });
    expect(within(nav).getByRole("link", { name: /orders/ })).toHaveTextContent("~250");
    expect(within(nav).getByRole("link", { name: /v_totals/ })).toHaveTextContent("view");
    expect(screen.getByLabelText("Schema")).toHaveValue("public");
    await user.click(within(nav).getByRole("link", { name: /orders/ }));
    const columns = await screen.findByRole("table", { name: "Columns of the table" });
    expect(within(columns).getByText("numeric(10,2)")).toBeInTheDocument();
    expect(within(columns).getByText("primary key 1")).toBeInTheDocument();
    expect(await screen.findByText(/orders_customer_idx/)).toBeInTheDocument();
    expect(screen.getByText(/references/)).toHaveTextContent("public.customers(id)");
    expect(screen.getByText(/On delete: CASCADE/)).toBeInTheDocument();
  });

  it("shows system schemas on request and an empty schema", async () => {
    const user = userEvent.setup();
    renderApp(`${DBS}/main`);
    const schema = await screen.findByLabelText("Schema");
    expect(within(schema).queryByRole("option", { name: /pg_catalog/ })).not.toBeInTheDocument();
    await user.click(screen.getByLabelText("Show system schemas"));
    expect(
      await within(schema).findByRole("option", { name: "pg_catalog (system)" }),
    ).toBeInTheDocument();
    await user.selectOptions(schema, "billing");
    expect(await screen.findByText("This schema has no tables.")).toBeInTheDocument();
  });

  it("says when the connection does not exist", async () => {
    renderApp(`${DBS}/nope`);
    expect(
      await screen.findByRole("heading", { name: "Connection not found" }),
    ).toBeInTheDocument();
  });
});

describe("rows", () => {
  const ROWS = `${DBS}/main?schema=public&table=orders&tab=rows`;

  it("pages with keyset cursors forwards and back", async () => {
    const urls: string[] = [];
    server.events.on("request:start", ({ request }) => {
      if (request.url.includes("/rows")) urls.push(new URL(request.url).search);
    });
    const user = userEvent.setup();
    renderApp(ROWS);
    const table = await screen.findByRole("table", { name: "Rows of orders" });
    expect(within(table).getAllByRole("row")).toHaveLength(101);
    expect(screen.getByText(/Ordered by the primary key \(id\)/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => {
      expect(
        within(screen.getByRole("table", { name: "Rows of orders" })).getByText("101"),
      ).toBeInTheDocument();
    });
    expect(urls.some((search) => search.includes("cursor="))).toBe(true);
    await user.click(screen.getByRole("button", { name: "Previous" }));
    await waitFor(() => {
      expect(
        within(screen.getByRole("table", { name: "Rows of orders" })).queryByText("101"),
      ).not.toBeInTheDocument();
    });
    server.events.removeAllListeners();
  });

  it("changes the page size and reaches the last page", async () => {
    const user = userEvent.setup();
    renderApp(ROWS);
    await screen.findByRole("table", { name: "Rows of orders" });
    await user.selectOptions(screen.getByLabelText("Rows per page"), "500");
    await waitFor(() => {
      expect(
        within(screen.getByRole("table", { name: "Rows of orders" })).getAllByRole("row"),
      ).toHaveLength(251);
    });
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("shows NULL, truncated text with the full value, and binary with hex", async () => {
    const user = userEvent.setup();
    renderApp(ROWS);
    const table = await screen.findByRole("table", { name: "Rows of orders" });
    expect(within(table).getAllByText("NULL").length).toBeGreaterThan(0);
    await user.click(within(table).getByRole("button", { name: "Show full value of notes" }));
    const text = await screen.findByRole("dialog", { name: "Value of notes" });
    expect(text).toHaveTextContent("first 4,096 bytes");
    expect(within(text).getByLabelText("Full value").textContent).toHaveLength(4096);
    const closers = within(text).getAllByRole("button", { name: "Close" });
    await user.click(closers[closers.length - 1] as HTMLElement);
    await user.click(within(table).getByRole("button", { name: "Show hex of payload" }));
    const hex = await screen.findByRole("dialog", { name: "Binary value of payload" });
    expect(hex).toHaveTextContent("8 of 18,321 bytes");
    expect(within(hex).getByLabelText("Hex dump")).toHaveTextContent("89 50 4e 47 0d 0a 1a 0a");
    expect(within(table).getByText(/binary, 17\.9 KiB/)).toBeInTheDocument();
  });

  it("explains position paging for a table without a primary key", async () => {
    const user = userEvent.setup();
    renderApp(`${DBS}/main?schema=public&table=audit_log&tab=rows`);
    expect(await screen.findByText(/no primary key or sortable columns/)).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Rows per page"), "50");
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("shows the server's refusal when production data needs admin", async () => {
    server.use(
      http.get(
        "/api/projects/:slug/environments/:env/databases/:name/schemas/:schema/tables/:table/rows",
        () => apiError(403, "forbidden", "production data requires the admin scope"),
      ),
    );
    renderApp(
      `/projects/shop-api/environments/production/databases/main?schema=public&table=orders&tab=rows`,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("requires the admin scope");
    expect(screen.getByText(/recorded in the audit log/)).toBeInTheDocument();
  });

  it("formats hex dumps eight bytes per line", () => {
    expect(hexLines("0001020304050607080910")).toBe("00 01 02 03 04 05 06 07\n08 09 10");
  });
});

describe("SQL console", () => {
  const CONSOLE = `${DBS}/main/console`;

  async function open() {
    renderApp(CONSOLE);
    await screen.findByRole("heading", { name: "SQL console: main" });
    await waitFor(() => {
      expect(editorViews).toHaveLength(1);
    });
    const view = editorViews[0];
    if (!view) throw new Error("no editor");
    return view;
  }

  function type(view: (typeof editorViews)[number], text: string) {
    act(() => {
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } });
    });
  }

  it("explains the limits and runs a statement into a results table", async () => {
    const user = userEvent.setup();
    const view = await open();
    await user.click(screen.getByText("What can I run here?"));
    expect(
      screen.getByText(/stopped after 5 seconds and show at most 500 rows/),
    ).toBeInTheDocument();
    expect(view.contentDOM.getAttribute("aria-label")).toBe("SQL statement");
    type(view, "SELECT * FROM orders WHERE id < 10;");
    await user.click(screen.getByRole("button", { name: "Run" }));
    const result = await screen.findByRole("region", { name: "Result" });
    expect(within(result).getByText(/SELECT: 3 rows in 12 ms/)).toBeInTheDocument();
    expect(within(result).getByRole("table", { name: "Query result" })).toBeInTheDocument();
    expect(within(result).getByRole("button", { name: "Show hex of payload" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Recent statements" })).toHaveTextContent(
      "SELECT * FROM orders WHERE id < 10;",
    );
  });

  it("runs with the keyboard shortcut", async () => {
    const view = await open();
    type(view, "SELECT 1");
    act(() => {
      view.contentDOM.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", ctrlKey: true, bubbles: true }),
      );
    });
    expect(await screen.findByRole("region", { name: "Result" })).toBeInTheDocument();
  });

  it("says when the result was cut at 500 rows", async () => {
    const user = userEvent.setup();
    const view = await open();
    type(view, "SELECT n FROM big");
    await user.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText("Showing the first rows only")).toBeInTheDocument();
    expect(screen.getByText(/cut at 500 rows/)).toBeInTheDocument();
  });

  it.each([
    ["DELETE FROM orders", "only SELECT, WITH, SHOW and EXPLAIN"],
    ["SELECT 1; SELECT 2", "only one statement"],
    ["SELECT pg_sleep(10)", "function that is not allowed"],
    ["SELECT * FROM orders WHERE id IN (SELECT id FROM orders FOR UPDATE)", "SELECT 1"],
  ])("shows a rejected statement clearly: %s", async (statement, text) => {
    const user = userEvent.setup();
    const view = await open();
    type(view, statement);
    await user.click(screen.getByRole("button", { name: "Run" }));
    const alert = await screen.findByRole("alert");
    if (text !== "SELECT 1") expect(alert).toHaveTextContent(text);
    expect(alert).toHaveTextContent("The statement was not run");
  });

  it("explains a timeout and an unreachable database", async () => {
    const user = userEvent.setup();
    const view = await open();
    type(view, "SELECT slow()");
    await user.click(screen.getByRole("button", { name: "Run" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The statement took too long");
    expect(alert).toHaveTextContent("5 seconds");
    server.use(
      http.post("/api/projects/:slug/environments/:env/databases/:name/query", () =>
        apiError(409, "conflict", "the database is not reachable"),
      ),
    );
    type(view, "SELECT 2");
    await user.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("Use Test connection");
    });
  });

  it("explains the admin requirement and the audit on production", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/projects/:slug/environments/:env/databases/:name/query", () =>
        apiError(403, "forbidden", "production data requires the admin scope"),
      ),
    );
    renderApp("/projects/shop-api/environments/production/databases/main/console");
    expect(await screen.findByText(/recorded in the audit log/)).toBeInTheDocument();
    await waitFor(() => {
      expect(editorViews).toHaveLength(1);
    });
    await user.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("needs the admin permission");
  });

  it("counts characters against the limit and disables Run above it", async () => {
    const view = await open();
    type(view, "SELECT '" + "x".repeat(20_001) + "'");
    expect(await screen.findByText(/20,01\d \/ 20,000 characters/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
  });

  it("reuses a statement from the history", async () => {
    const user = userEvent.setup();
    const view = await open();
    type(view, "SELECT 41");
    await user.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByRole("region", { name: "Result" });
    type(view, "SELECT 42");
    await user.click(screen.getByRole("button", { name: /SELECT 41/ }));
    expect(view.state.doc.toString()).toBe("SELECT 41");
  });
});

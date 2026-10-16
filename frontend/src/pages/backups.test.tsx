import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { resetBackupsMock, s3Settings, backupSecretsSent, backupRows } from "../mocks/backups";
import { setMockPersona } from "../mocks/access";
import { renderApp } from "../test/render";

afterEach(() => {
  setMockPersona("owner");
  resetBackupsMock();
  s3Settings.secret_configured = true;
});

describe("S3 database backup settings", () => {
  it("shows defaults, keeps the secret write-only, tests the connection and saves settings", async () => {
    const user = userEvent.setup();
    resetBackupsMock();
    renderApp("/settings/integrations");

    const form = await screen.findByLabelText("S3 backups");
    expect(within(form).getByLabelText("AWS access key ID")).toHaveValue("AKIAEXAMPLE");
    expect(within(form).getByLabelText("AWS secret access key")).toHaveAttribute(
      "type",
      "password",
    );
    expect(within(form).getByLabelText("AWS secret access key")).toHaveAttribute(
      "placeholder",
      "Saved; leave blank to keep it",
    );
    expect(within(form).getByLabelText("Daily schedule (Africa/Cairo)")).toHaveValue("3");
    expect(within(form).getByLabelText("Enable daily scheduled backups")).not.toBeChecked();

    await user.click(within(form).getByRole("button", { name: "Test connection" }));
    expect(await within(form).findByText(/S3 connection verified/)).toBeInTheDocument();
    await user.type(within(form).getByLabelText("AWS secret access key"), "private-secret-value");
    await user.click(within(form).getByRole("button", { name: "Save S3 settings" }));
    await waitFor(() => {
      expect(backupSecretsSent).toContain("private-secret-value");
    });
    expect(document.body.textContent).not.toContain("private-secret-value");
    expect(within(form).getByLabelText("Least-privilege IAM policy")).toHaveTextContent(
      "s3:AbortMultipartUpload",
    );
    expect(
      within(form).getByRole("button", { name: "Copy Least-privilege IAM policy" }),
    ).toBeInTheDocument();
  });
});

describe("database backup history and actions", () => {
  it("shows environment history and disables a connection while its latest run is active", async () => {
    resetBackupsMock();
    setMockPersona("maintainer");
    backupRows.unshift({
      ...backupRows[0],
      id: 44,
      project: "shop-api",
      environment: "development",
      connection: "main",
      trigger: "schedule",
      status: "running",
      started_at: new Date().toISOString(),
      finished_at: null,
      size_bytes: 64 * 1024,
      object_key: "backups/shop-api/development/main/2026-10-16T030000.dump",
      error: null,
    });
    renderApp("/projects/shop-api/environments/development/databases");

    const history = await screen.findByRole("list", { name: "Environment backup runs" });
    expect(within(history).getAllByText("main")).toHaveLength(2);
    expect(within(history).getByText("running")).toBeInTheDocument();
    expect(within(history).getByText(/Scheduled/)).toBeInTheDocument();
    expect(
      within(history).getByText("backups/shop-api/development/main/2026-10-16T030000.dump"),
    ).toBeInTheDocument();
    expect(within(history).getByText("database backup could not be completed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Back up now" })).toBeDisabled();
  });

  it("lists backup records and only offers manual runs to users with backup.run", async () => {
    renderApp("/backups");
    const history = await screen.findByRole("list", { name: "Database backup runs" });
    expect(within(history).getByText("shop-api/production/main")).toBeInTheDocument();
    expect(within(history).getByText("succeeded")).toBeInTheDocument();

    setMockPersona("viewer");
    const viewer = renderApp("/projects/shop-api/environments/development/databases");
    await screen.findByText("Development databases");
    expect(screen.queryByRole("button", { name: "Back up now" })).not.toBeInTheDocument();
    viewer.unmount();

    setMockPersona("maintainer");
    const { unmount } = renderApp("/projects/shop-api/environments/development/databases");
    await screen.findByText("Development databases");
    const run = await screen.findByRole("button", { name: "Back up now" });
    const user = userEvent.setup();
    await user.click(run);
    await waitFor(() => {
      expect(screen.getAllByText("running")).toHaveLength(2);
    });
    unmount();
  });
});

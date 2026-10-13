import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderApp } from "../test/render";

let consoleError: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
});
afterEach(() => {
  expect(consoleError).not.toHaveBeenCalled();
});

const SLACK_URL = "https://hooks.slack.com/services/T000/B000/abcdWXYZ";

async function openChannelDialog(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "New channel" }));
  return screen.findByRole("dialog", { name: "New notification channel" });
}

describe("Settings, Notifications: channels", () => {
  it("lists channels as configured and never shows a webhook URL", async () => {
    renderApp("/settings/notifications");
    const list = await screen.findByRole("list", { name: "Notification channels" });
    expect(within(list).getByText("#deploys")).toBeInTheDocument();
    expect(within(list).getByText(/Webhook URL configured, ends in x7Qa/)).toBeInTheDocument();
    expect(within(list).getByText("Slack")).toBeInTheDocument();
    expect(screen.queryByText(/https:\/\/hooks/)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Notifications" })).toHaveAttribute(
      "href",
      "/settings/notifications",
    );
  });

  it("creates a Slack channel with a write-only URL field", async () => {
    const user = userEvent.setup();
    renderApp("/settings/notifications");
    const dialog = await openChannelDialog(user);
    const url = within(dialog).getByLabelText(/Webhook URL/);
    expect(url).toHaveAttribute("type", "password");
    await user.type(within(dialog).getByLabelText(/^Name/), "#alerts");
    await user.type(url, "http://insecure.example/hook");
    await user.click(within(dialog).getByRole("button", { name: "Add channel" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("must start with https://");
    await user.clear(url);
    await user.type(url, SLACK_URL);
    await user.click(within(dialog).getByRole("button", { name: "Add channel" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    const list = await screen.findByRole("list", { name: "Notification channels" });
    expect(within(list).getByText("#alerts")).toBeInTheDocument();
    expect(within(list).getByText(/ends in WXYZ/)).toBeInTheDocument();
    expect(screen.queryByText(SLACK_URL)).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(SLACK_URL);
  });

  it("explains a duplicate name from the server", async () => {
    const user = userEvent.setup();
    renderApp("/settings/notifications");
    const dialog = await openChannelDialog(user);
    await user.type(within(dialog).getByLabelText(/^Name/), "#deploys");
    await user.type(within(dialog).getByLabelText(/Webhook URL/), SLACK_URL);
    await user.click(within(dialog).getByRole("button", { name: "Add channel" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("already exists");
  });

  it("collects SMTP settings and checks the required ones", async () => {
    const user = userEvent.setup();
    renderApp("/settings/notifications");
    const dialog = await openChannelDialog(user);
    await user.type(within(dialog).getByLabelText(/^Name/), "ops mail");
    await user.selectOptions(within(dialog).getByLabelText("Type"), "email");
    await user.click(within(dialog).getByRole("button", { name: "Add channel" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("SMTP host");
    await user.type(within(dialog).getByLabelText(/SMTP host/), "smtp.example.com");
    await user.type(within(dialog).getByLabelText(/^From/), "jendock@example.com");
    await user.type(within(dialog).getByLabelText(/^To/), "ops@example.com, dev@example.com");
    await user.type(within(dialog).getByLabelText(/SMTP username/), "mailer");
    await user.click(within(dialog).getByRole("button", { name: "Add channel" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("SMTP password");
    const password = within(dialog).getByLabelText(/SMTP password/);
    expect(password).toHaveAttribute("type", "password");
    await user.type(password, "hunter2");
    await user.click(within(dialog).getByRole("button", { name: "Add channel" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    const list = await screen.findByRole("list", { name: "Notification channels" });
    expect(
      within(list).getByText(/smtp\.example\.com:587 to ops@example\.com, dev@example\.com/),
    ).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("hunter2");
  });

  it("sends a test and reports success, then a failure with the safe reason", async () => {
    const user = userEvent.setup();
    renderApp("/settings/notifications");
    await user.click(await screen.findByRole("button", { name: "Send test to #deploys" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Test message sent to #deploys.");

    const dialog = await openChannelDialog(user);
    await user.type(within(dialog).getByLabelText(/^Name/), "broken hook");
    await user.type(within(dialog).getByLabelText(/Webhook URL/), SLACK_URL);
    await user.click(within(dialog).getByRole("button", { name: "Add channel" }));
    await user.click(await screen.findByRole("button", { name: "Send test to broken hook" }));
    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent(
        "The test to broken hook failed: the endpoint answered HTTP 404.",
      );
    });
    const history = await screen.findByRole("list", { name: "Delivery history" });
    expect(within(history).getAllByText("Test")).toHaveLength(2);
  });

  it("edits a channel without showing the stored secret and keeps it when left blank", async () => {
    const user = userEvent.setup();
    renderApp("/settings/notifications");
    await user.click(await screen.findByRole("button", { name: "Edit #deploys" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit #deploys" });
    const url = within(dialog).getByLabelText(/Webhook URL/);
    expect(url).toHaveValue("");
    expect(
      within(dialog).getByText(/Leave blank to keep the stored value \(ends in x7Qa\)/),
    ).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Type")).toBeDisabled();
    const name = within(dialog).getByLabelText(/^Name/);
    await user.clear(name);
    await user.type(name, "#releases");
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    const list = await screen.findByRole("list", { name: "Notification channels" });
    expect(within(list).getByText("#releases")).toBeInTheDocument();
    expect(within(list).getByText(/ends in x7Qa/)).toBeInTheDocument();
  });

  it("disables and enables a channel", async () => {
    const user = userEvent.setup();
    renderApp("/settings/notifications");
    await user.click(await screen.findByRole("button", { name: "Disable #deploys" }));
    expect(await screen.findByText("Disabled")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Enable #deploys" }));
    expect(await screen.findByText("Enabled")).toBeInTheDocument();
  });

  it("deletes a channel after confirmation", async () => {
    const user = userEvent.setup();
    renderApp("/settings/notifications");
    await user.click(await screen.findByRole("button", { name: "Delete #deploys" }));
    const confirm = await screen.findByRole("alertdialog", { name: "Delete #deploys?" });
    expect(confirm).toHaveTextContent("delivery history are removed too");
    await user.click(within(confirm).getByRole("button", { name: "Delete channel" }));
    expect(await screen.findByText("No channels yet")).toBeInTheDocument();
  });
});

describe("Settings, Notifications: default rules and history", () => {
  it("saves default rules, including the events that only exist globally", async () => {
    const user = userEvent.setup();
    renderApp("/settings/notifications");
    const group = await screen.findByRole("group", { name: "#deploys" });
    expect(within(group).getByLabelText("Disk usage above the threshold")).toBeInTheDocument();
    expect(within(group).getByLabelText("Webhook signature failures spike")).toBeInTheDocument();
    await user.click(within(group).getByLabelText("Run failed"));
    await user.click(within(group).getByLabelText("Deployment rolled back"));
    await user.click(within(group).getByLabelText("Disk usage above the threshold"));
    await user.click(screen.getByRole("button", { name: "Save rules" }));
    expect(await screen.findByText("Notification rules saved.")).toBeInTheDocument();
    expect(
      within(await screen.findByRole("group", { name: "#deploys" })).getByLabelText("Run failed"),
    ).toBeChecked();
  });

  it("states the quiet period and the retry policy", async () => {
    renderApp("/settings/notifications");
    expect(await screen.findByText(/within 30 minutes are collapsed into one/)).toBeInTheDocument();
    expect(screen.getByText(/retried up to 7 times/)).toBeInTheDocument();
  });

  it("shows the delivery history with status, attempts, errors and collapsed duplicates", async () => {
    renderApp("/settings/notifications");
    const history = await screen.findByRole("list", { name: "Delivery history" });
    const failed = within(history).getByText("Run #41 failed: shop-api/production").closest("li");
    expect(failed).not.toBeNull();
    expect(within(failed as HTMLElement).getByText("Failed")).toBeInTheDocument();
    expect(failed).toHaveTextContent("7 attempts");
    expect(failed).toHaveTextContent("Last error: the endpoint answered HTTP 404");
    const sent = within(history)
      .getByText("Deployment succeeded: shop-api/development")
      .closest("li");
    expect(within(sent as HTMLElement).getByText("Sent")).toBeInTheDocument();
    expect(sent).toHaveTextContent("2 duplicates collapsed");
  });
});

describe("project Settings, Notifications", () => {
  it("is listed in the project settings navigation", async () => {
    renderApp("/projects/shop-api/settings");
    expect(await screen.findByRole("link", { name: "Notifications" })).toHaveAttribute(
      "href",
      "/projects/shop-api/settings/notifications",
    );
  });

  it("offers only project events, lets a project override the default and an environment override the project", async () => {
    const user = userEvent.setup();
    renderApp("/projects/shop-api/settings/notifications");
    expect(await screen.findByText("Use the default rules (none are set)")).toBeInTheDocument();
    expect(screen.queryByLabelText("Disk usage above the threshold")).not.toBeInTheDocument();

    await user.click(screen.getByLabelText("Choose channels and events"));
    const group = await screen.findByRole("group", { name: "#deploys" });
    expect(within(group).queryByLabelText("Disk usage above the threshold")).toBeNull();
    expect(within(group).getByLabelText("Certificate renewal failed")).toBeInTheDocument();
    await user.click(within(group).getByLabelText("Run failed"));
    await user.click(screen.getByRole("button", { name: "Save rules" }));
    expect(await screen.findByText("Notification rules saved.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Production" }));
    expect(await screen.findByText("Use the project's rules")).toBeInTheDocument();
    await user.click(screen.getByLabelText("Choose channels and events"));
    const production = await screen.findByRole("group", { name: "#deploys" });
    expect(within(production).getByLabelText("Run failed")).not.toBeChecked();
    await user.click(within(production).getByLabelText("Run interrupted"));
    await user.click(screen.getByRole("button", { name: "Save rules" }));
    expect(await screen.findByText("Notification rules saved.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Development" }));
    expect(await screen.findByText("Use the project's rules")).toBeInTheDocument();
  });

  it("muting a scope sends an empty customized rule and going back to inherit clears it", async () => {
    const user = userEvent.setup();
    renderApp("/projects/shop-api/settings/notifications");
    await user.click(await screen.findByLabelText("Choose channels and events"));
    await user.click(screen.getByRole("button", { name: "Save rules" }));
    expect(await screen.findByText("Notification rules saved.")).toBeInTheDocument();
    // a customized project with nothing ticked stays customized (it mutes the project)
    expect(screen.getByLabelText("Choose channels and events")).toBeChecked();
    await user.click(screen.getByLabelText(/^Use the default rules/));
    await user.click(screen.getByRole("button", { name: "Save rules" }));
    await waitFor(() => {
      expect(screen.getByLabelText(/^Use the default rules/)).toBeChecked();
    });
  });

  it("shows the project's own delivery history", async () => {
    renderApp("/projects/shop-api/settings/notifications");
    const history = await screen.findByRole("list", { name: "Delivery history" });
    expect(within(history).getAllByRole("listitem")).toHaveLength(2);
  });
});

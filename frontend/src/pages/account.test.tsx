import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { initialsOf } from "../components/Initials";
import { MOCK_RECOVERY_CODES, MOCK_TOTP_CODE, MOCK_TOTP_SECRET } from "../mocks/account";
import { MOCK_MFA_USERNAME, MOCK_PASSWORD, MOCK_USERNAME } from "../mocks/data";
import { setMockAuthenticated } from "../mocks/handlers";
import { renderApp } from "../test/render";

let consoleError: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
});
afterEach(() => {
  expect(consoleError).not.toHaveBeenCalled();
});

describe("initials", () => {
  it("uses two words of a display name, else the first letters of the username", () => {
    expect(initialsOf("Ada Lovelace", "admin")).toBe("AL");
    expect(initialsOf("Ada Augusta King", "admin")).toBe("AK");
    expect(initialsOf("Ada", "admin")).toBe("AD");
    expect(initialsOf(null, "admin")).toBe("AD");
    expect(initialsOf("  ", "j.doe")).toBe("JD");
  });
});

describe("avatar menu", () => {
  it("shows initials and links to the profile and settings", async () => {
    const user = userEvent.setup();
    renderApp("/projects");
    await screen.findByRole("heading", { name: "Projects" });
    expect(screen.getAllByTestId("initials")[0]).toHaveTextContent("AD");
    await user.click(screen.getByRole("button", { name: "User menu" }));
    expect(await screen.findByRole("menuitem", { name: "Your profile" })).toHaveAttribute(
      "href",
      "/settings/profile",
    );
    expect(screen.getByRole("menuitem", { name: "Change password" })).toHaveAttribute(
      "href",
      "/settings/account",
    );
    expect(screen.getByRole("menuitem", { name: "Settings" })).toHaveAttribute("href", "/settings");
    expect(screen.getByRole("menuitem", { name: "Members" })).toHaveAttribute(
      "href",
      "/settings/members",
    );
    expect(screen.getByText("(owner)")).toBeInTheDocument();
  });

  it("lists the settings pages in the left navigation", async () => {
    renderApp("/settings/profile");
    const nav = await screen.findByRole("navigation", { name: "Settings" });
    for (const name of ["Public profile", "Account", "Sessions", "Security log", "API tokens"]) {
      expect(within(nav).getByRole("link", { name })).toBeInTheDocument();
    }
  });
});

describe("public profile", () => {
  it("saves the display name and email", async () => {
    const user = userEvent.setup();
    renderApp("/settings/profile");
    const name = await screen.findByLabelText("Display name");
    const save = screen.getByRole("button", { name: "Update profile" });
    expect(save).toBeDisabled();
    await user.type(name, "Ada Lovelace");
    await user.type(screen.getByLabelText("Email"), "ada@example.com");
    await user.click(save);
    expect(await screen.findByText("Profile updated.")).toBeInTheDocument();
  });

  it("reports an invalid email on the field", async () => {
    const user = userEvent.setup();
    renderApp("/settings/profile");
    await user.type(await screen.findByLabelText("Email"), "not-an-email");
    await user.click(screen.getByRole("button", { name: "Update profile" }));
    expect(await screen.findByText("Enter a valid email address.")).toBeInTheDocument();
  });
});

describe("profile page", () => {
  it("is the default settings page and holds only the public profile", async () => {
    renderApp("/settings");
    expect(await screen.findByRole("heading", { name: "Public profile" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Update profile" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Change password" })).not.toBeInTheDocument();
  });

  it("keeps the password and two-factor on the Account page", async () => {
    renderApp("/settings/account");
    for (const name of ["Account", "Two-factor authentication"]) {
      expect(await screen.findByRole("heading", { name })).toBeInTheDocument();
    }
    expect(screen.getByRole("button", { name: "Change password" })).toBeInTheDocument();
  });

  it("ticks the password hints as the user types", async () => {
    const user = userEvent.setup();
    renderApp("/settings/account");
    const hints = await screen.findByRole("list", { name: "Password hints" });
    expect(within(hints).getAllByText(/\(not yet\)/)).toHaveLength(3);
    await user.type(screen.getByLabelText("New password"), "a brand new passphrase");
    await user.type(screen.getByLabelText("Repeat the new password"), "a brand new passphrase");
    expect(within(hints).getAllByText(/\(done\)/)).toHaveLength(3);
  });

  it("checks the form, then changes the password and confirms it", async () => {
    const user = userEvent.setup();
    renderApp("/settings/account");
    await user.type(await screen.findByLabelText("Current password"), "current-secret-1");
    await user.type(screen.getByLabelText("New password"), "short");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("at least 12 characters");
    await user.clear(screen.getByLabelText("New password"));
    await user.type(screen.getByLabelText("New password"), "a brand new passphrase");
    await user.type(screen.getByLabelText("Repeat the new password"), "a brand new passphrase");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Your password was changed.");
  });
});

describe("two-factor authentication", () => {
  it("sets up with a QR code and a key, then shows the recovery codes once", async () => {
    const user = userEvent.setup();
    renderApp("/settings/account");
    await user.click(await screen.findByRole("button", { name: "Set up" }));
    const dialog = await screen.findByRole("dialog", { name: "Set up two-factor authentication" });
    expect(await within(dialog).findByRole("img", { name: /QR code/ })).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Setup key")).toHaveTextContent(MOCK_TOTP_SECRET);

    await user.type(within(dialog).getByLabelText(/Authentication code/), "000000");
    await user.click(within(dialog).getByRole("button", { name: "Verify and turn on" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("The code is incorrect.");

    await user.clear(within(dialog).getByLabelText(/Authentication code/));
    await user.type(within(dialog).getByLabelText(/Authentication code/), MOCK_TOTP_CODE);
    await user.click(within(dialog).getByRole("button", { name: "Verify and turn on" }));

    const codes = await screen.findByRole("dialog", { name: "Save your recovery codes" });
    expect(codes).toHaveTextContent("You will not see these codes again");
    expect(within(codes).getByLabelText("Recovery codes")).toHaveTextContent(
      MOCK_RECOVERY_CODES[0] ?? "",
    );
    await user.click(within(codes).getByRole("button", { name: "I have saved them" }));
    expect(screen.queryByText(MOCK_RECOVERY_CODES[0] ?? "")).not.toBeInTheDocument();

    const list = await screen.findByRole("list", { name: "Two-factor authentication" });
    expect(within(list).getByText("On")).toBeInTheDocument();
    expect(within(list).getByText("10 left")).toBeInTheDocument();
  });

  async function enable(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole("button", { name: "Set up" }));
    const dialog = await screen.findByRole("dialog", { name: "Set up two-factor authentication" });
    await user.type(await within(dialog).findByLabelText(/Authentication code/), MOCK_TOTP_CODE);
    await user.click(within(dialog).getByRole("button", { name: "Verify and turn on" }));
    const codes = await screen.findByRole("dialog", { name: "Save your recovery codes" });
    await user.click(within(codes).getByRole("button", { name: "I have saved them" }));
    await screen.findByRole("list", { name: "Two-factor authentication" });
  }

  it("generates new recovery codes after a valid code", async () => {
    const user = userEvent.setup();
    renderApp("/settings/account");
    await enable(user);
    await user.click(screen.getByRole("button", { name: "New codes" }));
    const dialog = await screen.findByRole("dialog", { name: "Generate new recovery codes" });
    await user.type(within(dialog).getByLabelText(/Authentication code/), "999999");
    await user.click(within(dialog).getByRole("button", { name: "Generate codes" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("The code is incorrect.");
    await user.clear(within(dialog).getByLabelText(/Authentication code/));
    await user.type(within(dialog).getByLabelText(/Authentication code/), MOCK_TOTP_CODE);
    await user.click(within(dialog).getByRole("button", { name: "Generate codes" }));
    expect(await screen.findByRole("dialog", { name: "Save your recovery codes" })).toBeVisible();
  });

  it("turns off only with the password and a code", async () => {
    const user = userEvent.setup();
    renderApp("/settings/account");
    await enable(user);
    await user.click(screen.getByRole("button", { name: "Turn off" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Turn off two-factor authentication?",
    });
    await user.type(within(dialog).getByLabelText(/^Password/), "wrong password");
    await user.type(within(dialog).getByLabelText(/Authentication code/), MOCK_TOTP_CODE);
    await user.click(within(dialog).getByRole("button", { name: "Turn off" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "The password is incorrect.",
    );
    await user.clear(within(dialog).getByLabelText(/^Password/));
    await user.type(within(dialog).getByLabelText(/^Password/), MOCK_PASSWORD);
    await user.click(within(dialog).getByRole("button", { name: "Turn off" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(await screen.findByRole("button", { name: "Set up" })).toBeInTheDocument();
  });
});

describe("sessions", () => {
  it("marks this device and revokes another one after confirmation", async () => {
    const user = userEvent.setup();
    renderApp("/settings/sessions");
    const list = await screen.findByRole("list", { name: "Sessions" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(within(rows[0] as HTMLElement).getByText("This device")).toBeInTheDocument();
    expect(within(rows[0] as HTMLElement).queryByRole("button")).not.toBeInTheDocument();
    expect(within(rows[1] as HTMLElement).getByText("Safari 17 on iOS")).toBeInTheDocument();
    expect(within(rows[1] as HTMLElement).getByText("198.51.100.23")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Revoke the session of Safari 17 on iOS" }),
    );
    const confirm = await screen.findByRole("alertdialog");
    await user.click(within(confirm).getByRole("button", { name: "Revoke session" }));
    await waitFor(() => {
      expect(
        within(screen.getByRole("list", { name: "Sessions" })).getAllByRole("listitem"),
      ).toHaveLength(1);
    });
    expect(
      screen.queryByRole("button", { name: "Revoke all other sessions" }),
    ).not.toBeInTheDocument();
  });

  it("revokes all other sessions at once", async () => {
    const user = userEvent.setup();
    renderApp("/settings/sessions");
    await user.click(await screen.findByRole("button", { name: "Revoke all other sessions" }));
    const confirm = await screen.findByRole("alertdialog", { name: "Revoke all other sessions?" });
    expect(confirm).toHaveTextContent("1 device is signed out at once");
    await user.click(within(confirm).getByRole("button", { name: "Revoke all" }));
    await waitFor(() => {
      expect(
        within(screen.getByRole("list", { name: "Sessions" })).getAllByRole("listitem"),
      ).toHaveLength(1);
    });
  });
});

describe("security log", () => {
  it("lists the account's own activity and loads older events", async () => {
    const user = userEvent.setup();
    renderApp("/settings/security-log");
    const list = await screen.findByRole("list", { name: "Security log" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(25);
    expect(within(list).getAllByText("Signed in").length).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: "Load older events" }));
    await waitFor(() => {
      expect(
        within(screen.getByRole("list", { name: "Security log" })).getAllByRole("listitem").length,
      ).toBeGreaterThan(25);
    });
  });
});

describe("personal API tokens", () => {
  it("lists the user's own tokens under settings", async () => {
    renderApp("/settings/tokens");
    const list = await screen.findByRole("list", { name: "API tokens" });
    expect(within(list).getByText("CI deploy")).toBeInTheDocument();
  });
});

describe("two-step sign in", () => {
  async function passwordStep(user: ReturnType<typeof userEvent.setup>) {
    setMockAuthenticated(false);
    renderApp("/login");
    await user.type(screen.getByLabelText("Username"), MOCK_MFA_USERNAME);
    await user.type(screen.getByLabelText("Password"), MOCK_PASSWORD);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    return screen.findByRole("heading", { name: "Two-factor authentication" });
  }

  it("asks for the code after the password and signs in with it", async () => {
    const user = userEvent.setup();
    await passwordStep(user);
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("Authentication code"), MOCK_TOTP_CODE);
    await user.click(screen.getByRole("button", { name: "Verify" }));
    expect(await screen.findByRole("heading", { name: "Home" })).toBeInTheDocument();
  });

  it("explains a wrong code and lets the user go back", async () => {
    const user = userEvent.setup();
    await passwordStep(user);
    await user.type(screen.getByLabelText("Authentication code"), "000000");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That code did not work");
    await user.click(screen.getByRole("button", { name: "Back to sign in" }));
    expect(await screen.findByRole("heading", { name: "Sign in to Jendock" })).toBeInTheDocument();
  });

  it("accepts a recovery code", async () => {
    const user = userEvent.setup();
    await passwordStep(user);
    await user.click(screen.getByRole("button", { name: "Use a recovery code" }));
    await user.type(screen.getByLabelText("Recovery code"), MOCK_RECOVERY_CODES[0] ?? "");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    expect(await screen.findByRole("heading", { name: "Home" })).toBeInTheDocument();
  });

  it("asks for a code before sending an empty one", async () => {
    const user = userEvent.setup();
    await passwordStep(user);
    await user.click(screen.getByRole("button", { name: "Verify" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Enter the six-digit code.");
  });

  it("a plain user still signs in with the password alone", async () => {
    const user = userEvent.setup();
    setMockAuthenticated(false);
    renderApp("/login");
    await user.type(screen.getByLabelText("Username"), MOCK_USERNAME);
    await user.type(screen.getByLabelText("Password"), MOCK_PASSWORD);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("heading", { name: "Home" })).toBeInTheDocument();
  });
});

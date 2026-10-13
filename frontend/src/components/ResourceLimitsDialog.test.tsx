import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import type { ResourceLimits } from "../api/resourceTypes";
import { formFromLimits, parseForm, splitBytes } from "../lib/resources";
import { server } from "../mocks/server";
import { ResourceLimitsDialog } from "./ResourceLimitsDialog";

const HOST = { cpus: 4, memory_bytes: 8 * 1024 ** 3 };
const GIB = 1024 ** 3;
const MIB = 1024 ** 2;
const URL = "/api/projects/shop-api/environments/development/resources/web";

function open(current: ResourceLimits | null = null) {
  const onClose = vi.fn();
  const onDone = vi.fn();
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ResourceLimitsDialog
        slug="shop-api"
        env="development"
        service="web"
        host={HOST}
        current={current}
        hasRunning
        onClose={onClose}
        onDone={onDone}
      />
    </QueryClientProvider>,
  );
  return { onClose, onDone, user: userEvent.setup() };
}

describe("ResourceLimitsDialog", () => {
  it("explains what saving and applying do and offers the three presets", async () => {
    open();
    const dialog = await screen.findByRole("dialog", { name: "Edit limits for web" });
    expect(within(dialog).getByText(/4 CPUs and 8.0 GiB of memory/)).toBeInTheDocument();
    expect(within(dialog).getByText(/kept for every future deploy/)).toBeInTheDocument();
    expect(within(dialog).getByText(/updated immediately, without a restart/)).toBeInTheDocument();
    for (const name of [/Small: 0.5 CPU, 512 MiB/, /Medium: 1 CPU, 1 GiB/, /Large: 2 CPU, 4 GiB/]) {
      expect(within(dialog).getByRole("button", { name })).toBeInTheDocument();
    }
    expect(within(dialog).queryByRole("button", { name: "Remove limit" })).not.toBeInTheDocument();
  });

  it("fills the fields from a preset and sends bytes with apply_now", async () => {
    let body: unknown = null;
    server.use(
      http.put(URL, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ service: "web", limits: null, applied: [], notes: [] });
      }),
    );
    const { user, onDone } = open();
    await user.click(await screen.findByRole("button", { name: /Medium: 1 CPU, 1 GiB/ }));
    expect(screen.getByLabelText("CPU cores")).toHaveValue(1);
    expect(screen.getByLabelText("Memory")).toHaveValue("1");
    expect(screen.getByRole("combobox", { name: "Memory unit" })).toHaveValue("GiB");
    await user.click(screen.getByRole("button", { name: "Save limits" }));
    await vi.waitFor(() => {
      expect(onDone).toHaveBeenCalled();
    });
    expect(body).toEqual({
      cpus: 1,
      memory_bytes: GIB,
      memory_reservation_bytes: null,
      cpu_shares: null,
      apply_now: true,
    });
  });

  it("converts the unit: 512 MiB then 2 GiB", async () => {
    let body: { memory_bytes: number } | null = null;
    server.use(
      http.put(URL, async ({ request }) => {
        body = (await request.json()) as { memory_bytes: number };
        return HttpResponse.json({ service: "web", limits: null, applied: [], notes: [] });
      }),
    );
    const { user, onDone } = open();
    await user.type(await screen.findByLabelText("Memory"), "2");
    await user.selectOptions(screen.getByRole("combobox", { name: "Memory unit" }), "GiB");
    await user.click(screen.getByRole("button", { name: "Save limits" }));
    await vi.waitFor(() => {
      expect(onDone).toHaveBeenCalled();
    });
    expect(body).toEqual(expect.objectContaining({ memory_bytes: 2 * GIB }));
  });

  it("refuses a value the host cannot give without calling the server", async () => {
    const calls = vi.fn();
    server.use(
      http.put(URL, () => {
        calls();
        return HttpResponse.json({});
      }),
    );
    const { user } = open();
    await user.type(await screen.findByLabelText("CPU cores"), "9");
    await user.click(screen.getByRole("button", { name: "Save limits" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("cannot exceed the host's 4 CPUs");
    expect(calls).not.toHaveBeenCalled();
  });

  it("shows the server's message when it refuses", async () => {
    server.use(
      http.put(URL, () =>
        HttpResponse.json(
          { error: { code: "validation_error", message: "memory must be at least 6 MiB" } },
          { status: 422 },
        ),
      ),
    );
    const { user } = open();
    await user.type(await screen.findByLabelText("CPU cores"), "1");
    await user.click(screen.getByRole("button", { name: "Save limits" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("at least 6 MiB");
  });

  it("removes the limit when one is saved", async () => {
    let query = "";
    server.use(
      http.delete(URL, ({ request }) => {
        query = new globalThis.URL(request.url).search;
        return HttpResponse.json({ service: "web", limits: null, applied: [], notes: [] });
      }),
    );
    const { user, onDone } = open({
      cpus: 2,
      memory_bytes: GIB,
      memory_reservation_bytes: null,
      cpu_shares: null,
    });
    expect(await screen.findByLabelText("CPU cores")).toHaveValue(2);
    await user.click(screen.getByRole("button", { name: "Remove limit" }));
    await vi.waitFor(() => {
      expect(onDone).toHaveBeenCalledWith(expect.anything(), true);
    });
    expect(query).toBe("?apply_now=true");
  });
});

describe("limit form rules", () => {
  it("writes sizes the way a person would", () => {
    expect(splitBytes(512 * MIB)).toEqual({ value: "512", unit: "MiB" });
    expect(splitBytes(2 * GIB)).toEqual({ value: "2", unit: "GiB" });
    expect(splitBytes(1.5 * GIB)).toEqual({ value: "1.5", unit: "GiB" });
    expect(splitBytes(null)).toEqual({ value: "", unit: "MiB" });
  });

  it("checks CPU, memory, reservation and shares like the server", () => {
    const base = formFromLimits(null);
    const problem = (patch: Partial<typeof base>) => {
      const parsed = parseForm({ ...base, ...patch }, HOST);
      return parsed.ok ? null : parsed.message;
    };
    expect(problem({})).toMatch(/at least one limit/);
    expect(problem({ cpus: "0" })).toMatch(/greater than 0/);
    expect(problem({ cpus: "abc" })).toMatch(/must be a number/);
    expect(problem({ cpus: "5" })).toMatch(/host's 4 CPUs/);
    expect(problem({ memory: "5" })).toMatch(/at least 6.0 MiB/);
    expect(problem({ memory: "9", memoryUnit: "GiB" })).toMatch(/host's 8.0 GiB/);
    expect(problem({ memory: "256", reservation: "512" })).toMatch(/reservation cannot be larger/);
    expect(problem({ cpus: "1", shares: "1" })).toMatch(/CPU shares must be/);
    expect(problem({ cpus: "0.5", memory: "512", shares: "512" })).toBeNull();
  });
});

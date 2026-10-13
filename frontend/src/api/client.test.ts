import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { server } from "../mocks/server";
import { ApiError, onUnauthenticated, request, setCsrfToken } from "./client";

describe("api client", () => {
  it("sends credentials and the CSRF header only on unsafe methods", async () => {
    const seen: Record<string, string | null> = {};
    server.use(
      http.get("/api/ping", ({ request: req }) => {
        seen.get = req.headers.get("X-CSRF-Token");
        return HttpResponse.json({ ok: true });
      }),
      http.post("/api/ping", ({ request: req }) => {
        seen.post = req.headers.get("X-CSRF-Token");
        return HttpResponse.json({ ok: true });
      }),
    );
    setCsrfToken("tok");
    await request("/api/ping");
    await request("/api/ping", { method: "POST", body: {} });
    expect(seen).toEqual({ get: null, post: "tok" });
  });

  it("parses the error envelope with diagnostics and retry-after", async () => {
    server.use(
      http.post("/api/pipeline", () =>
        HttpResponse.json(
          {
            error: {
              code: "validation_error",
              message: "invalid pipeline",
              diagnostics: [{ severity: "error", message: "bad", line: 3, column: 9 }],
            },
          },
          { status: 422, headers: { "Retry-After": "30" } },
        ),
      ),
    );
    const error = await request("/api/pipeline", { method: "POST" }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    const api = error as ApiError;
    expect(api.status).toBe(422);
    expect(api.code).toBe("validation_error");
    expect(api.diagnostics).toEqual([{ severity: "error", message: "bad", line: 3, column: 9 }]);
    expect(api.retryAfter).toBe(30);
  });

  it("calls the unauthenticated handler on 401 unless suppressed", async () => {
    server.use(
      http.get("/api/secret", () =>
        HttpResponse.json({ error: { code: "unauthenticated", message: "no" } }, { status: 401 }),
      ),
    );
    const handler = vi.fn();
    onUnauthenticated(handler);
    await request("/api/secret").catch(() => undefined);
    await request("/api/secret", { suppressUnauthenticated: true }).catch(() => undefined);
    expect(handler).toHaveBeenCalledTimes(1);
    onUnauthenticated(null);
  });

  it("turns network failures and non-JSON errors into ApiError", async () => {
    server.use(
      http.get("/api/down", () => HttpResponse.error()),
      http.get("/api/html", () => new HttpResponse("<h1>bad gateway</h1>", { status: 502 })),
    );
    await expect(request("/api/down")).rejects.toMatchObject({ code: "network_error", status: 0 });
    await expect(request("/api/html")).rejects.toMatchObject({ code: "http_error", status: 502 });
  });
});

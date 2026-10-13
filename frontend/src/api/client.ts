import type { Diagnostic, FieldIssue, Session } from "./types";

/** Parsed error envelope: `{"error": {"code", "message", "diagnostics"?, "fields"?}}`. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly diagnostics: Diagnostic[];
  readonly fields: FieldIssue[];
  /** Seconds from the `Retry-After` header (429 responses). */
  readonly retryAfter: number | null;

  constructor(init: {
    status: number;
    code: string;
    message: string;
    diagnostics?: Diagnostic[];
    fields?: FieldIssue[];
    retryAfter?: number | null;
  }) {
    super(init.message);
    this.name = "ApiError";
    this.status = init.status;
    this.code = init.code;
    this.diagnostics = init.diagnostics ?? [];
    this.fields = init.fields ?? [];
    this.retryAfter = init.retryAfter ?? null;
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

let csrfToken: string | null = null;
let unauthenticatedHandler: (() => void) | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

/** Called once when any request other than the login attempt returns 401. */
export function onUnauthenticated(handler: (() => void) | null): void {
  unauthenticatedHandler = handler;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

async function parseError(response: Response): Promise<ApiError> {
  let code = "http_error";
  let message = `Request failed (${response.status})`;
  let diagnostics: Diagnostic[] = [];
  let fields: FieldIssue[] = [];
  try {
    const body: unknown = await response.json();
    if (isRecord(body) && isRecord(body.error)) {
      const error = body.error;
      if (typeof error.code === "string") code = error.code;
      if (typeof error.message === "string") message = error.message;
      if (Array.isArray(error.diagnostics)) diagnostics = error.diagnostics as Diagnostic[];
      if (Array.isArray(error.fields)) fields = error.fields as FieldIssue[];
    }
  } catch {
    /* non-JSON body: keep the generic message */
  }
  const retry = Number(response.headers.get("Retry-After"));
  return new ApiError({
    status: response.status,
    code,
    message,
    diagnostics,
    fields,
    retryAfter: Number.isFinite(retry) && retry > 0 ? retry : null,
  });
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
  /** Login must show its own 401 instead of redirecting. */
  suppressUnauthenticated?: boolean;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = (options.method ?? "GET").toUpperCase();
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (!SAFE_METHODS.has(method) && csrfToken !== null) headers["X-CSRF-Token"] = csrfToken;

  let response: Response;
  try {
    response = await fetch(new URL(path, window.location.href), {
      method,
      headers,
      credentials: "include",
      signal: options.signal ?? null,
      body: options.body === undefined ? null : JSON.stringify(options.body),
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new ApiError({
      status: 0,
      code: "network_error",
      message: "Could not reach the server. Check your connection and try again.",
    });
  }

  if (!response.ok) {
    const error = await parseError(response);
    if (response.status === 401 && options.suppressUnauthenticated !== true) {
      setCsrfToken(null);
      unauthenticatedHandler?.();
    }
    throw error;
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** Plain GET of text-like content is not needed; everything is JSON. */
export const api = {
  get: <T>(path: string, signal?: AbortSignal) => request<T>(path, signal ? { signal } : {}),
  post: <T>(path: string, body?: unknown) => request<T>(path, { method: "POST", body }),
  put: <T>(path: string, body?: unknown) => request<T>(path, { method: "PUT", body }),
  patch: <T>(path: string, body?: unknown) => request<T>(path, { method: "PATCH", body }),
  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};

export async function fetchSession(): Promise<Session> {
  const session = await api.get<Session>("/api/auth/session");
  setCsrfToken(session.csrf_token);
  return session;
}

export async function login(username: string, password: string): Promise<Session> {
  const session = await request<Session>("/api/auth/login", {
    method: "POST",
    body: { username, password },
    suppressUnauthenticated: true,
  });
  setCsrfToken(session.csrf_token);
  return session;
}

export async function logout(): Promise<void> {
  try {
    await api.post<undefined>("/api/auth/logout");
  } finally {
    setCsrfToken(null);
  }
}

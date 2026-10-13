import { HttpResponse } from "msw";
import type { Page } from "../api/types";

/** How a mock list searches, filters and sorts; mirrors the server's list rules. */
export interface MockList<T> {
  /** The strings `q` is matched against (case-insensitive "contains"). */
  text: (item: T) => (string | null | undefined)[];
  /** Filter parameters by name; a filter only applies when the parameter is present. */
  filters?: Record<string, (item: T, value: string) => boolean>;
  /** Ascending comparators by sort key. */
  sorts: Record<string, (a: T, b: T) => number>;
  defaultSort: string;
  /** Keys that read newest/largest first unless `order` says otherwise. */
  descending?: readonly string[];
}

const MAX_PER_PAGE = 100;

function unprocessable(message: string): Response {
  return HttpResponse.json({ error: { code: "validation_error", message } }, { status: 422 });
}

/** Answers a list request the way the API does: search, filters, sort, then one page. */
export function listResponse<T>(items: readonly T[], url: string, config: MockList<T>): Response {
  const params = new URL(url).searchParams;
  const page = Number(params.get("page") ?? 1);
  const perPage = Number(params.get("per_page") ?? 25);
  if (!Number.isInteger(page) || page < 1) return unprocessable("page must be at least 1");
  if (!Number.isInteger(perPage) || perPage < 1 || perPage > MAX_PER_PAGE) {
    return unprocessable(`per_page must be 1-${String(MAX_PER_PAGE)}`);
  }
  const sort = params.get("sort") ?? config.defaultSort;
  const compare = config.sorts[sort];
  if (!compare) return unprocessable(`unknown sort: ${sort}`);

  let found = [...items];
  for (const [name, matches] of Object.entries(config.filters ?? {})) {
    const value = params.get(name);
    if (value !== null && value !== "") found = found.filter((item) => matches(item, value));
  }
  const needle = (params.get("q") ?? "").trim().toLowerCase();
  if (needle !== "") {
    found = found.filter((item) =>
      config.text(item).some((part) => (part ?? "").toLowerCase().includes(needle)),
    );
  }
  const order = params.get("order");
  const descending = order === null ? (config.descending ?? []).includes(sort) : order === "desc";
  found.sort((a, b) => (descending ? compare(b, a) : compare(a, b)));

  const body: Page<T> = {
    items: found.slice((page - 1) * perPage, page * perPage),
    total: found.length,
    page,
    per_page: perPage,
    pages: Math.max(1, Math.ceil(found.length / perPage)),
  };
  return HttpResponse.json(body);
}

export const byText = (a: string, b: string): number => a.localeCompare(b);
export const byTime = (a: string | null, b: string | null): number =>
  (a === null ? 0 : Date.parse(a)) - (b === null ? 0 : Date.parse(b));

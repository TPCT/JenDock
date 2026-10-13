import { useCallback, useMemo } from "react";
import { useLocation, useSearchParams } from "react-router-dom";

/** One entry of a sort menu. `value` is what the URL carries; `sort`/`order` go to the API. */
export interface SortOption {
  value: string;
  label: string;
  sort: string;
  order?: "asc" | "desc";
}

export interface FilterOption {
  value: string;
  label: string;
}

/** A filter menu: one query parameter with a fixed set of values (the empty value is "all"). */
export interface FilterDef {
  param: string;
  label: string;
  /** What the empty value reads in the menu, such as "All statuses". */
  allLabel: string;
  options: readonly FilterOption[];
}

export interface ListSpec {
  filters?: readonly FilterDef[];
  /** The first option is the default order. */
  sorts?: readonly SortOption[];
}

export interface ListState {
  page: number;
  q: string;
  filters: Readonly<Record<string, string>>;
  sort: SortOption | null;
  /** A search text or a filter is set (sorting alone does not count). */
  active: boolean;
  /** The query string for the API: `page`, `q`, `sort`, `order` and the filters. */
  apiQuery: string;
}

export interface ListController extends ListState {
  setQ: (q: string) => void;
  setPage: (page: number) => void;
  setFilter: (param: string, value: string) => void;
  setSort: (value: string) => void;
  clear: () => void;
  /** The address of page `page` of this list, for pagination links. */
  hrefFor: (page: number) => string;
}

/** Reads the page, search, filters and sort of a list from URL parameters. */
export function readListParams(params: URLSearchParams, spec: ListSpec): ListState {
  const rawPage = Number(params.get("page") ?? 1);
  const page = Number.isInteger(rawPage) && rawPage >= 1 ? rawPage : 1;
  const q = (params.get("q") ?? "").trim();
  const filters: Record<string, string> = {};
  for (const def of spec.filters ?? []) {
    const value = params.get(def.param) ?? "";
    filters[def.param] = def.options.some((option) => option.value === value) ? value : "";
  }
  const sorts = spec.sorts ?? [];
  const chosen = sorts.find((option) => option.value === params.get("sort"));
  const sort = chosen ?? sorts[0] ?? null;
  const api = new URLSearchParams();
  if (page > 1) api.set("page", String(page));
  if (q !== "") api.set("q", q);
  for (const [name, value] of Object.entries(filters)) if (value !== "") api.set(name, value);
  if (chosen !== undefined && chosen !== sorts[0]) {
    api.set("sort", chosen.sort);
    if (chosen.order) api.set("order", chosen.order);
  }
  const active = q !== "" || Object.values(filters).some((value) => value !== "");
  return { page, q, filters, sort, active, apiQuery: api.toString() };
}

/**
 * List state that lives in the address bar (`?q=&status=&sort=&page=`), so a filtered page can be
 * shared, bookmarked and reached with the back button. Other parameters of the URL are kept.
 * Pass a constant `spec` (declared outside the component).
 */
export function useListParams(spec: ListSpec): ListController {
  const [params, setParams] = useSearchParams();
  const { pathname } = useLocation();
  const state = useMemo(() => readListParams(params, spec), [params, spec]);

  const update = useCallback(
    (change: (next: URLSearchParams) => void, replace: boolean) => {
      setParams(
        (previous) => {
          const next = new URLSearchParams(previous);
          change(next);
          return next;
        },
        { replace },
      );
    },
    [setParams],
  );

  const setQ = useCallback(
    (q: string) => {
      update((next) => {
        if (q.trim() === "") next.delete("q");
        else next.set("q", q);
        next.delete("page");
      }, true);
    },
    [update],
  );
  const setPage = useCallback(
    (page: number) => {
      update((next) => {
        if (page <= 1) next.delete("page");
        else next.set("page", String(page));
      }, false);
    },
    [update],
  );
  const setFilter = useCallback(
    (param: string, value: string) => {
      update((next) => {
        if (value === "") next.delete(param);
        else next.set(param, value);
        next.delete("page");
      }, false);
    },
    [update],
  );
  const setSort = useCallback(
    (value: string) => {
      update((next) => {
        if (value === spec.sorts?.[0]?.value) next.delete("sort");
        else next.set("sort", value);
        next.delete("page");
      }, false);
    },
    [update, spec.sorts],
  );
  const clear = useCallback(() => {
    update((next) => {
      next.delete("q");
      next.delete("page");
      for (const def of spec.filters ?? []) next.delete(def.param);
    }, false);
  }, [update, spec.filters]);
  const hrefFor = useCallback(
    (page: number) => {
      const next = new URLSearchParams(params);
      if (page <= 1) next.delete("page");
      else next.set("page", String(page));
      const text = next.toString();
      return `${pathname}${text === "" ? "" : `?${text}`}`;
    },
    [params, pathname],
  );

  return { ...state, setQ, setPage, setFilter, setSort, clear, hrefFor };
}

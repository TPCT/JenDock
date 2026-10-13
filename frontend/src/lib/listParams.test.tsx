import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { readListParams, useListParams, type ListSpec } from "./listParams";

const SPEC: ListSpec = {
  filters: [
    {
      param: "status",
      label: "Status",
      allLabel: "All statuses",
      options: [
        { value: "failed", label: "Failed" },
        { value: "succeeded", label: "Succeeded" },
      ],
    },
  ],
  sorts: [
    { value: "newest", label: "Newest", sort: "created", order: "desc" },
    { value: "oldest", label: "Oldest", sort: "created", order: "asc" },
  ],
};

const params = (text: string) => new URLSearchParams(text);

describe("readListParams", () => {
  it("defaults to the first page, no search, no filter and the first sort", () => {
    const state = readListParams(params(""), SPEC);
    expect(state).toMatchObject({ page: 1, q: "", active: false, apiQuery: "" });
    expect(state.sort?.value).toBe("newest");
    expect(state.filters).toEqual({ status: "" });
  });

  it("builds the API query from page, search, filter and a non-default sort", () => {
    const state = readListParams(params("page=3&q=%20web%20&status=failed&sort=oldest"), SPEC);
    expect(state.page).toBe(3);
    expect(state.q).toBe("web");
    expect(state.active).toBe(true);
    const api = new URLSearchParams(state.apiQuery);
    expect(Object.fromEntries(api)).toEqual({
      page: "3",
      q: "web",
      status: "failed",
      sort: "created",
      order: "asc",
    });
  });

  it("ignores values it does not know instead of sending them to the server", () => {
    const state = readListParams(params("page=0&status=bogus&sort=bogus"), SPEC);
    expect(state.page).toBe(1);
    expect(state.filters.status).toBe("");
    expect(state.apiQuery).toBe("");
    expect(readListParams(params("page=abc"), SPEC).page).toBe(1);
  });

  it("does not count a sort change as an active filter", () => {
    expect(readListParams(params("sort=oldest"), SPEC).active).toBe(false);
  });
});

function wrapperFor(initial: string) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <MemoryRouter initialEntries={[initial]}>{children}</MemoryRouter>;
  };
}

function useListAndLocation() {
  return { list: useListParams(SPEC), location: useLocation() };
}

describe("useListParams", () => {
  it("writes the search to the URL, resets the page and keeps other parameters", () => {
    const { result } = renderHook(useListAndLocation, {
      wrapper: wrapperFor("/runs?page=4&run=new"),
    });
    act(() => {
      result.current.list.setQ("shop");
    });
    expect(new URLSearchParams(result.current.location.search).toString()).toBe("run=new&q=shop");
    expect(result.current.list.q).toBe("shop");
    expect(result.current.list.page).toBe(1);
  });

  it("sets and removes filters, sorts and pages", () => {
    const { result } = renderHook(useListAndLocation, { wrapper: wrapperFor("/runs") });
    act(() => {
      result.current.list.setFilter("status", "failed");
    });
    act(() => {
      result.current.list.setPage(2);
    });
    expect(result.current.location.search).toBe("?status=failed&page=2");
    act(() => {
      result.current.list.setSort("oldest");
    });
    expect(result.current.location.search).toBe("?status=failed&sort=oldest");
    act(() => {
      result.current.list.setSort("newest");
    });
    act(() => {
      result.current.list.setFilter("status", "");
    });
    expect(result.current.location.search).toBe("");
  });

  it("clears the search and the filters but keeps the sort and other parameters", () => {
    const { result } = renderHook(useListAndLocation, {
      wrapper: wrapperFor("/runs?q=a&status=failed&sort=oldest&page=2&run=new"),
    });
    expect(result.current.list.active).toBe(true);
    act(() => {
      result.current.list.clear();
    });
    expect(new URLSearchParams(result.current.location.search).toString()).toBe(
      "sort=oldest&run=new",
    );
    expect(result.current.list.active).toBe(false);
  });

  it("builds pagination links for the current path and filters", () => {
    const { result } = renderHook(useListAndLocation, {
      wrapper: wrapperFor("/runs?status=failed&page=2"),
    });
    expect(result.current.list.hrefFor(3)).toBe("/runs?status=failed&page=3");
    expect(result.current.list.hrefFor(1)).toBe("/runs?status=failed");
  });
});

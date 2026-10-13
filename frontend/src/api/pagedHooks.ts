import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "./client";
import type { Page } from "./types";

/**
 * A list endpoint: the path plus the list's query string (`ListController.apiQuery`), refetched
 * when the search, filter or page changes. The previous page stays on screen meanwhile, so
 * paging and typing do not flash a skeleton.
 */
export function usePagedQuery<T>(
  key: readonly unknown[],
  path: string,
  apiQuery: string,
  options: { refetchInterval?: number } = {},
) {
  return useQuery({
    queryKey: [...key, apiQuery] as const,
    queryFn: ({ signal }) =>
      api.get<Page<T>>(apiQuery === "" ? path : `${path}?${apiQuery}`, signal),
    placeholderData: keepPreviousData,
    ...(options.refetchInterval === undefined ? {} : { refetchInterval: options.refetchInterval }),
  });
}

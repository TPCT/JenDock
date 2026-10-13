import { SearchIcon } from "@primer/octicons-react";
import { Button, Pagination, SkeletonBox } from "@primer/react";
import { useEffect, useRef, type ReactNode } from "react";
import type { UseQueryResult } from "@tanstack/react-query";
import type { Page } from "../api/types";
import type { FilterDef, ListController, SortOption } from "../lib/listParams";
import box from "../styles/box.module.css";
import { EmptyState, ErrorCallout } from "./Feedback";
import { ListToolbar } from "./ListToolbar";
import styles from "./PagedList.module.css";

export interface PagedListProps<T> {
  /** What the list holds, plural and lower case ("runs"): used in the count and the messages. */
  noun: string;
  /** The singular of `noun` for a count of one; defaults to `noun` without its final "s". */
  singular?: string;
  query: UseQueryResult<Page<T>>;
  list: ListController;
  filters?: readonly FilterDef[];
  sorts?: readonly SortOption[];
  /** Placeholder of the search field, such as "Find a project…". */
  placeholder?: string;
  /** Shown instead of the list when there is nothing at all (no search or filter active). */
  empty: ReactNode;
  /** Extra buttons right of the filter menus. */
  actions?: ReactNode;
  /** Rendered inside the box, between the count header and the pagination. */
  children: (items: T[]) => ReactNode;
}

function Skeleton({ label, rows }: { label: string; rows: number }) {
  return (
    <div role="status" aria-live="polite" className={box.box}>
      <span className={styles.hidden}>{label}…</span>
      <div className={box.header} aria-hidden="true">
        <SkeletonBox height="1rem" width="6rem" />
      </div>
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className={`${box.row} ${styles.skeletonRow}`} aria-hidden="true">
          <SkeletonBox height="1.25rem" width="2.5rem" />
          <div className={styles.skeletonText}>
            <SkeletonBox height="1rem" width="45%" />
            <SkeletonBox height="0.75rem" width="70%" />
          </div>
        </div>
      ))}
    </div>
  );
}

/**
 * The shared list view: search and filter bar, loading skeleton, error banner, empty state,
 * "no match" state, the rows (`children`) and GitHub's pagination. State is in the URL through
 * `useListParams`; data comes from `usePagedQuery`.
 */
export function PagedList<T>({
  noun,
  singular = noun.replace(/s$/, ""),
  query,
  list,
  filters,
  sorts,
  placeholder,
  empty,
  actions,
  children,
}: PagedListProps<T>) {
  const { data } = query;
  const top = useRef<HTMLDivElement>(null);
  const { page, setPage } = list;

  // A shared link or a deleted row can leave the page number past the end: show the last page.
  useEffect(() => {
    if (data && !query.isPlaceholderData && page > data.pages) setPage(data.pages);
  }, [data, page, query.isPlaceholderData, setPage]);

  const nothing = data !== undefined && data.total === 0 && !list.active;
  const first = data === undefined ? 0 : (data.page - 1) * data.per_page + 1;
  const last = data === undefined ? 0 : first + data.items.length - 1;

  return (
    <div className={box.stack} ref={top}>
      {nothing ? null : (
        <ListToolbar
          list={list}
          filters={filters ?? []}
          sorts={sorts ?? []}
          searchLabel={`Search ${noun}`}
          {...(placeholder === undefined ? {} : { placeholder })}
          actions={actions}
        />
      )}
      {query.isPending ? <Skeleton label={`Loading ${noun}`} rows={4} /> : null}
      {query.isError ? (
        <ErrorCallout
          title={`Could not load ${noun}`}
          error={query.error}
          onRetry={() => void query.refetch()}
        />
      ) : null}
      {nothing ? empty : null}
      {data && data.total === 0 && list.active ? (
        <EmptyState
          icon={<SearchIcon size={32} aria-hidden="true" />}
          title={`No ${noun} match`}
          action={<Button onClick={list.clear}>Clear search and filters</Button>}
        >
          Nothing matches {list.q === "" ? "these filters" : `“${list.q}”`}. Check the spelling or
          clear the search to see everything.
        </EmptyState>
      ) : null}
      {data && data.total > 0 ? (
        <div className={box.box} aria-busy={query.isPlaceholderData}>
          <div className={box.header}>
            <span aria-live="polite">
              {data.total} {data.total === 1 ? singular : noun}
              {list.active ? " found" : ""}
            </span>
            {list.active ? (
              <Button variant="invisible" size="small" onClick={list.clear}>
                Clear search and filters
              </Button>
            ) : null}
          </div>
          <div className={query.isPlaceholderData ? styles.stale : undefined}>
            {children(data.items)}
          </div>
          <div className={box.footer}>
            <span className="muted">
              Showing {first}-{last} of {data.total}
            </span>
            {data.pages > 1 ? (
              <Pagination
                pageCount={data.pages}
                currentPage={Math.min(page, data.pages)}
                showPages={{ narrow: false }}
                hrefBuilder={list.hrefFor}
                onPageChange={(event, next) => {
                  event.preventDefault();
                  setPage(next);
                  top.current?.scrollIntoView({ block: "start" });
                }}
              />
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}

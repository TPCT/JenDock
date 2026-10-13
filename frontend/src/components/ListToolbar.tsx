import { SearchIcon, XIcon } from "@primer/octicons-react";
import { ActionList, ActionMenu, TextInput } from "@primer/react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import type { FilterDef, ListController, SortOption } from "../lib/listParams";
import { useDebouncedEffect } from "../lib/useDebouncedEffect";
import styles from "./ListToolbar.module.css";

const SEARCH_DELAY_MS = 300;

function FilterMenu({ def, list }: { def: FilterDef; list: ListController }) {
  const value = list.filters[def.param] ?? "";
  const current = def.options.find((option) => option.value === value);
  return (
    <ActionMenu>
      <ActionMenu.Button aria-label={`${def.label} filter`}>
        {def.label}
        {current ? `: ${current.label}` : ""}
      </ActionMenu.Button>
      <ActionMenu.Overlay width="small">
        <ActionList selectionVariant="single">
          <ActionList.Item
            selected={value === ""}
            onSelect={() => {
              list.setFilter(def.param, "");
            }}
          >
            {def.allLabel}
          </ActionList.Item>
          {def.options.map((option) => (
            <ActionList.Item
              key={option.value}
              selected={value === option.value}
              onSelect={() => {
                list.setFilter(def.param, option.value);
              }}
            >
              {option.label}
            </ActionList.Item>
          ))}
        </ActionList>
      </ActionMenu.Overlay>
    </ActionMenu>
  );
}

function SortMenu({ sorts, list }: { sorts: readonly SortOption[]; list: ListController }) {
  return (
    <ActionMenu>
      <ActionMenu.Button aria-label="Sort">
        Sort: {list.sort?.label ?? sorts[0]?.label}
      </ActionMenu.Button>
      <ActionMenu.Overlay width="small" align="end">
        <ActionList selectionVariant="single">
          <ActionList.Group>
            <ActionList.GroupHeading>Sort by</ActionList.GroupHeading>
            {sorts.map((option) => (
              <ActionList.Item
                key={option.value}
                selected={list.sort?.value === option.value}
                onSelect={() => {
                  list.setSort(option.value);
                }}
              >
                {option.label}
              </ActionList.Item>
            ))}
          </ActionList.Group>
        </ActionList>
      </ActionMenu.Overlay>
    </ActionMenu>
  );
}

/**
 * GitHub's list filter bar: a search field with a magnifier, filter menus and a sort menu. The
 * text is applied to the URL a moment after typing stops (Enter applies it at once).
 */
export function ListToolbar({
  list,
  filters = [],
  sorts = [],
  searchLabel,
  placeholder,
  actions,
}: {
  list: ListController;
  filters?: readonly FilterDef[];
  sorts?: readonly SortOption[];
  /** Accessible name of the search field, such as "Search projects". */
  searchLabel: string;
  placeholder?: string;
  actions?: ReactNode;
}) {
  const [text, setText] = useState(list.q);
  // The last value this toolbar wrote to the URL; anything else is an outside change (back
  // button, "Clear filters") that the field must follow.
  const written = useRef(list.q);
  useEffect(() => {
    if (list.q !== written.current) {
      written.current = list.q;
      setText(list.q);
    }
  }, [list.q]);
  useDebouncedEffect(text, SEARCH_DELAY_MS, (value) => {
    if (value.trim() !== written.current) {
      written.current = value.trim();
      list.setQ(value);
    }
  });

  const apply = (value: string) => {
    written.current = value.trim();
    list.setQ(value);
  };

  return (
    <form
      className={styles.bar}
      role="search"
      aria-label={searchLabel}
      onSubmit={(event) => {
        event.preventDefault();
        apply(text);
      }}
    >
      <div className={styles.search}>
        <TextInput
          block
          type="search"
          name="q"
          value={text}
          placeholder={placeholder ?? "Search"}
          aria-label={searchLabel}
          leadingVisual={SearchIcon}
          trailingAction={
            text === "" ? undefined : (
              <TextInput.Action
                icon={XIcon}
                aria-label="Clear search"
                onClick={() => {
                  setText("");
                  apply("");
                }}
              />
            )
          }
          onChange={(event) => {
            setText(event.target.value);
          }}
        />
      </div>
      <div className={styles.menus}>
        {filters.map((def) => (
          <FilterMenu key={def.param} def={def} list={list} />
        ))}
        {sorts.length > 1 ? <SortMenu sorts={sorts} list={list} /> : null}
        {actions}
      </div>
    </form>
  );
}

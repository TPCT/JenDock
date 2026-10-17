import { DatabaseIcon, EyeIcon, SearchIcon, TableIcon } from "@primer/octicons-react";
import {
  Button,
  ButtonGroup,
  Checkbox,
  FormControl,
  Heading,
  Label,
  LinkButton,
  NavList,
  Select,
  TextInput,
} from "@primer/react";
import { UnderlinePanels } from "@primer/react/experimental";
import { memo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useConnection, useRows, useSchemas, useTableDetail, useTables } from "../api/dbHooks";
import type { TableDetail, TableInfo } from "../api/dbTypes";
import { isApiError } from "../api/client";
import { ENVIRONMENTS, type EnvironmentName } from "../api/types";
import { Callout, EmptyState, ErrorCallout, Loading, errorMessage } from "../components/Feedback";
import { ResultTable } from "../components/ResultTable";
import { useDocumentTitle } from "../components/PageHead";
import { useFillViewport } from "../lib/useFillViewport";
import box from "../styles/box.module.css";
import styles from "./DatabaseBrowser.module.css";
import { databasesPath } from "./Databases";

const LIMITS = [50, 100, 500] as const;

function kindLabel(kind: TableInfo["kind"]): string {
  return kind.replaceAll("_", " ");
}

function Structure({ detail }: { detail: TableDetail }) {
  return (
    <div className={styles.structure}>
      <section className={`${styles.structureGrid} ${box.box}`} aria-label="Columns">
        <div className={box.header}>Columns</div>
        <ResultTable
          label="Columns of the table"
          bare
          columns={[
            { name: "Name", type: "" },
            { name: "#", type: "" },
            { name: "Type", type: "" },
            { name: "Null?", type: "" },
            { name: "Default", type: "" },
            { name: "Key", type: "" },
          ]}
          rows={detail.columns.map((column) => [
            column.name,
            column.position,
            `${column.data_type}${column.extra ? ` ${column.extra}` : ""}`,
            column.nullable ? "yes" : "no",
            column.default ?? "none",
            column.primary_key_position === null
              ? ""
              : `primary key ${String(column.primary_key_position)}`,
          ])}
          primaryKey={detail.columns
            .filter((column) => column.primary_key_position !== null)
            .map((column) => column.name)}
        />
      </section>
      <div className={styles.metadata}>
        <section className={`${box.box} ${styles.metadataBox}`} aria-label="Indexes">
          <div className={box.header}>Indexes</div>
          {detail.indexes.length === 0 ? (
            <p className={box.row}>No indexes.</p>
          ) : (
            <ul className={box.list}>
              {detail.indexes.map((index) => (
                <li key={index.name} className={box.row}>
                  <code>{index.name}</code> on <code>{index.columns.join(", ")}</code>{" "}
                  {index.primary ? (
                    <Label variant="accent">primary</Label>
                  ) : index.unique ? (
                    <Label>unique</Label>
                  ) : null}{" "}
                  {index.method ? <span className="muted">({index.method})</span> : null}
                </li>
              ))}
            </ul>
          )}
        </section>
        <section className={`${box.box} ${styles.metadataBox}`} aria-label="Foreign keys">
          <div className={box.header}>Foreign keys</div>
          {detail.foreign_keys.length === 0 ? (
            <p className={box.row}>No foreign keys.</p>
          ) : (
            <ul className={box.list}>
              {detail.foreign_keys.map((fk) => (
                <li key={fk.name} className={box.row}>
                  <code>{fk.columns.join(", ")}</code> references{" "}
                  <code>
                    {fk.ref_schema}.{fk.ref_table}({fk.ref_columns.join(", ")})
                  </code>
                  <div className="muted">
                    On update: {fk.on_update ?? "default"}. On delete: {fk.on_delete ?? "default"}.
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  );
}

/** Memoised: Primer re-creates link anchors on every render, which would swallow a click that a data load interrupts. */
const TableNav = memo(
  function TableNav({ tables, selected }: { tables: TableInfo[]; selected: string | null }) {
    return (
      <NavList aria-label="Tables">
        {tables.map((item) => (
          <NavList.Item
            key={item.name}
            as={Link}
            to={{
              search: `?${new URLSearchParams({ schema: item.schema, table: item.name }).toString()}`,
            }}
            aria-current={selected === item.name ? "page" : undefined}
          >
            <NavList.LeadingVisual>
              {item.kind === "table" ? <TableIcon /> : <EyeIcon />}
            </NavList.LeadingVisual>
            {item.name}
            <NavList.TrailingVisual>
              <span className="muted" title={`${kindLabel(item.kind)}, rows estimated`}>
                {item.approx_rows === null
                  ? kindLabel(item.kind)
                  : `~${item.approx_rows.toLocaleString()}`}
              </span>
            </NavList.TrailingVisual>
          </NavList.Item>
        ))}
      </NavList>
    );
  },
  (previous, next) =>
    previous.selected === next.selected &&
    JSON.stringify(previous.tables) === JSON.stringify(next.tables),
);

export function DatabaseBrowserPage() {
  const { slug = "", env: rawEnv, name = "" } = useParams();
  const env: EnvironmentName = ENVIRONMENTS.find((item) => item === rawEnv) ?? "development";
  const [params, setParams] = useSearchParams();
  const connection = useConnection(slug, env, name);
  const [system, setSystem] = useState(false);
  const [filter, setFilter] = useState("");
  const fillViewportRef = useFillViewport();
  const schemas = useSchemas(slug, env, name, system);
  const schemaList = schemas.data?.schemas ?? [];
  const schema =
    params.get("schema") ??
    schemaList.find((item) => item.name === "public")?.name ??
    schemaList.find((item) => !item.system)?.name ??
    schemaList[0]?.name ??
    null;
  const tables = useTables(slug, env, name, schema);
  const table = params.get("table");
  const tab = params.get("tab") === "rows" ? "rows" : "structure";
  const detail = useTableDetail(slug, env, name, schema, table);
  useDocumentTitle(`${name} · databases`);

  const select = (next: Record<string, string>) => {
    setParams(next, { replace: false });
  };

  if (connection.isPending) return <Loading label="Loading connection" />;
  if (connection.isError) {
    return isApiError(connection.error) && connection.error.status === 404 ? (
      <EmptyState
        icon={<DatabaseIcon size={32} aria-hidden="true" />}
        title="Connection not found"
        action={
          <Button as={Link} to={databasesPath(slug, env)}>
            Back to databases
          </Button>
        }
      >
        There is no connection called {name} in this environment.
      </EmptyState>
    ) : (
      <ErrorCallout title="Could not load the connection" error={connection.error} />
    );
  }
  const conn = connection.data;
  const base = `${databasesPath(slug, env)}/${encodeURIComponent(name)}`;

  return (
    <div ref={fillViewportRef} className={styles.viewer}>
      <div className={styles.header}>
        <Link className={styles.back} to={databasesPath(slug, env)}>
          ← Databases
        </Link>
        <div className={styles.connection}>
          <Heading as="h2" variant="small">
            {conn.name}
          </Heading>
          <Label>{conn.engine}</Label>
          <span className={`${styles.address} ltr`}>
            {conn.host}:{conn.port}/{conn.database}
          </span>
        </div>
        <LinkButton className={styles.console} size="small" as={Link} to={`${base}/console`}>
          SQL console
        </LinkButton>
      </div>
      {env === "production" ? (
        <Callout variant="warning" title="Production data">
          Rows need the admin permission and each page you open is recorded in the audit log (table
          and page, never the data).
        </Callout>
      ) : null}
      <div className={styles.split}>
        <div className={styles.pane}>
          <div className={styles.paneInner}>
            <div className={styles.controls}>
              <Select
                size="small"
                aria-label="Schema"
                value={schema ?? ""}
                onChange={(event) => {
                  select({ schema: event.target.value });
                }}
              >
                {schemaList.map((item) => (
                  <Select.Option key={item.name} value={item.name}>
                    {item.name}
                    {item.system ? " (system)" : ""}
                  </Select.Option>
                ))}
              </Select>
              <FormControl>
                <Checkbox
                  checked={system}
                  onChange={(event) => {
                    setSystem(event.target.checked);
                  }}
                />
                <FormControl.Label>Show system schemas</FormControl.Label>
              </FormControl>
            </div>
            <TextInput
              className={styles.filter}
              size="small"
              block
              leadingVisual={SearchIcon}
              placeholder="Filter tables"
              aria-label="Filter tables"
              value={filter}
              onChange={(event) => {
                setFilter(event.target.value);
              }}
            />
            <div className={styles.listScroll}>
              {schemas.isPending ? <Loading label="Loading schemas" /> : null}
              {schemas.isError ? (
                <Callout variant="danger" role="alert">
                  {errorMessage(schemas.error)}
                </Callout>
              ) : null}
              {tables.data ? (
                tables.data.tables.length === 0 ? (
                  <p className="muted">This schema has no tables.</p>
                ) : tables.data.tables.filter((item) =>
                    item.name.toLocaleLowerCase().includes(filter.toLocaleLowerCase()),
                  ).length > 0 ? (
                  <TableNav
                    tables={tables.data.tables.filter((item) =>
                      item.name.toLocaleLowerCase().includes(filter.toLocaleLowerCase()),
                    )}
                    selected={table}
                  />
                ) : (
                  <p className="muted">No tables match.</p>
                )
              ) : null}
              {tables.isError ? (
                <Callout variant="danger" role="alert">
                  {errorMessage(tables.error)}
                </Callout>
              ) : null}
            </div>
          </div>
        </div>
        <div className={styles.content}>
          {table === null || schema === null ? (
            <EmptyState icon={<TableIcon size={32} aria-hidden="true" />} title="Choose a table">
              Pick a table on the left to see its columns, indexes and relations, and to page
              through its rows. Row counts next to the names are estimates from the database.
            </EmptyState>
          ) : (
            <div className={styles.contentInner}>
              <Heading className={styles.tableHeading} as="h3" variant="small">
                {schema}.{table}
              </Heading>
              <UnderlinePanels
                aria-label="Table sections"
                value={tab}
                onChange={({ value }) => {
                  select({ schema, table, tab: value });
                }}
              >
                <UnderlinePanels.Tab value="structure">Structure</UnderlinePanels.Tab>
                <UnderlinePanels.Tab value="rows">Rows</UnderlinePanels.Tab>
                <UnderlinePanels.Panel value="structure">
                  <div className={styles.panel}>
                    {tab === "structure" ? (
                      <>
                        {detail.isPending ? <Loading label="Loading structure" /> : null}
                        {detail.isError ? (
                          <ErrorCallout title="Could not load the structure" error={detail.error} />
                        ) : null}
                        {detail.data ? <Structure detail={detail.data} /> : null}
                      </>
                    ) : null}
                  </div>
                </UnderlinePanels.Panel>
                <UnderlinePanels.Panel value="rows">
                  <div className={styles.panel}>
                    {tab === "rows" ? (
                      <RowsPanel
                        key={`${schema}.${table}`}
                        slug={slug}
                        env={env}
                        name={name}
                        schema={schema}
                        table={table}
                      />
                    ) : null}
                  </div>
                </UnderlinePanels.Panel>
              </UnderlinePanels>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

interface Position {
  cursor: string | null;
  offset: number;
}

function RowsPanel({
  slug,
  env,
  name,
  schema,
  table,
}: {
  slug: string;
  env: EnvironmentName;
  name: string;
  schema: string;
  table: string;
}) {
  const [limit, setLimit] = useState<number>(100);
  const [trail, setTrail] = useState<Position[]>([{ cursor: null, offset: 0 }]);
  const current = trail[trail.length - 1] ?? { cursor: null, offset: 0 };
  const rows = useRows(slug, env, name, schema, table, { limit, ...current }, true);
  const page = rows.data;

  return (
    <div className={styles.rowsPanel}>
      <div className={styles.toolbar}>
        <FormControl>
          <FormControl.Label visuallyHidden>Rows per page</FormControl.Label>
          <Select
            size="small"
            value={String(limit)}
            onChange={(event) => {
              setLimit(Number(event.target.value));
              setTrail([{ cursor: null, offset: 0 }]);
            }}
          >
            {LIMITS.map((value) => (
              <Select.Option key={value} value={String(value)}>
                {value} rows per page
              </Select.Option>
            ))}
          </Select>
        </FormControl>
        <ButtonGroup>
          <Button
            size="small"
            disabled={trail.length === 1}
            onClick={() => {
              setTrail([{ cursor: null, offset: 0 }]);
            }}
          >
            First page
          </Button>
          <Button
            size="small"
            disabled={trail.length === 1}
            onClick={() => {
              setTrail((value) => value.slice(0, -1));
            }}
          >
            Previous
          </Button>
          <Button
            size="small"
            disabled={!page || (page.next_cursor === null && page.next_offset === null)}
            onClick={() => {
              if (!page) return;
              setTrail((value) => [
                ...value,
                { cursor: page.next_cursor, offset: page.next_offset ?? 0 },
              ]);
            }}
          >
            Next
          </Button>
        </ButtonGroup>
        {page ? (
          <p className={styles.status} role="status">
            {page.rows.length === 0
              ? "Rows 0–0"
              : `Rows ${String(page.offset + 1)}–${String(page.offset + page.rows.length)}`}
          </p>
        ) : null}
      </div>
      {rows.isPending ? <Loading label="Loading rows" /> : null}
      {rows.isError ? (
        <Callout variant="danger" role="alert" title="Could not read the rows">
          {errorMessage(rows.error)}
        </Callout>
      ) : null}
      {page ? (
        <>
          {page.size_limited ? (
            <Callout>
              This page was cut short because it would be very large. Press Next to continue where
              it stopped.
            </Callout>
          ) : null}
          <ResultTable
            columns={page.columns}
            rows={page.rows}
            label={`Rows of ${table}`}
            primaryKey={page.ordering === "primary_key" ? page.order_by : []}
          />
          <p className={styles.help}>
            {page.ordering === "primary_key"
              ? `Ordered by primary key (${page.order_by.join(", ")}).`
              : page.ordering === "columns"
                ? `No primary key: ordered by ${page.order_by.join(", ")} and paged by position.`
                : "No primary key or sortable columns: order is not guaranteed."}
          </p>
        </>
      ) : null}
    </div>
  );
}

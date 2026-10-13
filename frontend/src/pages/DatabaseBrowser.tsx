import { DatabaseIcon, EyeIcon, TableIcon } from "@primer/octicons-react";
import {
  Button,
  ButtonGroup,
  Checkbox,
  FormControl,
  Heading,
  Label,
  LinkButton,
  NavList,
  PageLayout,
  Select,
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
import box from "../styles/box.module.css";
import tableStyles from "./Containers.module.css";
import { databasesPath } from "./Databases";

const LIMITS = [50, 100, 500] as const;

function kindLabel(kind: TableInfo["kind"]): string {
  return kind.replaceAll("_", " ");
}

function Structure({ detail }: { detail: TableDetail }) {
  return (
    <div className={box.stack}>
      <section className={`${box.box} ${tableStyles.scroll}`} aria-label="Columns">
        <div className={box.header}>Columns</div>
        <table className={tableStyles.table} aria-label="Columns of the table">
          <thead>
            <tr>
              <th scope="col">#</th>
              <th scope="col">Name</th>
              <th scope="col">Type</th>
              <th scope="col">Null?</th>
              <th scope="col">Default</th>
              <th scope="col">Key</th>
            </tr>
          </thead>
          <tbody>
            {detail.columns.map((column) => (
              <tr key={column.name}>
                <td>{column.position}</td>
                <th scope="row">
                  <code>{column.name}</code>
                </th>
                <td>
                  <code>{column.data_type}</code>
                  {column.extra ? <span className="muted"> {column.extra}</span> : null}
                </td>
                <td>{column.nullable ? "yes" : "no"}</td>
                <td>
                  {column.default === null ? (
                    <span className="muted">none</span>
                  ) : (
                    <code>{column.default}</code>
                  )}
                </td>
                <td>
                  {column.primary_key_position === null ? (
                    ""
                  ) : (
                    <Label variant="accent">primary key {column.primary_key_position}</Label>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <section className={box.box} aria-label="Indexes">
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
      <section className={box.box} aria-label="Foreign keys">
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
    <div className={box.stack}>
      <div>
        <Link to={databasesPath(slug, env)}>← Databases</Link>
      </div>
      <div className={box.spread}>
        <div className={box.cluster}>
          <Heading as="h2" variant="medium">
            {conn.name}
          </Heading>
          <Label>{conn.engine}</Label>
          <span className="muted ltr">
            {conn.host}:{conn.port}/{conn.database}
          </span>
        </div>
        <LinkButton as={Link} to={`${base}/console`}>
          SQL console
        </LinkButton>
      </div>
      {env === "production" ? (
        <Callout variant="warning" title="Production data">
          Rows need the admin permission and each page you open is recorded in the audit log (table
          and page, never the data).
        </Callout>
      ) : null}
      <PageLayout containerWidth="full" padding="none" columnGap="normal" rowGap="normal">
        <PageLayout.Pane position="start" width="medium" padding="none" divider="none">
          <div className={box.stack}>
            <FormControl>
              <FormControl.Label>Schema</FormControl.Label>
              <Select
                block
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
            </FormControl>
            <FormControl>
              <Checkbox
                checked={system}
                onChange={(event) => {
                  setSystem(event.target.checked);
                }}
              />
              <FormControl.Label>Show system schemas</FormControl.Label>
            </FormControl>
            {schemas.isPending ? <Loading label="Loading schemas" /> : null}
            {schemas.isError ? (
              <Callout variant="danger" role="alert">
                {errorMessage(schemas.error)}
              </Callout>
            ) : null}
            {tables.data ? (
              tables.data.tables.length === 0 ? (
                <p className="muted">This schema has no tables.</p>
              ) : (
                <TableNav tables={tables.data.tables} selected={table} />
              )
            ) : null}
            {tables.isError ? (
              <Callout variant="danger" role="alert">
                {errorMessage(tables.error)}
              </Callout>
            ) : null}
          </div>
        </PageLayout.Pane>
        <PageLayout.Content as="div" padding="none">
          {table === null || schema === null ? (
            <EmptyState icon={<TableIcon size={32} aria-hidden="true" />} title="Choose a table">
              Pick a table on the left to see its columns, indexes and relations, and to page
              through its rows. Row counts next to the names are estimates from the database.
            </EmptyState>
          ) : (
            <div className={box.stack}>
              <div className={box.cluster}>
                <Heading as="h3" variant="medium">
                  {schema}.{table}
                </Heading>
              </div>
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
                  <div className={box.section}>
                    {detail.isPending ? <Loading label="Loading structure" /> : null}
                    {detail.isError ? (
                      <ErrorCallout title="Could not load the structure" error={detail.error} />
                    ) : null}
                    {detail.data ? <Structure detail={detail.data} /> : null}
                  </div>
                </UnderlinePanels.Panel>
                <UnderlinePanels.Panel value="rows">
                  <div className={box.section}>
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
        </PageLayout.Content>
      </PageLayout>
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
    <div className={box.stack}>
      <div className={box.cluster}>
        <FormControl>
          <FormControl.Label visuallyHidden>Rows per page</FormControl.Label>
          <Select
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
            disabled={trail.length === 1}
            onClick={() => {
              setTrail([{ cursor: null, offset: 0 }]);
            }}
          >
            First page
          </Button>
          <Button
            disabled={trail.length === 1}
            onClick={() => {
              setTrail((value) => value.slice(0, -1));
            }}
          >
            Previous
          </Button>
          <Button
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
      </div>
      {rows.isPending ? <Loading label="Loading rows" /> : null}
      {rows.isError ? (
        <Callout variant="danger" role="alert" title="Could not read the rows">
          {errorMessage(rows.error)}
        </Callout>
      ) : null}
      {page ? (
        <>
          <p className="muted" role="status">
            {page.rows.length === 0
              ? "No rows."
              : `Rows ${String(page.offset + 1)} to ${String(page.offset + page.rows.length)} of this page sequence. `}
            {page.ordering === "primary_key"
              ? `Ordered by the primary key (${page.order_by.join(", ")}); paging continues after the last key, so it stays fast and stable.`
              : page.ordering === "columns"
                ? `This table has no primary key; rows are ordered by ${page.order_by.join(", ")} and paged by position.`
                : "This table has no primary key or sortable columns, so the order is not guaranteed and paging is by position."}
          </p>
          {page.size_limited ? (
            <Callout>
              This page was cut short because it would be very large. Press Next to continue where
              it stopped.
            </Callout>
          ) : null}
          <ResultTable columns={page.columns} rows={page.rows} label={`Rows of ${table}`} />
        </>
      ) : null}
    </div>
  );
}

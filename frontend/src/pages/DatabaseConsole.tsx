import { PlayIcon } from "@primer/octicons-react";
import { Button, Details, Heading } from "@primer/react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useConnection, useRunQuery } from "../api/dbHooks";
import { isApiError } from "../api/client";
import { ENVIRONMENTS, type EnvironmentName } from "../api/types";
import { Callout, ErrorCallout, Loading } from "../components/Feedback";
import { useDocumentTitle } from "../components/PageHead";
import { ResultTable } from "../components/ResultTable";
import { CodeEditor } from "../pipeline/CodeEditor";
import box from "../styles/box.module.css";
import styles from "./DatabaseBrowser.module.css";
import { databasesPath } from "./Databases";

const MAX_CHARS = 20_000;
const HISTORY = 8;

/** What the server said, in words that tell the user what to do. */
function explain(error: unknown): { title: string; text: string } {
  if (isApiError(error)) {
    if (error.status === 403) {
      return {
        title: "Not allowed",
        text: "Production data needs the admin permission. Ask an administrator, or run the query on Development.",
      };
    }
    if (error.status === 409) {
      return {
        title: "The database is not reachable",
        text: `${error.message}. Use Test connection on the Databases page to find out why.`,
      };
    }
    if (error.status === 422) {
      return {
        title: error.message.includes("timed out")
          ? "The statement took too long"
          : "The statement was not run",
        text: error.message.includes("timed out")
          ? `${error.message}. Statements are stopped after 5 seconds; add a WHERE or LIMIT, or select fewer columns.`
          : `${error.message}.`,
      };
    }
    return { title: "Could not run the statement", text: error.message };
  }
  return { title: "Could not run the statement", text: "Something went wrong. Try again." };
}

/** Read-only SQL console: one SELECT/WITH/SHOW/EXPLAIN at a time, 5 s, 500 rows, rolled back. */
export default function DatabaseConsolePage() {
  const { slug = "", env: rawEnv, name = "" } = useParams();
  const env: EnvironmentName = ENVIRONMENTS.find((item) => item === rawEnv) ?? "development";
  const connection = useConnection(slug, env, name);
  const run = useRunQuery(slug, env, name);
  const [statement, setStatement] = useState("SELECT 1");
  const [history, setHistory] = useState<string[]>([]);
  useDocumentTitle(`SQL console · ${name}`);

  const execute = () => {
    const text = statement.trim();
    if (text === "" || run.isPending) return;
    run.mutate(text, {
      onSuccess: () => {
        setHistory((current) =>
          [text, ...current.filter((item) => item !== text)].slice(0, HISTORY),
        );
      },
    });
  };

  if (connection.isPending) return <Loading label="Loading connection" />;
  if (connection.isError) {
    return <ErrorCallout title="Could not load the connection" error={connection.error} />;
  }
  const conn = connection.data;
  const failure = run.isError ? explain(run.error) : null;
  const result = run.data;

  return (
    <div className={`${styles.viewer} ${styles.consoleViewer}`}>
      <div>
        <Link to={`${databasesPath(slug, env)}/${encodeURIComponent(name)}`}>← {conn.name}</Link>
      </div>
      <Heading as="h2" variant="small">
        SQL console: {conn.name}
      </Heading>
      {env === "production" ? (
        <Callout variant="warning" title="Production data">
          Needs the admin permission. The statement text, its outcome and the number of rows are
          recorded in the audit log (never the results). Prefer a read-only database user.
        </Callout>
      ) : null}
      <Details>
        <Details.Summary>What can I run here?</Details.Summary>
        <ul>
          <li>
            One statement at a time, starting with SELECT, WITH, SHOW or EXPLAIN. A final semicolon
            is fine.
          </li>
          <li>
            Anything that could change data or the server is refused: INSERT, UPDATE, DELETE, DDL,
            SELECT … INTO, locking reads, EXPLAIN ANALYZE and functions such as pg_sleep.
          </li>
          <li>
            Statements are stopped after 5 seconds and show at most 500 rows (the result says when
            it was cut).
          </li>
          <li>Every statement runs in a read-only transaction that is rolled back.</li>
          <li>At most {MAX_CHARS.toLocaleString()} characters.</li>
        </ul>
      </Details>
      <div className={styles.consoleEditor}>
        <CodeEditor
          label="SQL statement"
          value={statement}
          readOnly={false}
          diagnostics={[]}
          language={conn.engine}
          onChange={setStatement}
          onRun={execute}
        />
      </div>
      <div className={box.spread}>
        <div className={box.cluster}>
          <Button
            variant="primary"
            leadingVisual={PlayIcon}
            disabled={run.isPending || statement.trim() === "" || statement.length > MAX_CHARS}
            onClick={execute}
          >
            {run.isPending ? "Running…" : "Run"}
          </Button>
          <span className="muted">Ctrl+Enter (Cmd+Enter on a Mac) also runs it.</span>
        </div>
        <span className={statement.length > MAX_CHARS ? "" : "muted"} role="status">
          {statement.length.toLocaleString()} / {MAX_CHARS.toLocaleString()} characters
        </span>
      </div>
      {failure ? (
        <Callout variant="danger" role="alert" title={failure.title}>
          {failure.text}
        </Callout>
      ) : null}
      {result ? (
        <section aria-label="Result" className={styles.consoleResult}>
          <p role="status" className="muted">
            {result.statement_kind}: {result.row_count} {result.row_count === 1 ? "row" : "rows"} in{" "}
            {result.elapsed_ms} ms.
          </p>
          {result.truncated ? (
            <Callout variant="warning" title="Showing the first rows only">
              The result was cut at {result.limit} rows. Add a WHERE or LIMIT to see something
              specific.
            </Callout>
          ) : null}
          {result.size_limited ? (
            <Callout>The result was cut short because it would be very large.</Callout>
          ) : null}
          <ResultTable
            columns={result.columns}
            rows={result.rows}
            label="Query result"
            emptyTitle="No rows returned"
            emptyDescription=""
          />
        </section>
      ) : null}
      {history.length > 0 ? (
        <section aria-label="Recent statements" className={box.stack}>
          <Heading as="h3" variant="small">
            Recent statements
          </Heading>
          <ul className={box.list}>
            {history.map((item) => (
              <li key={item}>
                <Button
                  variant="link"
                  onClick={() => {
                    setStatement(item);
                  }}
                >
                  <code>{item.length > 100 ? `${item.slice(0, 100)}…` : item}</code>
                </Button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

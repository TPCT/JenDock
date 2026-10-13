import { DatabaseIcon, PlusIcon } from "@primer/octicons-react";
import {
  Button,
  ConfirmationDialog,
  Dialog,
  FormControl,
  Label,
  LinkButton,
  Select,
  TextInput,
} from "@primer/react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  useConnections,
  useCreateConnection,
  useDeleteConnection,
  useTestConnection,
  useUpdateConnection,
} from "../api/dbHooks";
import type { DbConnection, DbEngine, TestResult, TlsMode } from "../api/dbTypes";
import { ENVIRONMENTS, type EnvironmentName } from "../api/types";
import { EnvToolbar } from "../components/EnvSwitch";
import { Callout, EmptyState, ErrorCallout, Loading, errorMessage } from "../components/Feedback";
import { RelativeTime } from "../components/Time";
import { capitalize } from "../lib/format";
import box from "../styles/box.module.css";

export function databasesPath(slug: string, env: string): string {
  return `/projects/${encodeURIComponent(slug)}/environments/${env}/databases`;
}

export const READ_ONLY_ADVICE =
  "Create a dedicated database user that only has SELECT privileges and use it here. Every session is opened read-only and rolled back, but that is defence in depth: the database user is the strongest guarantee.";

const ENGINE_LABEL: Record<DbEngine, string> = {
  postgresql: "PostgreSQL",
  mysql: "MySQL",
  mariadb: "MariaDB",
};

/** Test failures in plain English, by the server's error code. */
export function explainTestError(code: string, message: string): string {
  switch (code) {
    case "connect_failed":
      return `The network was reachable but the database did not accept the connection: ${message}. Check the host, port, database, user and password.`;
    case "network_denied":
      return `This host is outside the environment's private network (${message}). Use the name of a service in the environment, for example "db", not a public address or the host machine.`;
    case "network_unavailable":
      return "The environment's network does not exist yet. Deploy the environment first so that its containers are running, then test again.";
    case "secret_missing":
      return "The stored password is missing. Edit the connection and enter the password again.";
    case "helper_unavailable":
      return "The short-lived helper that opens the connection could not be started. Try again in a moment; if it keeps failing, ask an administrator.";
    case "timeout":
      return "The database did not answer within 30 seconds, so the attempt was cancelled.";
    default:
      return message;
  }
}

function TestOutcome({ result }: { result: TestResult }) {
  if (result.ok && result.server) {
    const { server } = result;
    return (
      <Callout variant="success" role="status" title="Connection works">
        {server.version}, database {server.database} as {server.current_user},{" "}
        {server.tls ? "encrypted (TLS)" : "not encrypted"}.{" "}
        {server.read_only
          ? "The session is read-only, as intended."
          : "The session is NOT read-only; use a user without write privileges."}
      </Callout>
    );
  }
  return (
    <Callout variant="danger" role="alert" title="Connection failed">
      {explainTestError(result.error?.code ?? "", result.error?.message ?? "unknown error")}
    </Callout>
  );
}

function ConnectionDialog({
  slug,
  env,
  existing,
  onClose,
}: {
  slug: string;
  env: EnvironmentName;
  existing: DbConnection | null;
  onClose: () => void;
}) {
  const create = useCreateConnection(slug, env);
  const update = useUpdateConnection(slug, env, existing?.name ?? "");
  const [name, setName] = useState(existing?.name ?? "main");
  const [engine, setEngine] = useState<DbEngine>(existing?.engine ?? "postgresql");
  const [host, setHost] = useState(existing?.host ?? "db");
  const [port, setPort] = useState(String(existing?.port ?? 5432));
  const [database, setDatabase] = useState(existing?.database ?? "");
  const [username, setUsername] = useState(existing?.username ?? "");
  const [password, setPassword] = useState("");
  const [tls, setTls] = useState<TlsMode>(existing?.tls_mode ?? "disable");
  const [problem, setProblem] = useState<string | null>(null);
  const mutation = existing ? update : create;

  const submit = () => {
    const portNumber = Number(port);
    let found: string | null = null;
    if (!existing && !/^[a-z0-9][a-z0-9-]{0,47}$/.test(name)) {
      found = "A name uses lowercase letters, digits and dashes, for example main.";
    } else if (host.trim() === "") found = "Enter the host: the name of the database service.";
    else if (!Number.isInteger(portNumber) || portNumber < 1 || portNumber > 65535) {
      found = "The port must be a number from 1 to 65535.";
    } else if (database.trim() === "") found = "Enter the database name.";
    else if (username.trim() === "") found = "Enter the user name.";
    else if (!existing && password === "") found = "Enter the password.";
    if (found !== null) {
      setProblem(found);
      return;
    }
    setProblem(null);
    const fields = {
      engine,
      host: host.trim(),
      port: portNumber,
      database: database.trim(),
      username: username.trim(),
      tls_mode: tls,
      ...(password === "" ? {} : { password }),
    };
    if (existing) update.mutate(fields, { onSuccess: onClose });
    else create.mutate({ name, password, ...fields }, { onSuccess: onClose });
  };

  return (
    <Dialog
      title={existing ? `Edit ${existing.name}` : "Add a database connection"}
      subtitle="The password is stored encrypted on the server and is never shown again."
      width="large"
      onClose={onClose}
      footerButtons={[
        { buttonType: "default", content: "Cancel", onClick: onClose },
        {
          buttonType: "primary",
          content: mutation.isPending ? "Saving…" : existing ? "Save changes" : "Add connection",
          disabled: mutation.isPending,
          onClick: submit,
        },
      ]}
    >
      <div className={box.stack}>
        <Callout variant={env === "production" ? "warning" : "default"}>{READ_ONLY_ADVICE}</Callout>
        {existing ? null : (
          <FormControl required>
            <FormControl.Label>Name</FormControl.Label>
            <TextInput
              block
              monospace
              value={name}
              onChange={(event) => {
                setName(event.target.value);
              }}
            />
            <FormControl.Caption>How you refer to it here, for example main.</FormControl.Caption>
          </FormControl>
        )}
        <FormControl>
          <FormControl.Label>Engine</FormControl.Label>
          <Select
            block
            value={engine}
            onChange={(event) => {
              const next = event.target.value as DbEngine;
              setEngine(next);
              if (port === "5432" || port === "3306")
                setPort(next === "postgresql" ? "5432" : "3306");
            }}
          >
            {(Object.keys(ENGINE_LABEL) as DbEngine[]).map((value) => (
              <Select.Option key={value} value={value}>
                {ENGINE_LABEL[value]}
              </Select.Option>
            ))}
          </Select>
        </FormControl>
        <FormControl required>
          <FormControl.Label>Host</FormControl.Label>
          <TextInput
            block
            monospace
            value={host}
            onChange={(event) => {
              setHost(event.target.value);
            }}
          />
          <FormControl.Caption>
            The name of the database service in this environment (as in its compose file, for
            example db). Connections are made from inside the environment&apos;s private network, so
            localhost and the host machine are not allowed.
          </FormControl.Caption>
        </FormControl>
        <FormControl required>
          <FormControl.Label>Port</FormControl.Label>
          <TextInput
            block
            type="number"
            value={port}
            onChange={(event) => {
              setPort(event.target.value);
            }}
          />
        </FormControl>
        <FormControl required>
          <FormControl.Label>Database</FormControl.Label>
          <TextInput
            block
            monospace
            value={database}
            onChange={(event) => {
              setDatabase(event.target.value);
            }}
          />
        </FormControl>
        <FormControl required>
          <FormControl.Label>User</FormControl.Label>
          <TextInput
            block
            monospace
            autoComplete="off"
            value={username}
            onChange={(event) => {
              setUsername(event.target.value);
            }}
          />
          <FormControl.Caption>
            A user that can only read, ideally with SELECT only.
          </FormControl.Caption>
        </FormControl>
        <FormControl required={!existing}>
          <FormControl.Label>Password</FormControl.Label>
          <TextInput
            block
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(event) => {
              setPassword(event.target.value);
            }}
          />
          <FormControl.Caption>
            {existing
              ? "Leave empty to keep the stored password; type a new one to replace it."
              : "Stored as an encrypted secret that only the build worker can read."}
          </FormControl.Caption>
        </FormControl>
        <FormControl>
          <FormControl.Label>Encryption (TLS)</FormControl.Label>
          <Select
            block
            value={tls}
            onChange={(event) => {
              setTls(event.target.value as TlsMode);
            }}
          >
            <Select.Option value="disable">
              Off: plain connection inside the private network
            </Select.Option>
            <Select.Option value="require">Require: encrypt the connection</Select.Option>
          </Select>
          <FormControl.Caption>
            Require encrypts the traffic but does not check the server&apos;s certificate.
          </FormControl.Caption>
        </FormControl>
        {problem ? (
          <Callout variant="danger" role="alert">
            {problem}
          </Callout>
        ) : null}
        {mutation.isError ? (
          <Callout variant="danger" role="alert">
            {errorMessage(mutation.error)}
          </Callout>
        ) : null}
      </div>
    </Dialog>
  );
}

/** Database tab: the environment's saved connections. */
export function DatabasesPage() {
  const { slug = "", env: rawEnv } = useParams();
  const env = ENVIRONMENTS.find((name) => name === rawEnv) ?? null;
  const connections = useConnections(slug, env ?? "development");
  const test = useTestConnection(slug, env ?? "development");
  const remove = useDeleteConnection(slug, env ?? "development");
  const [dialog, setDialog] = useState<{ existing: DbConnection | null } | null>(null);
  const [deleting, setDeleting] = useState<DbConnection | null>(null);
  const [tested, setTested] = useState<{ name: string; result: TestResult } | null>(null);

  if (env === null) {
    return (
      <EmptyState
        icon={<DatabaseIcon size={32} aria-hidden="true" />}
        title="Unknown environment"
        action={
          <Button as={Link} to={databasesPath(slug, "development")}>
            Go to Development databases
          </Button>
        }
      >
        Every project has exactly two environments: development and production.
      </EmptyState>
    );
  }

  return (
    <div className={box.stack}>
      <EnvToolbar
        title={`${capitalize(env)} databases`}
        subtitle="Look at your data without leaving the browser. Connections are read-only and made from inside the environment's private network."
        env={env}
        pathFor={(next) => databasesPath(slug, next)}
        actions={
          <Button
            variant="primary"
            leadingVisual={PlusIcon}
            onClick={() => {
              setDialog({ existing: null });
            }}
          >
            Add connection
          </Button>
        }
      />
      {env === "production" ? (
        <Callout variant="warning" title="Production data">
          Browsing rows and running queries here needs the admin permission and every access is
          recorded in the audit log (the table and page, or the statement text; never the data
          itself). {READ_ONLY_ADVICE}
        </Callout>
      ) : null}
      {connections.isPending ? <Loading label="Loading connections" /> : null}
      {connections.isError ? (
        <ErrorCallout
          title="Could not load the connections"
          error={connections.error}
          onRetry={() => void connections.refetch()}
        />
      ) : null}
      {connections.data?.length === 0 ? (
        <EmptyState icon={<DatabaseIcon size={32} aria-hidden="true" />} title="No connections yet">
          Add a connection to the environment&apos;s database to browse its tables and run read-only
          queries. {env === "production" ? "" : READ_ONLY_ADVICE}
        </EmptyState>
      ) : null}
      {tested ? (
        <div>
          <TestOutcome result={tested.result} />
        </div>
      ) : null}
      {test.isError ? (
        <Callout variant="danger" role="alert">
          {errorMessage(test.error)}
        </Callout>
      ) : null}
      {connections.data && connections.data.length > 0 ? (
        <ul className={`${box.box} ${box.list}`} aria-label="Database connections">
          {connections.data.map((item) => (
            <li key={item.name} className={box.row} aria-label={item.name}>
              <div className={box.spread}>
                <div className={box.cluster}>
                  <strong>{item.name}</strong>
                  <Label>{ENGINE_LABEL[item.engine]}</Label>
                  {item.last_test_ok === null ? (
                    <Label variant="secondary">not tested</Label>
                  ) : item.last_test_ok ? (
                    <Label variant="success">last test passed</Label>
                  ) : (
                    <Label variant="danger">last test failed</Label>
                  )}
                </div>
                <div className={box.cluster}>
                  <LinkButton
                    as={Link}
                    to={`${databasesPath(slug, env)}/${item.name}`}
                    size="small"
                    variant="primary"
                  >
                    Browse
                  </LinkButton>
                  <LinkButton
                    as={Link}
                    to={`${databasesPath(slug, env)}/${item.name}/console`}
                    size="small"
                  >
                    SQL console
                  </LinkButton>
                  <Button
                    size="small"
                    disabled={test.isPending}
                    aria-label={`Test connection ${item.name}`}
                    onClick={() => {
                      setTested(null);
                      test.mutate(item.name, {
                        onSuccess: (result) => {
                          setTested({ name: item.name, result });
                        },
                      });
                    }}
                  >
                    {test.isPending && test.variables === item.name
                      ? "Testing…"
                      : "Test connection"}
                  </Button>
                  <Button
                    size="small"
                    aria-label={`Edit ${item.name}`}
                    onClick={() => {
                      setDialog({ existing: item });
                    }}
                  >
                    Edit
                  </Button>
                  <Button
                    size="small"
                    variant="danger"
                    aria-label={`Delete ${item.name}`}
                    onClick={() => {
                      setDeleting(item);
                    }}
                  >
                    Delete
                  </Button>
                </div>
              </div>
              <p className="muted">
                <code>
                  {item.username}@{item.host}:{item.port}/{item.database}
                </code>{" "}
                · {item.tls_mode === "require" ? "encrypted" : "not encrypted"} ·{" "}
                {item.last_tested_at ? (
                  <>
                    tested <RelativeTime iso={item.last_tested_at} />
                  </>
                ) : (
                  "never tested"
                )}
              </p>
              {item.notice && env !== "production" ? <p className="muted">{item.notice}</p> : null}
            </li>
          ))}
        </ul>
      ) : null}

      {dialog ? (
        <ConnectionDialog
          slug={slug}
          env={env}
          existing={dialog.existing}
          onClose={() => {
            setDialog(null);
          }}
        />
      ) : null}
      {deleting ? (
        <ConfirmationDialog
          title={`Delete ${deleting.name}?`}
          confirmButtonContent="Delete connection"
          cancelButtonContent="Keep it"
          confirmButtonType="danger"
          confirmButtonLoading={remove.isPending}
          onClose={(gesture) => {
            if (gesture === "confirm") {
              remove.mutate(deleting.name, {
                onSuccess: () => {
                  setDeleting(null);
                },
              });
            } else setDeleting(null);
          }}
        >
          <p>
            The saved connection and its stored password are deleted. The database itself is not
            touched.
          </p>
          {remove.isError ? (
            <Callout variant="danger" role="alert">
              {errorMessage(remove.error)}
            </Callout>
          ) : null}
        </ConfirmationDialog>
      ) : null}
    </div>
  );
}

import { DownloadIcon, SearchIcon } from "@primer/octicons-react";
import { Button, FormControl, LinkButton, Select, TextInput } from "@primer/react";
import { useState, type SyntheticEvent } from "react";
import { auditQuery, useAuditActions, useAuditLog } from "../api/accountHooks";
import { EMPTY_AUDIT_FILTERS, type AuditFilters } from "../api/accountTypes";
import { AuditList } from "../components/AuditList";
import { EmptyState, ErrorCallout, Loading } from "../components/Feedback";
import { PageHead } from "../components/PageHead";
import box from "../styles/box.module.css";
import styles from "./AuditLog.module.css";

const ACTOR_TYPES = [
  { value: "", label: "Anyone" },
  { value: "user", label: "A user" },
  { value: "token", label: "An API token" },
  { value: "system", label: "The system" },
] as const;

const TARGET_TYPES = [
  "user",
  "project",
  "environment",
  "run",
  "deployment",
  "domain",
  "api_token",
  "database_connection",
  "webhook",
] as const;

/** Actions grouped by their first segment so the drop-down offers "all secret actions" too. */
function actionOptions(actions: string[]): { value: string; label: string }[] {
  const groups = [...new Set(actions.map((action) => action.split(".")[0] ?? action))].sort();
  return groups.flatMap((group) => [
    { value: `${group}.*`, label: `${group}.* (all)` },
    ...actions
      .filter((action) => action.startsWith(`${group}.`))
      .map((a) => ({ value: a, label: a })),
  ]);
}

function FilterForm({
  applied,
  onApply,
}: {
  applied: AuditFilters;
  onApply: (filters: AuditFilters) => void;
}) {
  const [draft, setDraft] = useState<AuditFilters>(applied);
  const actions = useAuditActions();
  const set = (key: keyof AuditFilters) => (value: string) => {
    setDraft((current) => ({ ...current, [key]: value }));
  };
  const submit = (event: SyntheticEvent<HTMLFormElement>) => {
    event.preventDefault();
    onApply(draft);
  };
  const clear = () => {
    setDraft(EMPTY_AUDIT_FILTERS);
    onApply(EMPTY_AUDIT_FILTERS);
  };
  const moreOpen = [
    draft.project,
    draft.target_type,
    draft.target_id,
    draft.since,
    draft.until,
  ].some((value) => value !== "");
  const invertedRange = draft.since !== "" && draft.until !== "" && draft.since > draft.until;

  return (
    <form
      className={styles.filters}
      onSubmit={submit}
      role="search"
      aria-label="Filter the audit log"
    >
      <FormControl>
        <FormControl.Label>Search</FormControl.Label>
        <TextInput
          block
          leadingVisual={SearchIcon}
          value={draft.q}
          maxLength={100}
          placeholder="Action, name, host, target…"
          onChange={(event) => {
            set("q")(event.target.value);
          }}
        />
      </FormControl>
      <FormControl>
        <FormControl.Label>Actor</FormControl.Label>
        <Select
          block
          value={draft.actor_type}
          onChange={(event) => {
            set("actor_type")(event.target.value);
          }}
        >
          {ACTOR_TYPES.map((option) => (
            <Select.Option key={option.value} value={option.value}>
              {option.label}
            </Select.Option>
          ))}
        </Select>
      </FormControl>
      <FormControl>
        <FormControl.Label>Action</FormControl.Label>
        <Select
          block
          value={draft.action}
          onChange={(event) => {
            set("action")(event.target.value);
          }}
        >
          <Select.Option value="">Any action</Select.Option>
          {actionOptions(actions.data?.actions ?? []).map((option) => (
            <Select.Option key={option.value} value={option.value}>
              {option.label}
            </Select.Option>
          ))}
        </Select>
      </FormControl>
      <details className={styles.more} open={moreOpen}>
        <summary>More filters</summary>
        <div className={styles.moreGrid}>
          <FormControl>
            <FormControl.Label>Project</FormControl.Label>
            <TextInput
              block
              value={draft.project}
              maxLength={64}
              placeholder="project short name"
              autoCapitalize="none"
              spellCheck={false}
              onChange={(event) => {
                set("project")(event.target.value);
              }}
            />
          </FormControl>
          <FormControl>
            <FormControl.Label>Target type</FormControl.Label>
            <Select
              block
              value={draft.target_type}
              onChange={(event) => {
                set("target_type")(event.target.value);
              }}
            >
              <Select.Option value="">Any target</Select.Option>
              {TARGET_TYPES.map((type) => (
                <Select.Option key={type} value={type}>
                  {type}
                </Select.Option>
              ))}
            </Select>
          </FormControl>
          <FormControl>
            <FormControl.Label>Target id</FormControl.Label>
            <TextInput
              block
              value={draft.target_id}
              maxLength={64}
              spellCheck={false}
              onChange={(event) => {
                set("target_id")(event.target.value);
              }}
            />
          </FormControl>
          <FormControl>
            <FormControl.Label>From</FormControl.Label>
            <TextInput
              block
              type="date"
              value={draft.since}
              onChange={(event) => {
                set("since")(event.target.value);
              }}
            />
          </FormControl>
          <FormControl>
            <FormControl.Label>To</FormControl.Label>
            <TextInput
              block
              type="date"
              value={draft.until}
              validationStatus={invertedRange ? "error" : undefined}
              onChange={(event) => {
                set("until")(event.target.value);
              }}
            />
            {invertedRange ? (
              <FormControl.Validation variant="error">
                The end date is before the start date.
              </FormControl.Validation>
            ) : null}
          </FormControl>
        </div>
      </details>
      <div className={styles.actions}>
        <Button type="submit" variant="primary" disabled={invertedRange}>
          Search
        </Button>
        <Button type="button" onClick={clear}>
          Clear filters
        </Button>
      </div>
    </form>
  );
}

export function AuditLogPage() {
  const [applied, setApplied] = useState<AuditFilters>(EMPTY_AUDIT_FILTERS);
  const log = useAuditLog(applied);
  const events = log.data?.pages.flatMap((page) => page.items) ?? [];
  const filtered = Object.values(applied).some((value) => value !== "");
  const query = auditQuery(applied);

  return (
    <>
      <PageHead
        title="Audit log"
        description="Every change and sign-in, newest first. Secret values are never recorded."
        actions={
          <LinkButton
            href={`/api/audit/export.csv${query ? `?${query}` : ""}`}
            download=""
            leadingVisual={DownloadIcon}
          >
            Export CSV
          </LinkButton>
        }
      />
      <div className={box.section}>
        <div className={box.stack}>
          <FilterForm applied={applied} onApply={setApplied} />
          {log.isPending ? <Loading label="Loading the audit log" /> : null}
          {log.isError ? (
            <ErrorCallout
              title="Could not load the audit log"
              error={log.error}
              onRetry={() => void log.refetch()}
            />
          ) : null}
          {log.data && events.length === 0 ? (
            <EmptyState
              icon={<SearchIcon size={32} aria-hidden="true" />}
              title={filtered ? "No events match" : "No events yet"}
              action={
                filtered ? (
                  <Button
                    onClick={() => {
                      setApplied(EMPTY_AUDIT_FILTERS);
                    }}
                  >
                    Clear filters
                  </Button>
                ) : null
              }
            >
              {filtered
                ? "Try a wider date range or fewer filters."
                : "Sign-ins and changes made in Jendock will be recorded here."}
            </EmptyState>
          ) : null}
          {events.length > 0 ? (
            <div className={box.box}>
              <AuditList events={events} label="Audit events" />
              <div className={box.footer}>
                <span className="muted">Showing {String(events.length)} events</span>
                {log.hasNextPage ? (
                  <Button
                    size="small"
                    disabled={log.isFetchingNextPage}
                    onClick={() => void log.fetchNextPage()}
                  >
                    {log.isFetchingNextPage ? "Loading…" : "Load older events"}
                  </Button>
                ) : (
                  <span className="muted">End of the log</span>
                )}
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </>
  );
}

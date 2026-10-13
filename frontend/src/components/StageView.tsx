import {
  ChevronDownIcon,
  ChevronRightIcon,
  ChevronUpIcon,
  GearIcon,
  SearchIcon,
  XCircleFillIcon,
} from "@primer/octicons-react";
import {
  ActionList,
  ActionMenu,
  CounterLabel,
  Heading,
  IconButton,
  TextInput,
  VisuallyHidden,
} from "@primer/react";
import { useRef, useState, useSyncExternalStore } from "react";
import type { RunDetail, StageRun, StepRun } from "../api/types";
import type { useRunStream } from "../api/useRunStream";
import { countMatches, hasTimestamps } from "../lib/ansi";
import { STATUS_LABEL } from "../lib/format";
import box from "../styles/box.module.css";
import { LogViewer } from "./LogViewer";
import styles from "./StageView.module.css";
import { StatusIcon } from "./Status";
import { Duration } from "./Time";

type Store = ReturnType<typeof useRunStream>["store"];

function StepRow({
  step,
  open,
  onToggle,
  run,
  store,
  stale,
  search,
  timestamps,
  matches,
}: {
  step: StepRun;
  open: boolean;
  onToggle: () => void;
  run: RunDetail;
  store: Store;
  stale: boolean;
  search: string;
  timestamps: boolean;
  matches: number;
}) {
  const panelId = `step-log-${step.id}`;
  const Chevron = open ? ChevronDownIcon : ChevronRightIcon;
  return (
    <li className={styles.step}>
      <button
        type="button"
        className={styles.head}
        aria-expanded={open}
        aria-controls={panelId}
        onClick={onToggle}
      >
        <Chevron size={16} aria-hidden="true" className={styles.chevron} />
        <StatusIcon status={step.status} />
        <span className={styles.label}>
          <VisuallyHidden>{STATUS_LABEL[step.status]}: </VisuallyHidden>
          {step.label}
        </span>
        <code className={styles.type}>{step.step_type}</code>
        {matches > 0 ? <CounterLabel>{matches}</CounterLabel> : null}
        <span className={styles.duration}>
          <Duration startedAt={step.started_at} finishedAt={step.finished_at} />
        </span>
      </button>
      {open ? (
        <div id={panelId} className={styles.body}>
          {step.exit_code !== null && step.exit_code !== 0 ? (
            <p className={styles.exit}>Exited with code {step.exit_code}.</p>
          ) : null}
          <LogViewer
            store={store}
            runId={run.id}
            stepId={step.id}
            running={step.status === "running"}
            stale={stale}
            search={search}
            timestamps={timestamps}
          />
        </div>
      ) : null}
    </li>
  );
}

/**
 * One stage like an Actions job page: header with log search and settings, then the steps as
 * collapsible groups, each with its log.
 */
export function StageView({
  stage,
  openSteps,
  toggle,
  run,
  store,
  stale,
}: {
  stage: StageRun;
  openSteps: ReadonlySet<number>;
  toggle: (id: number) => void;
  run: RunDetail;
  store: Store;
  stale: boolean;
}) {
  const [search, setSearch] = useState("");
  const [timestamps, setTimestamps] = useState(false);
  const [cursor, setCursor] = useState(-1);
  const list = useRef<HTMLUListElement>(null);
  useSyncExternalStore(store.subscribe, () => store.version);

  // Subscribing to the store version re-renders this view whenever any log buffer changes; the
  // counts are cheap (nothing runs without a search term) so they are derived on every render.
  const counts = new Map<number, number>();
  let total = 0;
  let anyTimestamps = false;
  for (const step of stage.steps) {
    if (!openSteps.has(step.id)) continue;
    const text = store.get(step.id).text;
    const count = countMatches(text, search);
    counts.set(step.id, count);
    total += count;
    anyTimestamps = anyTimestamps || hasTimestamps(text);
  }

  const go = (delta: 1 | -1) => {
    const marks = Array.from(list.current?.querySelectorAll("mark") ?? []);
    if (marks.length === 0) return;
    const next = (cursor + delta + marks.length) % marks.length;
    marks.forEach((mark) => {
      mark.removeAttribute("data-current");
    });
    const target = marks[next];
    target?.setAttribute("data-current", "true");
    target?.scrollIntoView({ block: "center" });
    setCursor(next);
  };

  return (
    <section className={box.box} aria-labelledby={`stage-${stage.id}-title`}>
      <div className={box.header}>
        <Heading as="h2" id={`stage-${stage.id}-title`} className={styles.stageTitle}>
          <StatusIcon status={stage.status} />
          {stage.name}
          <span className={styles.stageTime}>
            <Duration startedAt={stage.started_at} finishedAt={stage.finished_at} />
          </span>
        </Heading>
        <div className={styles.tools}>
          <TextInput
            size="small"
            aria-label="Search logs"
            placeholder="Search logs"
            leadingVisual={SearchIcon}
            value={search}
            onChange={(event) => {
              setSearch(event.target.value);
              setCursor(-1);
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                go(event.shiftKey ? -1 : 1);
              }
            }}
            trailingAction={
              search === "" ? undefined : (
                <TextInput.Action
                  icon={XCircleFillIcon}
                  aria-label="Clear search"
                  onClick={() => {
                    setSearch("");
                    setCursor(-1);
                  }}
                />
              )
            }
          />
          {search !== "" ? (
            <>
              <span className={styles.count} role="status">
                {total} {total === 1 ? "match" : "matches"} in open steps
              </span>
              <IconButton
                size="small"
                variant="invisible"
                icon={ChevronUpIcon}
                aria-label="Previous match"
                disabled={total === 0}
                onClick={() => {
                  go(-1);
                }}
              />
              <IconButton
                size="small"
                variant="invisible"
                icon={ChevronDownIcon}
                aria-label="Next match"
                disabled={total === 0}
                onClick={() => {
                  go(1);
                }}
              />
            </>
          ) : null}
          <ActionMenu>
            <ActionMenu.Anchor>
              <IconButton
                size="small"
                variant="invisible"
                icon={GearIcon}
                aria-label="Log settings"
              />
            </ActionMenu.Anchor>
            <ActionMenu.Overlay width="medium">
              <ActionList selectionVariant="multiple">
                <ActionList.Item
                  selected={timestamps}
                  disabled={!anyTimestamps}
                  onSelect={() => {
                    setTimestamps((value) => !value);
                  }}
                >
                  Show timestamps
                  <ActionList.Description variant="block">
                    {anyTimestamps
                      ? "Time of day, UTC, at the start of each line."
                      : "The open logs carry no timestamps."}
                  </ActionList.Description>
                </ActionList.Item>
              </ActionList>
            </ActionMenu.Overlay>
          </ActionMenu>
        </div>
      </div>
      {stage.steps.length === 0 ? (
        <p className={box.row}>No steps have started in this stage.</p>
      ) : (
        <ul ref={list} className={styles.steps}>
          {stage.steps.map((step) => (
            <StepRow
              key={step.id}
              step={step}
              open={openSteps.has(step.id)}
              onToggle={() => {
                toggle(step.id);
              }}
              run={run}
              store={store}
              stale={stale}
              search={search}
              timestamps={timestamps}
              matches={counts.get(step.id) ?? 0}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

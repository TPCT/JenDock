import { ChevronRightIcon } from "@primer/octicons-react";
import type { StageRun } from "../api/types";
import { STATUS_LABEL } from "../lib/format";
import styles from "./StageGraph.module.css";
import { StatusIcon } from "./Status";
import { Duration } from "./Time";

/** Groups stages by `column`: columns run in sequence, stages inside one column in parallel. */
export function groupColumns(stages: StageRun[]): StageRun[][] {
  const byColumn = new Map<number, StageRun[]>();
  for (const stage of stages) {
    // Without `column` (backend does not report parallel structure) every stage is sequential.
    const column = stage.column ?? stage.position;
    byColumn.set(column, [...(byColumn.get(column) ?? []), stage]);
  }
  return [...byColumn.entries()]
    .sort(([a], [b]) => a - b)
    .map(([, items]) => items.sort((a, b) => a.position - b.position));
}

/**
 * The Actions "jobs" graph: one card per stage, sequential columns left to right with parallel
 * stages stacked. A card opens that stage's steps and logs.
 */
export function StageGraph({
  stages,
  onSelect,
}: {
  stages: StageRun[];
  onSelect: (id: number) => void;
}) {
  const columns = groupColumns(stages);
  if (columns.length === 0) {
    return <p className="muted">Stages appear here once the pipeline starts.</p>;
  }
  return (
    <ol className={styles.graph} aria-label="Pipeline stages">
      {columns.map((column, index) => {
        const parallel = column.length > 1;
        return (
          <li key={column[0]?.id ?? index} className={styles.column}>
            {index > 0 ? <ChevronRightIcon className={styles.arrow} aria-hidden="true" /> : null}
            <ul
              className={parallel ? `${styles.group} ${styles.parallel}` : styles.group}
              aria-label={parallel ? "Stages that run in parallel" : undefined}
            >
              {parallel ? <li className={styles.tag}>Parallel</li> : null}
              {column.map((stage) => (
                <li key={stage.id}>
                  <button
                    type="button"
                    className={`${styles.node} ${styles[stage.status] ?? ""}`}
                    onClick={() => {
                      onSelect(stage.id);
                    }}
                    aria-label={`${stage.name}, ${STATUS_LABEL[stage.status]}. Go to its steps`}
                  >
                    <StatusIcon status={stage.status} />
                    <span className={styles.name}>{stage.name}</span>
                    <span className={styles.time}>
                      <Duration startedAt={stage.started_at} finishedAt={stage.finished_at} />
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </li>
        );
      })}
    </ol>
  );
}

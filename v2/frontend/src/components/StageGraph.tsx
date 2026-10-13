import { ChevronRightIcon } from "@primer/octicons-react";
import type { StageRun } from "../api/types";
import { STATUS_LABEL } from "../lib/format";
import { StatusIcon } from "./Status";
import { Duration } from "./Time";
import "./StageGraph.css";

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

export function stageAnchor(id: number): string {
  return `stage-${id}`;
}

export function StageGraph({ stages }: { stages: StageRun[] }) {
  const columns = groupColumns(stages);
  if (columns.length === 0) {
    return <p className="muted">Stages appear here once the pipeline starts.</p>;
  }
  const jump = (id: number) => {
    const target = document.getElementById(stageAnchor(id));
    target?.scrollIntoView({ block: "start" });
    target?.focus();
  };
  return (
    <ol className="graph" aria-label="Pipeline stages">
      {columns.map((column, index) => {
        const parallel = column.length > 1;
        return (
          <li key={column[0]?.id ?? index} className="graph-column">
            {index > 0 ? <ChevronRightIcon className="graph-arrow" aria-hidden="true" /> : null}
            <ul
              className={parallel ? "graph-group graph-parallel" : "graph-group"}
              aria-label={parallel ? "Stages that run in parallel" : undefined}
            >
              {parallel ? <li className="graph-tag muted">Parallel</li> : null}
              {column.map((stage) => (
                <li key={stage.id}>
                  <button
                    type="button"
                    className={`graph-node graph-${stage.status}`}
                    onClick={() => {
                      jump(stage.id);
                    }}
                    aria-label={`${stage.name}, ${STATUS_LABEL[stage.status]}. Go to its steps`}
                  >
                    <StatusIcon status={stage.status} />
                    <span className="graph-name">{stage.name}</span>
                    <span className="graph-time muted">
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

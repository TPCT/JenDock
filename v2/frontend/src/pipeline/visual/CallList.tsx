import { ArrowDownIcon, ArrowUpIcon, PlusIcon, TrashIcon } from "@primer/octicons-react";
import { useId, useRef, useState } from "react";
import type { Call, StepDef } from "../../api/pipelineTypes";
import { useEditorEnv, moveItem, removeAt, replaceAt } from "./context";
import { ArgField } from "./fields";
import { useFocusAfterMove } from "./useFocusAfterMove";

export function newCall(def: StepDef): Call {
  const args: Call["args"] = {};
  for (const arg of def.args) {
    if (!arg.required) continue;
    if (arg.type === "string") args[arg.name] = "";
    else if (arg.type === "int")
      args[arg.name] = typeof arg.default === "number" ? arg.default : (arg.min ?? 1);
    else if (arg.type === "list") args[arg.name] = [];
    else if (arg.type === "bool") args[arg.name] = false;
  }
  if (def.takes_block) return { name: def.name, args, body: [] };
  return { name: def.name, args };
}

function CallCard({
  call,
  def,
  index,
  count,
  onChange,
  onMove,
  onRemove,
  scope,
}: {
  call: Call;
  def: StepDef | undefined;
  index: number;
  count: number;
  onChange: (call: Call) => void;
  onMove: (delta: -1 | 1) => void;
  onRemove: () => void;
  scope: string;
}) {
  const { catalog } = useEditorEnv();
  const title = def?.title ?? call.name;
  return (
    <li className="card step-card" data-card={`${scope}-${index}`}>
      <div className="card-head">
        <div>
          <strong>{title}</strong> <code className="muted">{call.name}</code>
        </div>
        <div className="row-actions">
          <button
            type="button"
            className="btn btn-icon"
            data-focus-key={`${scope}-${index}-up`}
            aria-label={`Move ${title} ${index + 1} up`}
            disabled={index === 0}
            onClick={() => {
              onMove(-1);
            }}
          >
            <ArrowUpIcon size={16} />
          </button>
          <button
            type="button"
            className="btn btn-icon"
            data-focus-key={`${scope}-${index}-down`}
            aria-label={`Move ${title} ${index + 1} down`}
            disabled={index === count - 1}
            onClick={() => {
              onMove(1);
            }}
          >
            <ArrowDownIcon size={16} />
          </button>
          <button
            type="button"
            className="btn btn-icon btn-danger"
            aria-label={`Remove ${title} ${index + 1}`}
            onClick={onRemove}
          >
            <TrashIcon size={16} />
          </button>
        </div>
      </div>
      {def ? (
        <>
          {def.description ? <p className="hint">{def.description}</p> : null}
          <div className="card-body">
            {def.args.map((arg) => (
              <ArgField
                key={arg.name}
                def={arg}
                value={call.args[arg.name]}
                onChange={(next) => {
                  const args: Call["args"] = Object.fromEntries(
                    Object.entries(call.args).filter(([key]) => key !== arg.name),
                  );
                  if (next !== undefined) args[arg.name] = next;
                  onChange({ ...call, args });
                }}
              />
            ))}
            {def.takes_block ? (
              <div className="nested">
                <p className="hint">Steps inside:</p>
                <CallList
                  calls={call.body ?? []}
                  defs={catalog.steps}
                  scope={`${scope}-${index}-body`}
                  onChange={(body) => {
                    onChange({ ...call, body });
                  }}
                />
              </div>
            ) : null}
          </div>
        </>
      ) : (
        <p className="hint">
          This item is not in the catalogue. Edit it in the Text tab; it is kept as is.
        </p>
      )}
    </li>
  );
}

/** An ordered list of steps/options/parameters whose forms come from the server catalogue. */
export function CallList({
  calls,
  defs,
  onChange,
  scope,
  addLabel = "Add step",
  typeLabel = "Step type",
  emptyText = "Nothing here yet.",
}: {
  calls: Call[];
  defs: StepDef[];
  onChange: (calls: Call[]) => void;
  scope: string;
  addLabel?: string;
  typeLabel?: string;
  emptyText?: string;
}) {
  const container = useRef<HTMLDivElement>(null);
  const focus = useFocusAfterMove(container);
  const selectId = useId();
  const [choice, setChoice] = useState("");
  const selected = defs.find((item) => item.name === choice) ?? defs[0];

  return (
    <div ref={container} className="calllist">
      {calls.length === 0 ? <p className="muted">{emptyText}</p> : null}
      <ol className="cards">
        {calls.map((call, index) => (
          <CallCard
            key={index}
            call={call}
            def={defs.find((item) => item.name === call.name)}
            index={index}
            count={calls.length}
            scope={scope}
            onChange={(next) => {
              onChange(replaceAt(calls, index, next));
            }}
            onMove={(delta) => {
              focus(`${scope}-${index + delta}-${delta < 0 ? "up" : "down"}`);
              onChange(moveItem(calls, index, delta));
            }}
            onRemove={() => {
              onChange(removeAt(calls, index));
            }}
          />
        ))}
      </ol>
      <div className="add-row">
        <div className="field">
          <label htmlFor={selectId}>{typeLabel}</label>
          <select
            id={selectId}
            className="input"
            value={selected?.name ?? ""}
            onChange={(event) => {
              setChoice(event.target.value);
            }}
          >
            {defs.map((item) => (
              <option key={item.name} value={item.name}>
                {item.title}
              </option>
            ))}
          </select>
        </div>
        <button
          type="button"
          className="btn"
          disabled={!selected}
          onClick={() => {
            if (selected) onChange([...calls, newCall(selected)]);
          }}
        >
          <PlusIcon size={16} aria-hidden="true" />
          {addLabel}
        </button>
      </div>
    </div>
  );
}

import { ArrowDownIcon, ArrowUpIcon, PlusIcon } from "@primer/octicons-react";
import { ActionList, ActionMenu, IconButton } from "@primer/react";
import { useRef } from "react";
import type { Call, StepDef } from "../../api/pipelineTypes";
import styles from "../pipeline.module.css";
import { RemoveButton } from "../ui";
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
    <li className={styles.card} data-card={`${scope}-${index}`}>
      <div className={styles.cardHead}>
        <div>
          <strong>{title}</strong> <code className="muted">{call.name}</code>
        </div>
        <div className={styles.cluster}>
          <IconButton
            icon={ArrowUpIcon}
            size="small"
            data-focus-key={`${scope}-${index}-up`}
            aria-label={`Move ${title} ${index + 1} up`}
            tooltipDirection="s"
            disabled={index === 0}
            onClick={() => {
              onMove(-1);
            }}
          />
          <IconButton
            icon={ArrowDownIcon}
            size="small"
            data-focus-key={`${scope}-${index}-down`}
            aria-label={`Move ${title} ${index + 1} down`}
            tooltipDirection="s"
            disabled={index === count - 1}
            onClick={() => {
              onMove(1);
            }}
          />
          <RemoveButton label={`Remove ${title} ${index + 1}`} onClick={onRemove} />
        </div>
      </div>
      {def ? (
        <>
          {def.description ? <p className={styles.hint}>{def.description}</p> : null}
          <div className={styles.cardBody}>
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
              <div className={styles.nested}>
                <p className={styles.hint}>Steps inside:</p>
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
        <p className={styles.hint}>
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

  return (
    <div ref={container} className={styles.stack}>
      {calls.length === 0 ? <p className="muted">{emptyText}</p> : null}
      <ol className={styles.cards}>
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
      <div>
        <ActionMenu>
          <ActionMenu.Button size="small" leadingVisual={PlusIcon} disabled={defs.length === 0}>
            {addLabel}
          </ActionMenu.Button>
          <ActionMenu.Overlay width="large" maxHeight="medium">
            <ActionList>
              <ActionList.Group>
                <ActionList.GroupHeading>{typeLabel}</ActionList.GroupHeading>
                {defs.map((item) => (
                  <ActionList.Item
                    key={item.name}
                    onSelect={() => {
                      onChange([...calls, newCall(item)]);
                    }}
                  >
                    {item.title}
                    {item.description ? (
                      <ActionList.Description variant="block">
                        {item.description}
                      </ActionList.Description>
                    ) : null}
                  </ActionList.Item>
                ))}
              </ActionList.Group>
            </ActionList>
          </ActionMenu.Overlay>
        </ActionMenu>
      </div>
    </div>
  );
}

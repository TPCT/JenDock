import { ArrowDownIcon, ArrowUpIcon } from "@primer/octicons-react";
import {
  ConfirmationDialog,
  Details,
  FormControl,
  IconButton,
  Radio,
  RadioGroup,
  TextInput,
  VisuallyHidden,
} from "@primer/react";
import { useId, useRef, useState, type KeyboardEvent } from "react";
import type { Stage, StageBody } from "../../api/pipelineTypes";
import styles from "../pipeline.module.css";
import { AddButton, Group, RemoveButton } from "../ui";
import { CallList } from "./CallList";
import { moveItem, removeAt, replaceAt, useEditorEnv } from "./context";
import { EnvEditor, AgentPicker, PostEditor, WhenEditor } from "./Sections";
import { useFocusAfterMove } from "./useFocusAfterMove";

export function newStage(name: string): Stage {
  return {
    name,
    agent: null,
    environment: [],
    options: [],
    when: null,
    post: null,
    body: { type: "steps", steps: [] },
  };
}

function uniqueName(stages: Stage[], base: string): string {
  const taken = new Set(stages.map((stage) => stage.name));
  if (!taken.has(base)) return base;
  let n = 2;
  while (taken.has(`${base} ${n}`)) n += 1;
  return `${base} ${n}`;
}

type BodyType = StageBody["type"];

function bodyIsEmpty(body: StageBody): boolean {
  return body.type === "steps" ? body.steps.length === 0 : body.stages.length === 0;
}

function StageCard({
  stage,
  index,
  count,
  scope,
  inParallel,
  onChange,
  onMove,
  onRemove,
}: {
  stage: Stage;
  index: number;
  count: number;
  scope: string;
  inParallel: boolean;
  onChange: (stage: Stage) => void;
  onMove: (delta: -1 | 1) => void;
  onRemove: () => void;
}) {
  const { catalog } = useEditorEnv();
  const nameId = useId();
  const [confirmBody, setConfirmBody] = useState<BodyType | null>(null);
  const label = stage.name || `stage ${index + 1}`;

  const setBody = (type: BodyType) => {
    onChange({
      ...stage,
      body: type === "steps" ? { type, steps: [] } : { type, stages: [] },
    });
  };
  const requestBody = (type: BodyType) => {
    if (type === stage.body.type) return;
    if (bodyIsEmpty(stage.body)) setBody(type);
    else setConfirmBody(type);
  };

  const onNameKey = (event: KeyboardEvent<HTMLInputElement>) => {
    if (!event.altKey) return;
    if (event.key === "ArrowUp" && index > 0) {
      event.preventDefault();
      onMove(-1);
    } else if (event.key === "ArrowDown" && index < count - 1) {
      event.preventDefault();
      onMove(1);
    }
  };

  const bodyOptions: { type: BodyType; label: string }[] = [
    { type: "steps", label: "Run steps" },
    ...(inParallel ? [] : [{ type: "parallel" as const, label: "Run branches in parallel" }]),
    { type: "stages", label: "Contain nested stages" },
  ];

  return (
    <li className={styles.card} data-card={`${scope}-${index}`}>
      <div className={styles.cardHead}>
        <div className={styles.grow}>
          <FormControl>
            <FormControl.Label>Stage name</FormControl.Label>
            <TextInput
              block
              value={stage.name}
              onKeyDown={onNameKey}
              onChange={(event) => {
                onChange({ ...stage, name: event.target.value });
              }}
            />
            <FormControl.Caption>
              <VisuallyHidden>
                Press Alt and the up or down arrow to move this stage.
              </VisuallyHidden>
            </FormControl.Caption>
          </FormControl>
        </div>
        <div className={styles.cluster}>
          <IconButton
            icon={ArrowUpIcon}
            size="small"
            data-focus-key={`${scope}-${index}-up`}
            aria-label={`Move stage ${label} up`}
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
            aria-label={`Move stage ${label} down`}
            tooltipDirection="s"
            disabled={index === count - 1}
            onClick={() => {
              onMove(1);
            }}
          />
          <RemoveButton label={`Remove stage ${label}`} onClick={onRemove} />
        </div>
      </div>

      <RadioGroup
        name={`${nameId}-body`}
        onChange={(selected) => {
          if (selected !== null) requestBody(selected as BodyType);
        }}
      >
        <RadioGroup.Label>What does {label} do?</RadioGroup.Label>
        {bodyOptions.map((option) => (
          <FormControl key={option.type}>
            <Radio value={option.type} checked={stage.body.type === option.type} />
            <FormControl.Label>{option.label}</FormControl.Label>
          </FormControl>
        ))}
      </RadioGroup>

      {stage.body.type === "steps" ? (
        <CallList
          calls={stage.body.steps}
          defs={catalog.steps}
          scope={`${scope}-${index}-steps`}
          emptyText="This stage has no steps yet. Add one below."
          onChange={(steps) => {
            onChange({ ...stage, body: { type: "steps", steps } });
          }}
        />
      ) : (
        <div className={styles.nested}>
          <p className={styles.hint}>
            {stage.body.type === "parallel"
              ? "These branches run at the same time:"
              : "These stages run one after another inside this stage:"}
          </p>
          <StageList
            stages={stage.body.stages}
            scope={`${scope}-${index}-${stage.body.type}`}
            inParallel={stage.body.type === "parallel"}
            addLabel={stage.body.type === "parallel" ? "Add branch" : "Add nested stage"}
            onChange={(stages) => {
              onChange({
                ...stage,
                body:
                  stage.body.type === "parallel"
                    ? { type: "parallel", stages }
                    : { type: "stages", stages },
              });
            }}
          />
        </div>
      )}

      <Details className={styles.more}>
        <Details.Summary className={styles.summary}>Settings for {label}</Details.Summary>
        <div className={styles.stack}>
          <AgentPicker
            label={`Where ${label} runs`}
            allowInherit
            value={stage.agent}
            onChange={(agent) => {
              onChange({ ...stage, agent });
            }}
          />
          <WhenEditor
            value={stage.when}
            onChange={(when) => {
              onChange({ ...stage, when });
            }}
          />
          <Group legend={`Variables for ${label}`}>
            <EnvEditor
              scope={`${scope}-${index}-env`}
              value={stage.environment}
              onChange={(environment) => {
                onChange({ ...stage, environment });
              }}
            />
          </Group>
          <Group legend={`After ${label}`}>
            <PostEditor
              scope={`${scope}-${index}`}
              value={stage.post}
              onChange={(post) => {
                onChange({ ...stage, post });
              }}
            />
          </Group>
        </div>
      </Details>

      {confirmBody ? (
        <ConfirmationDialog
          title="Replace the contents of this stage?"
          confirmButtonContent="Replace"
          cancelButtonContent="Keep it"
          confirmButtonType="danger"
          onClose={(gesture) => {
            if (gesture === "confirm") setBody(confirmBody);
            setConfirmBody(null);
          }}
        >
          Changing what the stage does removes what it currently contains.
        </ConfirmationDialog>
      ) : null}
    </li>
  );
}

export function StageList({
  stages,
  onChange,
  scope,
  inParallel = false,
  addLabel = "Add stage",
}: {
  stages: Stage[];
  onChange: (stages: Stage[]) => void;
  scope: string;
  inParallel?: boolean;
  addLabel?: string;
}) {
  const container = useRef<HTMLDivElement>(null);
  const focus = useFocusAfterMove(container);
  return (
    <div ref={container} className={styles.stack}>
      {stages.length === 0 ? <p className="muted">No stages yet.</p> : null}
      <ol className={styles.cards}>
        {stages.map((stage, index) => (
          <StageCard
            key={index}
            stage={stage}
            index={index}
            count={stages.length}
            scope={scope}
            inParallel={inParallel}
            onChange={(next) => {
              onChange(replaceAt(stages, index, next));
            }}
            onMove={(delta) => {
              focus(`${scope}-${index + delta}-${delta < 0 ? "up" : "down"}`);
              onChange(moveItem(stages, index, delta));
            }}
            onRemove={() => {
              onChange(removeAt(stages, index));
            }}
          />
        ))}
      </ol>
      <div>
        <AddButton
          onClick={() => {
            onChange([...stages, newStage(uniqueName(stages, inParallel ? "Branch" : "Stage"))]);
          }}
        >
          {addLabel}
        </AddButton>
      </div>
    </div>
  );
}

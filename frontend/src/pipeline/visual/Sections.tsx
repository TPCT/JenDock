import { PlusIcon } from "@primer/octicons-react";
import { Button, FormControl, Radio, RadioGroup, Select, TextInput } from "@primer/react";
import { useId, useState } from "react";
import type {
  Agent,
  Condition,
  EnvVar,
  Post,
  PostBlock,
  PostCondition,
} from "../../api/pipelineTypes";
import styles from "../pipeline.module.css";
import { AddButton, RemoveButton } from "../ui";
import { CallList } from "./CallList";
import { removeAt, replaceAt, useEditorEnv } from "./context";
import { ImageField, StringField, textOf } from "./fields";

// ---- agent -------------------------------------------------------------------------------

type AgentChoice = "inherit" | "any" | "none" | "docker";

export function AgentPicker({
  value,
  onChange,
  allowInherit,
  label,
}: {
  value: Agent | null;
  onChange: (value: Agent | null) => void;
  allowInherit: boolean;
  label: string;
}) {
  const id = useId();
  const choice: AgentChoice = value === null ? "inherit" : value.type;
  const options: { value: AgentChoice; label: string; help: string }[] = [
    ...(allowInherit
      ? [
          {
            value: "inherit" as const,
            label: "Same as the pipeline",
            help: "Uses the pipeline's agent.",
          },
        ]
      : []),
    {
      value: "any",
      label: "Any available machine",
      help: "Platform steps only; shell commands need a Docker image.",
    },
    { value: "none", label: "No default agent", help: "Each stage chooses its own." },
    {
      value: "docker",
      label: "Run in a Docker image",
      help: "Shell commands run inside this image.",
    },
  ];
  const select = (next: AgentChoice) => {
    if (next === "inherit") onChange(null);
    else if (next === "docker") onChange({ type: "docker", image: "", args: null });
    else onChange({ type: next });
  };
  return (
    <div className={styles.field}>
      <RadioGroup
        name={`${id}-agent`}
        onChange={(selected) => {
          if (selected !== null) select(selected as AgentChoice);
        }}
      >
        <RadioGroup.Label>{label}</RadioGroup.Label>
        {options.map((option) => (
          <FormControl key={option.value}>
            <Radio value={option.value} checked={choice === option.value} />
            <FormControl.Label>{option.label}</FormControl.Label>
            <FormControl.Caption>{option.help}</FormControl.Caption>
          </FormControl>
        ))}
      </RadioGroup>
      {value?.type === "docker" ? (
        <div className={styles.nested}>
          <ImageField
            label="Image"
            value={textOf(value.image)}
            help="Pick an image built earlier in this pipeline, or an external one with a tag."
            onChange={(image) => {
              onChange({ ...value, image });
            }}
          />
          <StringField
            label="Extra Docker options (optional)"
            value={value.args ?? ""}
            help="Allowed: --network none, -e NAME=value, --memory N, --cpus N."
            onChange={(args) => {
              onChange({ ...value, args: textOf(args) === "" ? null : args });
            }}
          />
        </div>
      ) : null}
    </div>
  );
}

// ---- environment variables ---------------------------------------------------------------

export function EnvEditor({
  value,
  onChange,
  scope,
}: {
  value: EnvVar[];
  onChange: (value: EnvVar[]) => void;
  scope: string;
}) {
  const { secrets } = useEditorEnv();
  return (
    <div className={styles.stack} data-scope={scope}>
      {value.length === 0 ? <p className="muted">No variables.</p> : null}
      {value.map((variable, index) => {
        const isSecret = typeof variable.value === "object" && "credentials" in variable.value;
        const update = (next: EnvVar) => {
          onChange(replaceAt(value, index, next));
        };
        return (
          <div key={index} className={styles.envRow}>
            <FormControl>
              <FormControl.Label>Variable name</FormControl.Label>
              <TextInput
                block
                monospace
                value={variable.name}
                placeholder="NODE_ENV"
                onChange={(event) => {
                  update({ ...variable, name: event.target.value });
                }}
              />
            </FormControl>
            <FormControl>
              <FormControl.Label>Value comes from</FormControl.Label>
              <Select
                block
                value={isSecret ? "secret" : "text"}
                onChange={(event) => {
                  update({
                    ...variable,
                    value: event.target.value === "secret" ? { credentials: secrets[0] ?? "" } : "",
                  });
                }}
              >
                <Select.Option value="text">Plain text</Select.Option>
                <Select.Option value="secret">A secret</Select.Option>
              </Select>
            </FormControl>
            {typeof variable.value === "object" && "credentials" in variable.value ? (
              <FormControl>
                <FormControl.Label>Secret</FormControl.Label>
                <Select
                  block
                  value={variable.value.credentials}
                  onChange={(event) => {
                    update({ ...variable, value: { credentials: event.target.value } });
                  }}
                >
                  {secrets.length === 0 ? (
                    <Select.Option value="">No secrets defined yet</Select.Option>
                  ) : null}
                  {variable.value.credentials !== "" &&
                  !secrets.includes(variable.value.credentials) ? (
                    <Select.Option value={variable.value.credentials}>
                      {variable.value.credentials} (missing)
                    </Select.Option>
                  ) : null}
                  {secrets.map((name) => (
                    <Select.Option key={name} value={name}>
                      {name}
                    </Select.Option>
                  ))}
                </Select>
                <FormControl.Caption>
                  Only the name is shown. Secrets are managed in Settings.
                </FormControl.Caption>
              </FormControl>
            ) : (
              <StringField
                label="Value"
                value={variable.value}
                onChange={(next) => {
                  update({ ...variable, value: next });
                }}
              />
            )}
            <RemoveButton
              label={`Remove variable ${variable.name || index + 1}`}
              onClick={() => {
                onChange(removeAt(value, index));
              }}
            />
          </div>
        );
      })}
      <div>
        <AddButton
          onClick={() => {
            onChange([...value, { name: "", value: "" }]);
          }}
        >
          Add variable
        </AddButton>
      </div>
    </div>
  );
}

// ---- when --------------------------------------------------------------------------------

type WhenPreset = "always" | "branch" | "production" | "development" | "not-branch" | "custom";

function presetOf(when: Condition[] | null): WhenPreset {
  if (when === null || when.length === 0) return "always";
  const [only] = when;
  if (when.length !== 1 || !only) return "custom";
  if (only.type === "branch") return "branch";
  if (only.type === "environment" && only.name === "ENVIRONMENT") {
    const text = textOf(only.value);
    if (text === "production" || text === "development") return text;
  }
  if (only.type === "not" && only.condition.type === "branch") return "not-branch";
  return "custom";
}

export function WhenEditor({
  value,
  onChange,
}: {
  value: Condition[] | null;
  onChange: (value: Condition[] | null) => void;
}) {
  const { branch } = useEditorEnv();
  const preset = presetOf(value);
  const first = value?.[0];
  const pattern =
    first?.type === "branch"
      ? textOf(first.pattern)
      : first?.type === "not" && first.condition.type === "branch"
        ? textOf(first.condition.pattern)
        : branch;

  const apply = (next: WhenPreset, text: string) => {
    switch (next) {
      case "always":
        onChange(null);
        break;
      case "branch":
        onChange([{ type: "branch", pattern: text }]);
        break;
      case "not-branch":
        onChange([{ type: "not", condition: { type: "branch", pattern: text } }]);
        break;
      case "production":
      case "development":
        onChange([{ type: "environment", name: "ENVIRONMENT", value: next }]);
        break;
      case "custom":
        break;
    }
  };

  return (
    <div className={styles.field}>
      <FormControl>
        <FormControl.Label>When should this stage run?</FormControl.Label>
        <Select
          block
          value={preset}
          onChange={(event) => {
            apply(event.target.value as WhenPreset, pattern);
          }}
        >
          <Select.Option value="always">Always</Select.Option>
          <Select.Option value="branch">Only on a specific branch</Select.Option>
          <Select.Option value="production">Only when deploying to Production</Select.Option>
          <Select.Option value="development">Only when deploying to Development</Select.Option>
          <Select.Option value="not-branch">On every branch except one</Select.Option>
          {preset === "custom" ? (
            <Select.Option value="custom">Custom condition (kept as is)</Select.Option>
          ) : null}
        </Select>
        {preset === "custom" ? (
          <FormControl.Caption>
            This condition is more complex than the presets. Edit it in the Text tab.
          </FormControl.Caption>
        ) : null}
      </FormControl>
      {preset === "branch" || preset === "not-branch" ? (
        <div className={styles.nested}>
          <FormControl>
            <FormControl.Label>Branch name</FormControl.Label>
            <TextInput
              block
              monospace
              value={pattern}
              onChange={(event) => {
                apply(preset, event.target.value);
              }}
            />
            <FormControl.Caption>
              This environment deploys the {branch} branch. Patterns like release/* work.
            </FormControl.Caption>
          </FormControl>
        </div>
      ) : null}
    </div>
  );
}

// ---- post --------------------------------------------------------------------------------

const NOTIFY_TEMPLATES: Partial<Record<PostCondition, string>> = {
  failure: "Build ${BUILD_NUMBER} failed on ${BRANCH_NAME}",
  success: "Build ${BUILD_NUMBER} succeeded on ${BRANCH_NAME}",
  cleanup: "Cleaning up",
};

export function PostEditor({
  value,
  onChange,
  scope,
}: {
  value: Post | null;
  onChange: (value: Post | null) => void;
  scope: string;
}) {
  const { catalog } = useEditorEnv();
  const blocks = value?.blocks ?? [];
  const [pick, setPick] = useState("");
  const setBlocks = (next: PostBlock[]) => {
    onChange(next.length === 0 ? null : { blocks: next });
  };
  const addPreset = (condition: PostCondition) => {
    if (blocks.some((block) => block.condition === condition)) return;
    const message = NOTIFY_TEMPLATES[condition] ?? "Done";
    setBlocks([
      ...blocks,
      { condition, steps: [{ name: "echo", args: { message: { template: message } } }] },
    ]);
  };
  const free = catalog.post_conditions.filter((c) => !blocks.some((b) => b.condition === c.name));
  const presets: { condition: PostCondition; label: string }[] = [
    { condition: "failure", label: "Notify when it fails" },
    { condition: "success", label: "Notify when it succeeds" },
    { condition: "cleanup", label: "Clean up at the end" },
  ];
  return (
    <div className={styles.stack}>
      <div className={styles.cluster} role="group" aria-label="Quick additions">
        {presets.map((preset) => (
          <Button
            key={preset.condition}
            size="small"
            leadingVisual={PlusIcon}
            disabled={blocks.some((b) => b.condition === preset.condition)}
            onClick={() => {
              addPreset(preset.condition);
            }}
          >
            {preset.label}
          </Button>
        ))}
      </div>
      {blocks.map((block, index) => {
        const description = catalog.post_conditions.find(
          (c) => c.name === block.condition,
        )?.description;
        return (
          <section
            key={block.condition}
            className={styles.card}
            aria-label={`Run ${block.condition}`}
          >
            <div className={styles.cardHead}>
              <strong>{description ?? block.condition}</strong>
              <RemoveButton
                label={`Remove the ${block.condition} actions`}
                onClick={() => {
                  setBlocks(removeAt(blocks, index));
                }}
              />
            </div>
            <CallList
              calls={block.steps}
              defs={catalog.steps}
              scope={`${scope}-post-${block.condition}`}
              emptyText="No steps yet."
              onChange={(steps) => {
                setBlocks(replaceAt(blocks, index, { ...block, steps }));
              }}
            />
          </section>
        );
      })}
      {free.length > 0 ? (
        <div className={styles.addRow}>
          <FormControl>
            <FormControl.Label>Another moment</FormControl.Label>
            <Select
              value={free.some((c) => c.name === pick) ? pick : (free[0]?.name ?? "")}
              onChange={(event) => {
                setPick(event.target.value);
              }}
            >
              {free.map((c) => (
                <Select.Option key={c.name} value={c.name}>
                  {c.description}
                </Select.Option>
              ))}
            </Select>
          </FormControl>
          <AddButton
            onClick={() => {
              const chosen = free.find((c) => c.name === pick) ?? free[0];
              if (chosen) addPreset(chosen.name);
            }}
          >
            Add actions
          </AddButton>
        </div>
      ) : null}
    </div>
  );
}

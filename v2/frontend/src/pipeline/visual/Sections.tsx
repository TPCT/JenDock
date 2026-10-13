import { PlusIcon, TrashIcon } from "@primer/octicons-react";
import { useId, useState } from "react";
import type {
  Agent,
  Condition,
  EnvVar,
  Post,
  PostBlock,
  PostCondition,
} from "../../api/pipelineTypes";
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
    <fieldset className="subfield">
      <legend>{label}</legend>
      {options.map((option) => (
        <label key={option.value} className="check radio">
          <input
            type="radio"
            name={`${id}-agent`}
            checked={choice === option.value}
            onChange={() => {
              select(option.value);
            }}
          />
          <span>
            {option.label} <span className="hint">{option.help}</span>
          </span>
        </label>
      ))}
      {value?.type === "docker" ? (
        <div className="nested">
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
    </fieldset>
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
    <div className="stack">
      {value.length === 0 ? <p className="muted">No variables.</p> : null}
      {value.map((variable, index) => {
        const isSecret = typeof variable.value === "object" && "credentials" in variable.value;
        const update = (next: EnvVar) => {
          onChange(replaceAt(value, index, next));
        };
        return (
          <div key={index} className="env-row">
            <div className="field">
              <label htmlFor={`${scope}-name-${index}`}>Variable name</label>
              <input
                id={`${scope}-name-${index}`}
                className="input mono"
                value={variable.name}
                placeholder="NODE_ENV"
                onChange={(event) => {
                  update({ ...variable, name: event.target.value });
                }}
              />
            </div>
            <div className="field">
              <label htmlFor={`${scope}-kind-${index}`}>Value comes from</label>
              <select
                id={`${scope}-kind-${index}`}
                className="input"
                value={isSecret ? "secret" : "text"}
                onChange={(event) => {
                  update({
                    ...variable,
                    value: event.target.value === "secret" ? { credentials: secrets[0] ?? "" } : "",
                  });
                }}
              >
                <option value="text">Plain text</option>
                <option value="secret">A secret</option>
              </select>
            </div>
            {typeof variable.value === "object" && "credentials" in variable.value ? (
              <div className="field">
                <label htmlFor={`${scope}-secret-${index}`}>Secret</label>
                <select
                  id={`${scope}-secret-${index}`}
                  className="input"
                  value={variable.value.credentials}
                  onChange={(event) => {
                    update({ ...variable, value: { credentials: event.target.value } });
                  }}
                >
                  {secrets.length === 0 ? <option value="">No secrets defined yet</option> : null}
                  {variable.value.credentials !== "" &&
                  !secrets.includes(variable.value.credentials) ? (
                    <option value={variable.value.credentials}>
                      {variable.value.credentials} (missing)
                    </option>
                  ) : null}
                  {secrets.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
                <p className="hint">Only the name is shown. Secrets are managed in Settings.</p>
              </div>
            ) : (
              <StringField
                label="Value"
                value={variable.value}
                onChange={(next) => {
                  update({ ...variable, value: next });
                }}
              />
            )}
            <button
              type="button"
              className="btn btn-icon btn-danger"
              aria-label={`Remove variable ${variable.name || index + 1}`}
              onClick={() => {
                onChange(removeAt(value, index));
              }}
            >
              <TrashIcon size={16} />
            </button>
          </div>
        );
      })}
      <div>
        <button
          type="button"
          className="btn"
          onClick={() => {
            onChange([...value, { name: "", value: "" }]);
          }}
        >
          <PlusIcon size={16} aria-hidden="true" />
          Add variable
        </button>
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
  const id = useId();
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
    <div className="field">
      <label htmlFor={id}>When should this stage run?</label>
      <select
        id={id}
        className="input"
        value={preset}
        onChange={(event) => {
          apply(event.target.value as WhenPreset, pattern);
        }}
      >
        <option value="always">Always</option>
        <option value="branch">Only on a specific branch</option>
        <option value="production">Only when deploying to Production</option>
        <option value="development">Only when deploying to Development</option>
        <option value="not-branch">On every branch except one</option>
        {preset === "custom" ? <option value="custom">Custom condition (kept as is)</option> : null}
      </select>
      {preset === "custom" ? (
        <p className="hint">
          This condition is more complex than the presets. Edit it in the Text tab.
        </p>
      ) : null}
      {preset === "branch" || preset === "not-branch" ? (
        <div className="nested">
          <label htmlFor={`${id}-branch`}>Branch name</label>
          <input
            id={`${id}-branch`}
            className="input mono"
            value={pattern}
            onChange={(event) => {
              apply(preset, event.target.value);
            }}
          />
          <p className="hint">
            This environment deploys the {branch} branch. Patterns like release/* work.
          </p>
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
  const id = useId();
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
    <div className="stack">
      <div className="row-actions" role="group" aria-label="Quick additions">
        {presets.map((preset) => (
          <button
            key={preset.condition}
            type="button"
            className="btn"
            disabled={blocks.some((b) => b.condition === preset.condition)}
            onClick={() => {
              addPreset(preset.condition);
            }}
          >
            <PlusIcon size={16} aria-hidden="true" />
            {preset.label}
          </button>
        ))}
      </div>
      {blocks.map((block, index) => {
        const description = catalog.post_conditions.find(
          (c) => c.name === block.condition,
        )?.description;
        return (
          <section key={block.condition} className="card" aria-label={`Run ${block.condition}`}>
            <div className="card-head">
              <strong>{description ?? block.condition}</strong>
              <button
                type="button"
                className="btn btn-icon btn-danger"
                aria-label={`Remove the ${block.condition} actions`}
                onClick={() => {
                  setBlocks(removeAt(blocks, index));
                }}
              >
                <TrashIcon size={16} />
              </button>
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
        <div className="add-row">
          <div className="field">
            <label htmlFor={`${id}-cond`}>Another moment</label>
            <select
              id={`${id}-cond`}
              className="input"
              value={free.some((c) => c.name === pick) ? pick : (free[0]?.name ?? "")}
              onChange={(event) => {
                setPick(event.target.value);
              }}
            >
              {free.map((c) => (
                <option key={c.name} value={c.name}>
                  {c.description}
                </option>
              ))}
            </select>
          </div>
          <button
            type="button"
            className="btn"
            onClick={() => {
              const chosen = free.find((c) => c.name === pick) ?? free[0];
              if (chosen) addPreset(chosen.name);
            }}
          >
            <PlusIcon size={16} aria-hidden="true" />
            Add actions
          </button>
        </div>
      ) : null}
    </div>
  );
}

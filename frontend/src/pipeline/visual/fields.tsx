import { Checkbox, FormControl, Select, Textarea, TextInput } from "@primer/react";
import type { ArgDef, StringValue, Value } from "../../api/pipelineTypes";
import styles from "../pipeline.module.css";
import { AddButton, Group, RemoveButton } from "../ui";
import { useEditorEnv } from "./context";

export function textOf(value: Value | StringValue | undefined): string {
  if (typeof value === "string") return value;
  if (typeof value === "object" && !Array.isArray(value) && "template" in value) {
    return value.template;
  }
  return "";
}

export function isTemplate(value: Value | StringValue | undefined): boolean {
  return (
    value !== undefined && typeof value === "object" && !Array.isArray(value) && "template" in value
  );
}

function stringValue(text: string, template: boolean): StringValue {
  return template ? { template: text } : text;
}

const OTHER_IMAGE = "__other__";

/** Text input for a string value with an explicit switch for `${VARIABLE}` substitution. */
export function StringField({
  label,
  value,
  onChange,
  help,
  multiline = false,
  required = false,
  placeholder,
}: {
  label: string;
  value: StringValue;
  onChange: (value: StringValue) => void;
  help?: string;
  multiline?: boolean;
  required?: boolean;
  placeholder?: string;
}) {
  const template = isTemplate(value);
  const text = textOf(value);
  return (
    <div className={styles.field}>
      <FormControl required={required}>
        <FormControl.Label>{label}</FormControl.Label>
        {multiline ? (
          <Textarea
            block
            rows={3}
            className="mono"
            value={text}
            {...(placeholder ? { placeholder } : {})}
            spellCheck={false}
            onChange={(event) => {
              onChange(stringValue(event.target.value, template));
            }}
          />
        ) : (
          <TextInput
            block
            value={text}
            {...(placeholder ? { placeholder } : {})}
            spellCheck={false}
            onChange={(event) => {
              onChange(stringValue(event.target.value, template));
            }}
          />
        )}
        {help ? <FormControl.Caption>{help}</FormControl.Caption> : null}
      </FormControl>
      <FormControl>
        <Checkbox
          checked={template}
          onChange={(event) => {
            onChange(stringValue(text, event.target.checked));
          }}
        />
        <FormControl.Label>Substitute variables such as {"${BUILD_NUMBER}"}</FormControl.Label>
      </FormControl>
    </div>
  );
}

/** Choose an image built earlier in this pipeline, or type an external image reference. */
export function ImageField({
  label,
  value,
  onChange,
  help,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  help?: string;
}) {
  const { builtImages } = useEditorEnv();
  const isBuilt = builtImages.includes(value);
  const choice = isBuilt ? value : value === "" && builtImages.length > 0 ? "" : OTHER_IMAGE;
  return (
    <div className={styles.field}>
      <FormControl>
        <FormControl.Label>{label}</FormControl.Label>
        <Select
          block
          value={choice}
          onChange={(event) => {
            const next = event.target.value;
            onChange(next === OTHER_IMAGE ? "" : next);
          }}
        >
          {builtImages.length > 0 ? <Select.Option value="">Choose an image…</Select.Option> : null}
          {builtImages.map((name) => (
            <Select.Option key={name} value={name}>
              Built in this pipeline: {name}
            </Select.Option>
          ))}
          <Select.Option value={OTHER_IMAGE}>An external image (for example node:22)</Select.Option>
        </Select>
        {help ? <FormControl.Caption>{help}</FormControl.Caption> : null}
      </FormControl>
      {choice === OTHER_IMAGE ? (
        <FormControl>
          <FormControl.Label visuallyHidden>
            {label} (external image name with tag)
          </FormControl.Label>
          <TextInput
            block
            monospace
            value={value}
            placeholder="node:22"
            spellCheck={false}
            onChange={(event) => {
              onChange(event.target.value);
            }}
          />
        </FormControl>
      ) : null}
    </div>
  );
}

function StringListField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string[];
  onChange: (value: string[]) => void;
}) {
  return (
    <Group legend={label}>
      {value.map((item, index) => (
        <div key={index} className={styles.inlineRow}>
          <FormControl>
            <FormControl.Label visuallyHidden>
              {label} {index + 1}
            </FormControl.Label>
            <TextInput
              block
              monospace
              value={item}
              onChange={(event) => {
                onChange(value.map((v, i) => (i === index ? event.target.value : v)));
              }}
            />
          </FormControl>
          <RemoveButton
            label={`Remove ${label} ${index + 1}`}
            onClick={() => {
              onChange(value.filter((_, i) => i !== index));
            }}
          />
        </div>
      ))}
      <div>
        <AddButton
          onClick={() => {
            onChange([...value, ""]);
          }}
        >
          Add to {label}
        </AddButton>
      </div>
    </Group>
  );
}

function MapField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: { key: string; value: Value }[];
  onChange: (value: { key: string; value: Value }[]) => void;
}) {
  return (
    <Group legend={label}>
      {value.map((entry, index) => (
        <div key={index} className={styles.inlineRow}>
          <FormControl>
            <FormControl.Label visuallyHidden>
              {label} name {index + 1}
            </FormControl.Label>
            <TextInput
              block
              monospace
              value={entry.key}
              placeholder="name"
              onChange={(event) => {
                onChange(
                  value.map((e, i) => (i === index ? { ...e, key: event.target.value } : e)),
                );
              }}
            />
          </FormControl>
          <FormControl>
            <FormControl.Label visuallyHidden>
              {label} value {index + 1}
            </FormControl.Label>
            <TextInput
              block
              monospace
              value={textOf(entry.value)}
              placeholder="value"
              onChange={(event) => {
                onChange(
                  value.map((e, i) => (i === index ? { ...e, value: event.target.value } : e)),
                );
              }}
            />
          </FormControl>
          <RemoveButton
            label={`Remove ${label} ${index + 1}`}
            onClick={() => {
              onChange(value.filter((_, i) => i !== index));
            }}
          />
        </div>
      ))}
      <div>
        <AddButton
          onClick={() => {
            onChange([...value, { key: "", value: "" }]);
          }}
        >
          Add to {label}
        </AddButton>
      </div>
    </Group>
  );
}

/** One form control generated from a catalogue argument definition. */
export function ArgField({
  def,
  value,
  onChange,
}: {
  def: ArgDef;
  value: Value | undefined;
  onChange: (value: Value | undefined) => void;
}) {
  const label = def.name;
  const help = def.description || undefined;
  const caption = help ? (
    <FormControl.Caption>
      {help}
      {def.default !== null ? ` Default: ${String(def.default)}.` : ""}
    </FormControl.Caption>
  ) : null;

  if (def.enum) {
    return (
      <div className={styles.field}>
        <FormControl>
          <FormControl.Label>{label}</FormControl.Label>
          <Select
            block
            value={textOf(value) || (def.default !== null ? String(def.default) : "")}
            onChange={(event) => {
              onChange(event.target.value === "" ? undefined : event.target.value);
            }}
          >
            {def.required ? null : <Select.Option value="">Not set</Select.Option>}
            {def.enum.map((option) => (
              <Select.Option key={option} value={option}>
                {option}
              </Select.Option>
            ))}
          </Select>
          {caption}
        </FormControl>
      </div>
    );
  }
  if (def.type === "int") {
    return (
      <div className={styles.field}>
        <FormControl required={def.required}>
          <FormControl.Label>{label}</FormControl.Label>
          <TextInput
            block
            type="number"
            value={typeof value === "number" ? String(value) : ""}
            {...(def.min !== null ? { min: def.min } : {})}
            {...(def.max !== null ? { max: def.max } : {})}
            placeholder={def.default !== null ? String(def.default) : ""}
            onChange={(event) => {
              onChange(event.target.value === "" ? undefined : Number(event.target.value));
            }}
          />
          {caption}
        </FormControl>
      </div>
    );
  }
  if (def.type === "bool") {
    return (
      <div className={styles.field}>
        <FormControl>
          <Checkbox
            checked={value === true}
            onChange={(event) => {
              onChange(event.target.checked);
            }}
          />
          <FormControl.Label>{label}</FormControl.Label>
          {caption}
        </FormControl>
      </div>
    );
  }
  if (def.type === "list") {
    const items = Array.isArray(value) ? value.map(textOf) : [];
    return (
      <StringListField
        label={label}
        value={items}
        onChange={(next) => {
          onChange(next.length === 0 && !def.required ? undefined : next);
        }}
      />
    );
  }
  if (def.type === "map") {
    const entries =
      value !== undefined && typeof value === "object" && !Array.isArray(value) && "map" in value
        ? value.map
        : [];
    return (
      <MapField
        label={label}
        value={entries}
        onChange={(next) => {
          onChange(next.length === 0 ? undefined : { map: next });
        }}
      />
    );
  }
  if (def.role === "uses_image") {
    return (
      <ImageField
        label={label}
        value={textOf(value)}
        onChange={(next) => {
          onChange(next === "" ? undefined : next);
        }}
        {...(help ? { help } : {})}
      />
    );
  }
  return (
    <StringField
      label={label}
      value={typeof value === "string" || isTemplate(value) ? (value as StringValue) : ""}
      multiline={def.multiline}
      required={def.required}
      {...(help ? { help } : {})}
      onChange={(next) => {
        onChange(next === "" && !def.required ? undefined : next);
      }}
    />
  );
}

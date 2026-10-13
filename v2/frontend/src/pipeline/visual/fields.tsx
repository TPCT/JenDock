import { PlusIcon, TrashIcon } from "@primer/octicons-react";
import { useId } from "react";
import type { ArgDef, StringValue, Value } from "../../api/pipelineTypes";
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
  const id = useId();
  const template = isTemplate(value);
  const text = textOf(value);
  const Input = multiline ? "textarea" : "input";
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <Input
        id={id}
        className={multiline ? "input mono" : "input"}
        value={text}
        {...(multiline ? { rows: 3 } : {})}
        {...(placeholder ? { placeholder } : {})}
        aria-required={required || undefined}
        aria-describedby={help ? `${id}-help` : undefined}
        spellCheck={false}
        onChange={(event) => {
          onChange(stringValue(event.target.value, template));
        }}
      />
      {help ? (
        <p id={`${id}-help`} className="hint">
          {help}
        </p>
      ) : null}
      <label className="check template">
        <input
          type="checkbox"
          checked={template}
          onChange={(event) => {
            onChange(stringValue(text, event.target.checked));
          }}
        />
        Substitute variables such as {"${BUILD_NUMBER}"}
      </label>
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
  const selectId = useId();
  const textId = useId();
  const isBuilt = builtImages.includes(value);
  const choice = isBuilt ? value : value === "" && builtImages.length > 0 ? "" : OTHER_IMAGE;
  return (
    <div className="field">
      <label htmlFor={selectId}>{label}</label>
      <select
        id={selectId}
        className="input"
        value={choice}
        onChange={(event) => {
          const next = event.target.value;
          onChange(next === OTHER_IMAGE ? "" : next);
        }}
      >
        {builtImages.length > 0 ? <option value="">Choose an image…</option> : null}
        {builtImages.map((name) => (
          <option key={name} value={name}>
            Built in this pipeline: {name}
          </option>
        ))}
        <option value={OTHER_IMAGE}>An external image (for example node:22)</option>
      </select>
      {choice === OTHER_IMAGE ? (
        <>
          <label htmlFor={textId} className="sr-only">
            {label} (external image name with tag)
          </label>
          <input
            id={textId}
            className="input mono"
            value={value}
            placeholder="node:22"
            spellCheck={false}
            onChange={(event) => {
              onChange(event.target.value);
            }}
          />
        </>
      ) : null}
      {help ? <p className="hint">{help}</p> : null}
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
    <fieldset className="subfield">
      <legend>{label}</legend>
      {value.map((item, index) => (
        <div key={index} className="inline-row">
          <label className="sr-only" htmlFor={`${label}-${index}`}>
            {label} {index + 1}
          </label>
          <input
            id={`${label}-${index}`}
            className="input mono"
            value={item}
            onChange={(event) => {
              onChange(value.map((v, i) => (i === index ? event.target.value : v)));
            }}
          />
          <button
            type="button"
            className="btn btn-invisible btn-icon"
            aria-label={`Remove ${label} ${index + 1}`}
            onClick={() => {
              onChange(value.filter((_, i) => i !== index));
            }}
          >
            <TrashIcon size={16} />
          </button>
        </div>
      ))}
      <button
        type="button"
        className="btn"
        onClick={() => {
          onChange([...value, ""]);
        }}
      >
        <PlusIcon size={16} aria-hidden="true" />
        Add to {label}
      </button>
    </fieldset>
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
    <fieldset className="subfield">
      <legend>{label}</legend>
      {value.map((entry, index) => (
        <div key={index} className="inline-row">
          <label className="sr-only" htmlFor={`${label}-k-${index}`}>
            {label} name {index + 1}
          </label>
          <input
            id={`${label}-k-${index}`}
            className="input mono"
            value={entry.key}
            placeholder="name"
            onChange={(event) => {
              onChange(value.map((e, i) => (i === index ? { ...e, key: event.target.value } : e)));
            }}
          />
          <label className="sr-only" htmlFor={`${label}-v-${index}`}>
            {label} value {index + 1}
          </label>
          <input
            id={`${label}-v-${index}`}
            className="input mono"
            value={textOf(entry.value)}
            placeholder="value"
            onChange={(event) => {
              onChange(
                value.map((e, i) => (i === index ? { ...e, value: event.target.value } : e)),
              );
            }}
          />
          <button
            type="button"
            className="btn btn-invisible btn-icon"
            aria-label={`Remove ${label} ${index + 1}`}
            onClick={() => {
              onChange(value.filter((_, i) => i !== index));
            }}
          >
            <TrashIcon size={16} />
          </button>
        </div>
      ))}
      <button
        type="button"
        className="btn"
        onClick={() => {
          onChange([...value, { key: "", value: "" }]);
        }}
      >
        <PlusIcon size={16} aria-hidden="true" />
        Add to {label}
      </button>
    </fieldset>
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
  const id = useId();
  const label = def.name;
  const help = def.description || undefined;
  const describedBy = help ? `${id}-help` : undefined;
  const helpNode = help ? (
    <p id={`${id}-help`} className="hint">
      {help}
      {def.default !== null ? ` Default: ${String(def.default)}.` : ""}
    </p>
  ) : null;

  if (def.enum) {
    return (
      <div className="field">
        <label htmlFor={id}>{label}</label>
        <select
          id={id}
          className="input"
          value={textOf(value) || (def.default !== null ? String(def.default) : "")}
          aria-describedby={describedBy}
          onChange={(event) => {
            onChange(event.target.value === "" ? undefined : event.target.value);
          }}
        >
          {def.required ? null : <option value="">Not set</option>}
          {def.enum.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
        {helpNode}
      </div>
    );
  }
  if (def.type === "int") {
    return (
      <div className="field">
        <label htmlFor={id}>{label}</label>
        <input
          id={id}
          type="number"
          className="input"
          value={typeof value === "number" ? value : ""}
          {...(def.min !== null ? { min: def.min } : {})}
          {...(def.max !== null ? { max: def.max } : {})}
          placeholder={def.default !== null ? String(def.default) : ""}
          aria-required={def.required || undefined}
          aria-describedby={describedBy}
          onChange={(event) => {
            onChange(event.target.value === "" ? undefined : Number(event.target.value));
          }}
        />
        {helpNode}
      </div>
    );
  }
  if (def.type === "bool") {
    return (
      <div className="field">
        <label className="check">
          <input
            type="checkbox"
            checked={value === true}
            onChange={(event) => {
              onChange(event.target.checked);
            }}
          />
          {label}
        </label>
        {helpNode}
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

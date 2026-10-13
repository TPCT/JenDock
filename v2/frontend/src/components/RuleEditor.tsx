import {
  Button,
  Checkbox,
  CheckboxGroup,
  FormControl,
  Label,
  Radio,
  RadioGroup,
} from "@primer/react";
import { useState } from "react";
import { useSaveRules } from "../api/notificationHooks";
import type {
  EventInfo,
  NotificationChannel,
  RuleItem,
  RuleSet,
  Severity,
} from "../api/notificationTypes";
import type { EnvironmentName } from "../api/types";
import { Callout, errorMessage } from "./Feedback";
import box from "../styles/box.module.css";

const SEVERITY_VARIANT: Record<Severity, "secondary" | "attention" | "danger"> = {
  info: "secondary",
  warning: "attention",
  critical: "danger",
};

type Mode = "inherit" | "custom";
type Selection = Record<number, string[]>;

function initialSelection(set: RuleSet): Selection {
  const selection: Selection = {};
  for (const rule of set.rules) selection[rule.channel_id] = rule.enabled ? rule.events : [];
  return selection;
}

function buildRules(
  mode: Mode,
  scoped: boolean,
  channels: NotificationChannel[],
  selection: Selection,
) {
  if (scoped && mode === "inherit") return [];
  const rules: RuleItem[] = [];
  for (const channel of channels) {
    const events = selection[channel.id] ?? [];
    // Below the default scope an empty rule is meaningful: it mutes the channel there.
    if (events.length > 0 || scoped) rules.push({ channel_id: channel.id, events, enabled: true });
  }
  return rules;
}

/**
 * Rules of one scope: the global default (`slug` null), a project, or one environment.
 * Below the default a scope either uses the wider rules or customizes them (GitHub's "use
 * default" / "custom" pattern); a customized scope with nothing ticked sends nothing.
 */
export function RuleEditor({
  slug,
  env,
  channels,
  events,
  ruleSet,
}: {
  slug: string | null;
  env: EnvironmentName | null;
  channels: NotificationChannel[];
  events: EventInfo[];
  ruleSet: RuleSet;
}) {
  const scoped = slug !== null;
  const [mode, setMode] = useState<Mode>(ruleSet.rules.length > 0 ? "custom" : "inherit");
  const [selection, setSelection] = useState<Selection>(() => initialSelection(ruleSet));
  const save = useSaveRules(slug, env);
  const [saved, setSaved] = useState(false);
  const offered = events.filter((info) => !scoped || info.project_scoped);
  const inheritedFrom = ruleSet.inherited_from;
  const editable = !scoped || mode === "custom";

  const submit = () => {
    setSaved(false);
    save.mutate(buildRules(mode, scoped, channels, selection), {
      onSuccess: () => {
        setSaved(true);
      },
    });
  };

  if (channels.length === 0) {
    return (
      <Callout>
        Add a channel under Settings, Notifications before choosing what to send to it.
      </Callout>
    );
  }
  return (
    <div className={box.stack}>
      {scoped ? (
        <RadioGroup
          name={`mode-${slug}-${env ?? "project"}`}
          onChange={(value) => {
            setMode(value as Mode);
            setSaved(false);
          }}
        >
          <RadioGroup.Label>
            Which notifications does this {env ? "environment" : "project"} send?
          </RadioGroup.Label>
          <FormControl>
            <Radio value="inherit" checked={mode === "inherit"} />
            <FormControl.Label>
              {inheritedFrom === "project"
                ? "Use the project's rules"
                : inheritedFrom === "default"
                  ? "Use the default rules"
                  : "Use the default rules (none are set)"}
            </FormControl.Label>
            <FormControl.Caption>Nothing is customized here.</FormControl.Caption>
          </FormControl>
          <FormControl>
            <Radio value="custom" checked={mode === "custom"} />
            <FormControl.Label>Choose channels and events</FormControl.Label>
            <FormControl.Caption>
              Overrides the wider rules. Leave everything unticked to send nothing from here.
            </FormControl.Caption>
          </FormControl>
        </RadioGroup>
      ) : null}
      {editable ? (
        <div className={box.stack}>
          {channels.map((channel) => (
            <div key={channel.id} className={box.box}>
              <CheckboxGroup
                className={box.row}
                disabled={!channel.enabled}
                onChange={(selected) => {
                  setSelection((current) => ({ ...current, [channel.id]: selected }));
                  setSaved(false);
                }}
              >
                <CheckboxGroup.Label>{channel.name}</CheckboxGroup.Label>
                {channel.enabled ? null : (
                  <CheckboxGroup.Caption>
                    This channel is switched off; nothing is sent until it is enabled.
                  </CheckboxGroup.Caption>
                )}
                {offered.map((info) => (
                  <FormControl key={info.event}>
                    <Checkbox
                      value={info.event}
                      checked={(selection[channel.id] ?? []).includes(info.event)}
                    />
                    <FormControl.Label>{info.label}</FormControl.Label>
                    <FormControl.Caption>
                      <Label variant={SEVERITY_VARIANT[info.severity]}>{info.severity}</Label>{" "}
                      <code>{info.event}</code>
                    </FormControl.Caption>
                  </FormControl>
                ))}
              </CheckboxGroup>
            </div>
          ))}
        </div>
      ) : null}
      {save.isError ? (
        <Callout variant="danger" role="alert">
          {errorMessage(save.error)}
        </Callout>
      ) : null}
      {saved ? (
        <Callout variant="success" role="status">
          Notification rules saved.
        </Callout>
      ) : null}
      <div>
        <Button variant="primary" disabled={save.isPending} onClick={submit}>
          {save.isPending ? "Saving…" : "Save rules"}
        </Button>
      </div>
    </div>
  );
}

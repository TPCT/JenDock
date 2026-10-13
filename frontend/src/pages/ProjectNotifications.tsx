import { SegmentedControl } from "@primer/react";
import { useState } from "react";
import { useParams } from "react-router-dom";
import { useChannels, useNotificationPolicy, useRules } from "../api/notificationHooks";
import type { EnvironmentName } from "../api/types";
import { DeliveryHistory } from "../components/DeliveryHistory";
import { ErrorCallout, Loading } from "../components/Feedback";
import { RuleEditor } from "../components/RuleEditor";
import { SettingsSection } from "../components/SettingsSection";

type ScopeChoice = "project" | EnvironmentName;

const SCOPES: { id: ScopeChoice; label: string }[] = [
  { id: "project", label: "All environments" },
  { id: "development", label: "Development" },
  { id: "production", label: "Production" },
];

function ScopeRules({ slug, scope }: { slug: string; scope: ScopeChoice }) {
  const env = scope === "project" ? null : scope;
  const channels = useChannels();
  const policy = useNotificationPolicy();
  const rules = useRules(slug, env);
  const failed = channels.error ?? policy.error ?? rules.error;
  if (failed) return <ErrorCallout title="Could not load the rules" error={failed} />;
  if (!channels.data || !policy.data || !rules.data) return <Loading label="Loading rules" />;
  return (
    <RuleEditor
      key={scope}
      slug={slug}
      env={env}
      channels={channels.data}
      events={policy.data.events}
      ruleSet={rules.data}
    />
  );
}

/** Project settings, Notifications: which events of this project go to which channel. */
export function ProjectNotificationsPage() {
  const { slug = "" } = useParams();
  const [scope, setScope] = useState<ScopeChoice>("project");
  return (
    <>
      <SettingsSection
        title="Notifications"
        intro="Choose which channels hear about this project and for which events. A project, or one of its environments, can follow the default rules or override them. Channels are managed under Settings, Notifications."
        actions={
          <SegmentedControl
            aria-label="Rules apply to"
            onChange={(index) => {
              setScope(SCOPES[index]?.id ?? "project");
            }}
          >
            {SCOPES.map((item) => (
              <SegmentedControl.Button key={item.id} selected={scope === item.id}>
                {item.label}
              </SegmentedControl.Button>
            ))}
          </SegmentedControl>
        }
      >
        <ScopeRules slug={slug} scope={scope} />
      </SettingsSection>
      <SettingsSection
        title="Delivery history"
        intro="Recent notifications about this project, including failures and retries."
      >
        <DeliveryHistory slug={slug} />
      </SettingsSection>
    </>
  );
}

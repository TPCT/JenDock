import { Details, Heading } from "@primer/react";
import { useMemo, type ReactNode } from "react";
import type { Catalog, Model } from "../../api/pipelineTypes";
import styles from "../pipeline.module.css";
import { CallList } from "./CallList";
import { EditorEnvContext, builtImagesOf } from "./context";
import { AgentPicker, EnvEditor, PostEditor } from "./Sections";
import { StageList } from "./StageList";

function Panel({
  id,
  title,
  hint,
  children,
}: {
  id: string;
  title: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <section className={styles.panel} aria-labelledby={id}>
      <Heading as="h3" id={id} className={styles.panelTitle}>
        {title}
      </Heading>
      {hint ? <p className={styles.hint}>{hint}</p> : null}
      {children}
    </section>
  );
}

function Collapsible({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Details className={`${styles.panel} ${styles.more}`}>
      <Details.Summary className={styles.summary}>
        <Heading as="h3" className={styles.inlineTitle}>
          {title}
        </Heading>
      </Details.Summary>
      {children}
    </Details>
  );
}

export function VisualEditor({
  model,
  onChange,
  catalog,
  secrets,
  branch,
  environment,
  readOnly,
}: {
  model: Model;
  onChange: (model: Model) => void;
  catalog: Catalog;
  secrets: string[];
  branch: string;
  environment: string;
  readOnly: boolean;
}) {
  const env = useMemo(
    () => ({
      catalog,
      secrets,
      branch,
      environment,
      builtImages: builtImagesOf(model, catalog.steps),
    }),
    [catalog, secrets, branch, environment, model],
  );
  const set = (patch: Partial<Model>) => {
    onChange({ ...model, ...patch });
  };

  return (
    <EditorEnvContext value={env}>
      <fieldset className={styles.visual} disabled={readOnly}>
        <legend className={styles.legendHidden}>Visual pipeline editor</legend>

        <Panel id="ve-agent" title="Where the pipeline runs">
          <AgentPicker
            label="Default agent"
            allowInherit={false}
            value={model.agent}
            onChange={(agent) => {
              set({ agent });
            }}
          />
        </Panel>

        <Panel
          id="ve-stages"
          title="Stages"
          hint="Stages run one after another, from top to bottom."
        >
          <StageList
            stages={model.stages}
            scope="stages"
            onChange={(stages) => {
              set({ stages });
            }}
          />
        </Panel>

        <Collapsible title="Variables">
          <EnvEditor
            scope="pipeline-env"
            value={model.environment}
            onChange={(environment) => {
              set({ environment });
            }}
          />
        </Collapsible>

        <Collapsible title="Options">
          <CallList
            calls={model.options}
            defs={catalog.options}
            scope="options"
            typeLabel="Option"
            addLabel="Add option"
            emptyText="No options."
            onChange={(options) => {
              set({ options });
            }}
          />
        </Collapsible>

        <Collapsible title="Parameters">
          <CallList
            calls={model.parameters}
            defs={catalog.parameters}
            scope="parameters"
            typeLabel="Parameter type"
            addLabel="Add parameter"
            emptyText="No parameters."
            onChange={(parameters) => {
              set({ parameters });
            }}
          />
        </Collapsible>

        <Panel id="ve-post" title="After the whole run">
          <PostEditor
            scope="pipeline"
            value={model.post}
            onChange={(post) => {
              set({ post });
            }}
          />
        </Panel>
      </fieldset>
    </EditorEnvContext>
  );
}

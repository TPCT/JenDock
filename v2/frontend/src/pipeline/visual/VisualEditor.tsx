import { useMemo } from "react";
import type { Catalog, Model } from "../../api/pipelineTypes";
import { CallList } from "./CallList";
import { EditorEnvContext, builtImagesOf } from "./context";
import { AgentPicker, EnvEditor, PostEditor } from "./Sections";
import { StageList } from "./StageList";

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
      <fieldset className="visual" disabled={readOnly}>
        <legend className="sr-only">Visual pipeline editor</legend>

        <section className="panel" aria-labelledby="ve-agent">
          <h3 id="ve-agent">Where the pipeline runs</h3>
          <AgentPicker
            label="Default agent"
            allowInherit={false}
            value={model.agent}
            onChange={(agent) => {
              set({ agent });
            }}
          />
        </section>

        <section className="panel" aria-labelledby="ve-stages">
          <h3 id="ve-stages">Stages</h3>
          <p className="hint">Stages run one after another, from top to bottom.</p>
          <StageList
            stages={model.stages}
            scope="stages"
            onChange={(stages) => {
              set({ stages });
            }}
          />
        </section>

        <details className="panel more">
          <summary>
            <h3 className="inline">Variables</h3>
          </summary>
          <EnvEditor
            scope="pipeline-env"
            value={model.environment}
            onChange={(environment) => {
              set({ environment });
            }}
          />
        </details>

        <details className="panel more">
          <summary>
            <h3 className="inline">Options</h3>
          </summary>
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
        </details>

        <details className="panel more">
          <summary>
            <h3 className="inline">Parameters</h3>
          </summary>
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
        </details>

        <section className="panel" aria-labelledby="ve-post">
          <h3 id="ve-post">After the whole run</h3>
          <PostEditor
            scope="pipeline"
            value={model.post}
            onChange={(post) => {
              set({ post });
            }}
          />
        </section>
      </fieldset>
    </EditorEnvContext>
  );
}

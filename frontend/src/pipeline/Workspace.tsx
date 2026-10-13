import { CheckIcon } from "@primer/octicons-react";
import { Button, ConfirmationDialog } from "@primer/react";
import { UnderlinePanels } from "@primer/react/experimental";
import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { useBlocker } from "react-router-dom";
import { isApiError } from "../api/client";
import { pipelineApi, pipelineKeys, pipelinePath, useCatalog, useSecretNames } from "../api/hooks";
import type { Model, PipelineDocument } from "../api/pipelineTypes";
import type { Diagnostic, EnvironmentName, Project } from "../api/types";
import { api } from "../api/client";
import { Callout, ErrorCallout, Loading, errorMessage } from "../components/Feedback";
import { RelativeTime } from "../components/Time";
import { CodeEditor, type CodeEditorHandle } from "./CodeEditor";
import { DiagnosticsList } from "./DiagnosticsList";
import { History } from "./History";
import { STARTER_MODEL } from "./starter";
import { VisualEditor } from "./visual/VisualEditor";
import styles from "./pipeline.module.css";

type Tab = "visual" | "text";
const VALIDATE_DELAY_MS = 400;
const RENDER_DELAY_MS = 250;

function useDebounced(callback: () => void, delay: number, deps: readonly unknown[]): void {
  const latest = useRef(callback);
  useEffect(() => {
    latest.current = callback;
  });
  useEffect(() => {
    const timer = window.setTimeout(() => {
      latest.current();
    }, delay);
    return () => {
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}

export function Workspace({
  project,
  env,
  doc,
}: {
  project: Project;
  env: EnvironmentName;
  doc: PipelineDocument;
}) {
  const slug = project.slug;
  const environment = project.environments.find((item) => item.name === env);
  const branch = environment?.branch ?? "main";
  const context = { project: slug, environment: env };
  const readOnly = doc.source === "repository";
  const client = useQueryClient();
  const catalog = useCatalog();
  const secrets = useSecretNames(slug, env);

  const [saved, setSaved] = useState({ version: doc.version, text: doc.jenkinsfile });
  const [text, setText] = useState(doc.jenkinsfile);
  const [baseVersion, setBaseVersion] = useState(doc.version);
  const [tab, setTab] = useState<Tab>("visual");
  const [model, setModel] = useState<Model | null>(
    doc.source === "none" && doc.jenkinsfile === "" ? STARTER_MODEL : null,
  );
  const [visual, setVisual] = useState<"loading" | "ready" | "blocked">(
    doc.source === "none" && doc.jenkinsfile === "" ? "ready" : "loading",
  );
  const [diagnostics, setDiagnostics] = useState<Diagnostic[]>([]);
  const [rendering, setRendering] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<unknown>(null);
  const [justSaved, setJustSaved] = useState(false);
  const [conflict, setConflict] = useState<PipelineDocument | null>(null);
  const [showTheirs, setShowTheirs] = useState(false);

  const editor = useRef<CodeEditorHandle>(null);
  const pendingJump = useRef<{ line: number; column: number } | null>(null);
  const sequence = useRef(0);
  const dirty = text !== saved.text;
  const starter = doc.source === "none" && doc.jenkinsfile === "";

  // ---- guard against losing edits -----------------------------------------------------
  const blocker = useBlocker(dirty && !saving);
  useEffect(() => {
    if (!dirty) return;
    const handler = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener("beforeunload", handler);
    return () => {
      window.removeEventListener("beforeunload", handler);
    };
  }, [dirty]);

  // ---- server round trips, each guarded against out-of-order answers ---------------------
  const render = useCallback(
    async (next: Model) => {
      const mine = ++sequence.current;
      setRendering(true);
      try {
        const result = await pipelineApi.render(next, context);
        if (mine !== sequence.current) return;
        if (result.jenkinsfile !== null) setText(result.jenkinsfile);
        setDiagnostics(result.diagnostics);
      } catch (error) {
        if (mine === sequence.current) setSaveError(error);
      } finally {
        if (mine === sequence.current) setRendering(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [slug, env],
  );

  const parse = useCallback(
    async (source: string) => {
      const mine = ++sequence.current;
      setVisual("loading");
      try {
        const result = await pipelineApi.parse(source, context);
        if (mine !== sequence.current) return;
        setDiagnostics(result.diagnostics);
        setModel(result.model);
        setVisual(result.model === null ? "blocked" : "ready");
      } catch (error) {
        if (mine !== sequence.current) return;
        setSaveError(error);
        setVisual("blocked");
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [slug, env],
  );

  // A new environment starts from a server-rendered starter pipeline.
  useEffect(() => {
    const timer = window.setTimeout(() => {
      if (starter) void render(STARTER_MODEL);
      else void parse(doc.jenkinsfile);
    }, 0);
    return () => {
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Text tab: validate on a pause in typing.
  const validatedText = useRef(text);
  useDebounced(
    () => {
      if (tab !== "text" || text === validatedText.current) return;
      validatedText.current = text;
      const mine = ++sequence.current;
      pipelineApi
        .validate(text, context)
        .then((result) => {
          if (mine === sequence.current) setDiagnostics(result.diagnostics);
        })
        .catch(() => undefined);
    },
    VALIDATE_DELAY_MS,
    [text, tab],
  );

  // Visual tab: every edit goes through the server renderer.
  const pendingModel = useRef<Model | null>(null);
  const [renderTick, setRenderTick] = useState(0);
  useDebounced(
    () => {
      const next = pendingModel.current;
      if (next === null) return;
      pendingModel.current = null;
      void render(next);
    },
    RENDER_DELAY_MS,
    [renderTick],
  );

  const onVisualChange = (next: Model) => {
    setModel(next);
    pendingModel.current = next;
    setRendering(true);
    setSaveError(null);
    setJustSaved(false);
    setRenderTick((tick) => tick + 1);
  };

  const onTextChange = (next: string) => {
    setText(next);
    setSaveError(null);
    setJustSaved(false);
  };

  const switchTab = (next: Tab) => {
    if (next === tab) return;
    setTab(next);
    const waiting = pendingModel.current;
    if (next === "text" && waiting !== null) {
      pendingModel.current = null;
      void render(waiting);
    }
    if (next === "visual") void parse(text);
    else validatedText.current = text;
  };

  // Jump requested from the visual tab's problem list: open Text, then move the cursor.
  const jump = (line: number, column: number) => {
    if (tab === "text") {
      editor.current?.jumpTo(line, column);
      return;
    }
    pendingJump.current = { line, column };
    switchTab("text");
  };
  useEffect(() => {
    if (tab === "text" && pendingJump.current) {
      const target = pendingJump.current;
      pendingJump.current = null;
      editor.current?.jumpTo(target.line, target.column);
    }
  });

  // ---- saving ---------------------------------------------------------------------------
  const save = async () => {
    setSaving(true);
    setSaveError(null);
    try {
      const result = await pipelineApi.save(slug, env, text, baseVersion);
      setSaved({ version: result.version, text: result.jenkinsfile });
      setBaseVersion(result.version);
      setDiagnostics([]);
      setJustSaved(true);
      await client.invalidateQueries({ queryKey: pipelineKeys.versions(slug, env) });
    } catch (error) {
      if (isApiError(error) && error.status === 409) {
        try {
          setConflict(await api.get<PipelineDocument>(pipelinePath(slug, env)));
        } catch (inner) {
          setSaveError(inner);
        }
      } else if (isApiError(error) && error.status === 422) {
        setDiagnostics(error.diagnostics);
        setSaveError(error);
      } else {
        setSaveError(error);
      }
    } finally {
      setSaving(false);
    }
  };

  const loadTheirs = () => {
    if (!conflict) return;
    setText(conflict.jenkinsfile);
    setSaved({ version: conflict.version, text: conflict.jenkinsfile });
    setBaseVersion(conflict.version);
    setConflict(null);
    setShowTheirs(false);
    setDiagnostics([]);
    if (tab === "visual") void parse(conflict.jenkinsfile);
    void client.invalidateQueries({ queryKey: pipelineKeys.versions(slug, env) });
  };
  const keepMine = () => {
    if (!conflict) return;
    setBaseVersion(conflict.version);
    setConflict(null);
    setShowTheirs(false);
    void client.invalidateQueries({ queryKey: pipelineKeys.versions(slug, env) });
  };

  const restore = (source: string) => {
    setText(source);
    setJustSaved(false);
    if (tab === "visual") void parse(source);
  };

  const errors = diagnostics.filter((item) => item.severity === "error");
  const textReadOnly = readOnly || rendering;

  return (
    <div className={styles.workspace}>
      {readOnly ? (
        <Callout
          role="status"
          title={`This pipeline is defined by the Jenkinsfile in ${project.repository}@${branch}; edit it in the repository.`}
        >
          Runs use the file from the repository at the commit they build. You can look at it here
          and at the history of pipelines saved in this platform earlier.
        </Callout>
      ) : null}
      {starter ? (
        <Callout role="status" title="This environment has no pipeline yet.">
          Below is a starter you can change. Nothing is stored until you press Save pipeline.
        </Callout>
      ) : null}
      {doc.version > 0 && !readOnly ? (
        <p className="muted">
          Version {saved.version}
          {doc.updated_at ? (
            <>
              , saved <RelativeTime iso={doc.updated_at} />
              {doc.updated_by ? ` by ${doc.updated_by}` : ""}
            </>
          ) : null}
        </p>
      ) : null}

      <div className={styles.toolbar}>
        <span role="status" className={styles.saveState}>
          {rendering ? "Applying changes…" : null}
          {!rendering && dirty ? "Unsaved changes" : null}
          {!rendering && !dirty && justSaved ? (
            <span className={styles.saved}>
              <CheckIcon size={14} aria-hidden="true" /> Saved
            </span>
          ) : null}
        </span>
        {!readOnly ? (
          <Button
            variant="primary"
            leadingVisual={CheckIcon}
            disabled={!dirty || rendering || saving}
            onClick={() => void save()}
          >
            {saving ? "Saving…" : "Save pipeline"}
          </Button>
        ) : null}
      </div>

      {saveError && !(isApiError(saveError) && saveError.status === 422) ? (
        <Callout variant="danger" role="alert">
          {errorMessage(saveError)}
        </Callout>
      ) : null}
      {isApiError(saveError) && saveError.status === 422 ? (
        <Callout variant="danger" role="alert">
          Not saved: {saveError.message}. Fix the problems below.
        </Callout>
      ) : null}

      {conflict ? (
        <>
          <Callout
            variant="warning"
            role="alert"
            title="Someone else saved this pipeline first"
            actions={
              <>
                <Button onClick={loadTheirs}>
                  Load version {conflict.version} and discard my changes
                </Button>
                <Button onClick={keepMine}>
                  Keep my changes on top of version {conflict.version}
                </Button>
                <Button
                  aria-expanded={showTheirs}
                  onClick={() => {
                    setShowTheirs((value) => !value);
                  }}
                >
                  {showTheirs ? "Hide their version" : "Look at their version"}
                </Button>
              </>
            }
          >
            You started from version {baseVersion}, but version {conflict.version} was saved
            {conflict.updated_by ? ` by ${conflict.updated_by}` : ""} in the meantime. Nothing was
            overwritten.
          </Callout>
          {showTheirs ? (
            <pre className={styles.versionText} tabIndex={0} aria-label="Their Jenkinsfile">
              {conflict.jenkinsfile}
            </pre>
          ) : null}
        </>
      ) : null}

      <UnderlinePanels
        aria-label="Editor mode"
        value={tab}
        onChange={({ value }) => {
          switchTab(value === "text" ? "text" : "visual");
        }}
      >
        <UnderlinePanels.Tab value="visual">Visual</UnderlinePanels.Tab>
        <UnderlinePanels.Tab value="text">Text</UnderlinePanels.Tab>
        <UnderlinePanels.Panel value="visual" className={styles.tabpanel}>
          {tab === "visual" ? (
            <>
              {visual === "loading" || catalog.isPending || secrets.isPending ? (
                <Loading label="Preparing the visual editor" />
              ) : null}
              {catalog.isError ? (
                <ErrorCallout title="Could not load the step catalogue" error={catalog.error} />
              ) : null}
              {visual === "blocked" ? (
                <Callout
                  variant="danger"
                  role="alert"
                  title="The visual editor cannot open this pipeline yet"
                  actions={
                    <Button
                      onClick={() => {
                        switchTab("text");
                      }}
                    >
                      Open the Text tab
                    </Button>
                  }
                >
                  The text has errors, so it cannot be shown as stages. Fix them in the Text tab;
                  the visual editor stays read-only until then.
                </Callout>
              ) : null}
              {visual === "ready" && model && catalog.data && secrets.data ? (
                <VisualEditor
                  model={model}
                  onChange={onVisualChange}
                  catalog={catalog.data}
                  secrets={secrets.data}
                  branch={branch}
                  environment={env}
                  readOnly={readOnly}
                />
              ) : null}
            </>
          ) : null}
        </UnderlinePanels.Panel>
        <UnderlinePanels.Panel value="text" className={styles.tabpanel}>
          {tab === "text" ? (
            <CodeEditor
              handle={editor}
              label="Jenkinsfile"
              value={text}
              readOnly={textReadOnly}
              diagnostics={diagnostics}
              onChange={onTextChange}
            />
          ) : null}
        </UnderlinePanels.Panel>
      </UnderlinePanels>

      <DiagnosticsList
        diagnostics={diagnostics}
        onJump={jump}
        {...(errors.length > 0 && tab === "visual"
          ? { jumpHint: "Select a problem to see where it is in the text." }
          : {})}
      />

      <History slug={slug} env={env} canRestore={!readOnly} onRestore={restore} />

      {blocker.state === "blocked" ? (
        <ConfirmationDialog
          title="Leave without saving?"
          confirmButtonContent="Leave and discard changes"
          cancelButtonContent="Stay and keep editing"
          confirmButtonType="danger"
          onClose={(gesture) => {
            if (gesture === "confirm") blocker.proceed();
            else blocker.reset();
          }}
        >
          You have unsaved changes to this pipeline. If you leave now they are lost.
        </ConfirmationDialog>
      ) : null}
    </div>
  );
}

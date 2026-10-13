import { AlertIcon, CheckIcon, LockIcon } from "@primer/octicons-react";
import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { useBlocker } from "react-router-dom";
import { isApiError } from "../api/client";
import { pipelineApi, pipelineKeys, pipelinePath, useCatalog, useSecretNames } from "../api/hooks";
import type { Model, PipelineDocument } from "../api/pipelineTypes";
import type { Diagnostic, EnvironmentName, Project } from "../api/types";
import { api } from "../api/client";
import { Dialog } from "../components/Dialog";
import { ErrorCallout, Loading, errorMessage } from "../components/Feedback";
import { RelativeTime } from "../components/Time";
import { CodeEditor, type CodeEditorHandle } from "./CodeEditor";
import { DiagnosticsList } from "./DiagnosticsList";
import { History } from "./History";
import { STARTER_MODEL } from "./starter";
import { VisualEditor } from "./visual/VisualEditor";
import "./pipeline.css";

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

  const onTabKey = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const next: Tab = tab === "visual" ? "text" : "visual";
    switchTab(next);
    document.getElementById(`tab-${next}`)?.focus();
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
    <div className="workspace">
      {readOnly ? (
        <div className="callout" role="status">
          <LockIcon size={16} aria-hidden="true" />
          <div className="callout-body">
            <strong>
              This pipeline is defined by the Jenkinsfile in {project.repository}@{branch}; edit it
              in the repository.
            </strong>
            <span>
              Runs use the file from the repository at the commit they build. You can look at it
              here and at the history of pipelines saved in this platform earlier.
            </span>
          </div>
        </div>
      ) : null}
      {starter ? (
        <div className="callout" role="status">
          <div className="callout-body">
            <strong>This environment has no pipeline yet.</strong>
            <span>
              Below is a starter you can change. Nothing is stored until you press Save pipeline.
            </span>
          </div>
        </div>
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

      <div className="toolbar">
        <div role="tablist" aria-label="Editor mode" className="tabs">
          {(["visual", "text"] as const).map((name) => (
            <button
              key={name}
              id={`tab-${name}`}
              type="button"
              role="tab"
              aria-selected={tab === name}
              aria-controls={`panel-${name}`}
              tabIndex={tab === name ? 0 : -1}
              className="tab"
              onKeyDown={onTabKey}
              onClick={() => {
                switchTab(name);
              }}
            >
              {name === "visual" ? "Visual" : "Text"}
            </button>
          ))}
        </div>
        <div className="row-actions">
          <span role="status" className="muted save-state">
            {rendering ? "Applying changes…" : null}
            {!rendering && dirty ? "Unsaved changes" : null}
            {!rendering && !dirty && justSaved ? (
              <span className="saved">
                <CheckIcon size={14} aria-hidden="true" /> Saved
              </span>
            ) : null}
          </span>
          {!readOnly ? (
            <button
              type="button"
              className="btn btn-primary"
              disabled={!dirty || rendering || saving}
              onClick={() => void save()}
            >
              <CheckIcon size={16} aria-hidden="true" />
              {saving ? "Saving…" : "Save pipeline"}
            </button>
          ) : null}
        </div>
      </div>

      {saveError && !(isApiError(saveError) && saveError.status === 422) ? (
        <div className="callout callout-danger" role="alert">
          <AlertIcon size={16} aria-hidden="true" />
          <div className="callout-body">{errorMessage(saveError)}</div>
        </div>
      ) : null}
      {isApiError(saveError) && saveError.status === 422 ? (
        <div className="callout callout-danger" role="alert">
          <AlertIcon size={16} aria-hidden="true" />
          <div className="callout-body">
            Not saved: {saveError.message}. Fix the problems below.
          </div>
        </div>
      ) : null}

      {conflict ? (
        <div className="callout callout-attention" role="alert">
          <AlertIcon size={16} aria-hidden="true" />
          <div className="callout-body">
            <strong>Someone else saved this pipeline first</strong>
            <span>
              You started from version {baseVersion}, but version {conflict.version} was saved
              {conflict.updated_by ? ` by ${conflict.updated_by}` : ""} in the meantime. Nothing was
              overwritten.
            </span>
            <div className="callout-actions">
              <button type="button" className="btn" onClick={loadTheirs}>
                Load version {conflict.version} and discard my changes
              </button>
              <button type="button" className="btn" onClick={keepMine}>
                Keep my changes on top of version {conflict.version}
              </button>
              <button
                type="button"
                className="btn"
                aria-expanded={showTheirs}
                onClick={() => {
                  setShowTheirs((value) => !value);
                }}
              >
                {showTheirs ? "Hide their version" : "Look at their version"}
              </button>
            </div>
            {showTheirs ? (
              <pre className="version-text" tabIndex={0} aria-label="Their Jenkinsfile">
                {conflict.jenkinsfile}
              </pre>
            ) : null}
          </div>
        </div>
      ) : null}

      <div
        id="panel-visual"
        role="tabpanel"
        aria-labelledby="tab-visual"
        hidden={tab !== "visual"}
        className="tabpanel"
      >
        {tab === "visual" ? (
          <>
            {visual === "loading" || catalog.isPending || secrets.isPending ? (
              <Loading label="Preparing the visual editor" />
            ) : null}
            {catalog.isError ? (
              <ErrorCallout title="Could not load the step catalogue" error={catalog.error} />
            ) : null}
            {visual === "blocked" ? (
              <div className="callout callout-danger" role="alert">
                <AlertIcon size={16} aria-hidden="true" />
                <div className="callout-body">
                  <strong>The visual editor cannot open this pipeline yet</strong>
                  <span>
                    The text has errors, so it cannot be shown as stages. Fix them in the Text tab;
                    the visual editor stays read-only until then.
                  </span>
                  <div className="callout-actions">
                    <button
                      type="button"
                      className="btn"
                      onClick={() => {
                        switchTab("text");
                      }}
                    >
                      Open the Text tab
                    </button>
                  </div>
                </div>
              </div>
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
      </div>

      <div
        id="panel-text"
        role="tabpanel"
        aria-labelledby="tab-text"
        hidden={tab !== "text"}
        className="tabpanel"
      >
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
      </div>

      <DiagnosticsList
        diagnostics={diagnostics}
        onJump={jump}
        {...(errors.length > 0 && tab === "visual"
          ? { jumpHint: "Select a problem to see where it is in the text." }
          : {})}
      />

      <History slug={slug} env={env} canRestore={!readOnly} onRestore={restore} />

      {blocker.state === "blocked" ? (
        <Dialog
          title="Leave without saving?"
          onClose={() => {
            blocker.reset();
          }}
        >
          <p>You have unsaved changes to this pipeline. If you leave now they are lost.</p>
          <div className="dialog-actions">
            <button
              type="button"
              className="btn"
              onClick={() => {
                blocker.reset();
              }}
            >
              Stay and keep editing
            </button>
            <button
              type="button"
              className="btn btn-danger-solid"
              onClick={() => {
                blocker.proceed();
              }}
            >
              Leave and discard changes
            </button>
          </div>
        </Dialog>
      ) : null}
    </div>
  );
}

import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import {
  HighlightStyle,
  StreamLanguage,
  bracketMatching,
  indentOnInput,
  syntaxHighlighting,
} from "@codemirror/language";
import { groovy } from "@codemirror/legacy-modes/mode/groovy";
import { mariaDB, mySQL, pgSQL } from "@codemirror/legacy-modes/mode/sql";
import { lintGutter, setDiagnostics, type Diagnostic as CmDiagnostic } from "@codemirror/lint";
import { Compartment, EditorState } from "@codemirror/state";
import {
  EditorView,
  highlightActiveLine,
  highlightActiveLineGutter,
  keymap,
  lineNumbers,
} from "@codemirror/view";
import { tags } from "@lezer/highlight";
import { useEffect, useImperativeHandle, useRef, type Ref } from "react";
import type { Diagnostic } from "../api/types";
import styles from "./pipeline.module.css";

export type EditorLanguage = "groovy" | "postgresql" | "mysql" | "mariadb";
const LANGUAGES = { groovy, postgresql: pgSQL, mysql: mySQL, mariadb: mariaDB } as const;

export interface CodeEditorHandle {
  /** Moves the cursor to a 1-based line/column, scrolls to it and focuses the editor. */
  jumpTo: (line: number, column: number) => void;
}

/**
 * The text editor wears GitHub's code view: Primer's own syntax colours (the
 * `--color-prettylights-syntax-*` variables GitHub uses for rendered code), code font, gutter and
 * selection colours. Variables inherit through the shadow root, so both colour modes work.
 */
const highlight = HighlightStyle.define([
  {
    tag: [tags.keyword, tags.controlKeyword, tags.definitionKeyword, tags.operatorKeyword],
    color: "var(--color-prettylights-syntax-keyword)",
  },
  {
    tag: [tags.string, tags.special(tags.string)],
    color: "var(--color-prettylights-syntax-string)",
  },
  {
    tag: [tags.number, tags.bool, tags.atom],
    color: "var(--color-prettylights-syntax-constant)",
  },
  {
    tag: [tags.comment, tags.lineComment, tags.blockComment],
    color: "var(--color-prettylights-syntax-comment)",
  },
  {
    tag: [tags.variableName, tags.propertyName],
    color: "var(--color-prettylights-syntax-variable)",
  },
  { tag: [tags.className, tags.typeName], color: "var(--color-prettylights-syntax-entity)" },
  { tag: tags.operator, color: "var(--fgColor-muted)" },
]);

const theme = EditorView.theme({
  "&": {
    color: "var(--fgColor-default)",
    backgroundColor: "var(--bgColor-default)",
    border: "var(--borderWidth-thin) solid var(--borderColor-default)",
    borderRadius: "var(--borderRadius-medium)",
    fontSize: "var(--text-codeBlock-size)",
    height: "100%",
  },
  "&.cm-focused": {
    outline: "2px solid var(--focus-outlineColor)",
    outlineOffset: "-1px",
  },
  ".cm-scroller": {
    fontFamily: "var(--fontStack-monospace)",
    overflow: "auto",
    lineHeight: "1.5",
  },
  ".cm-gutters": {
    backgroundColor: "var(--bgColor-default)",
    color: "var(--fgColor-muted)",
    border: "none",
  },
  ".cm-activeLine": { backgroundColor: "var(--bgColor-muted)" },
  ".cm-activeLineGutter": {
    backgroundColor: "var(--bgColor-muted)",
    color: "var(--fgColor-default)",
  },
  ".cm-cursor": { borderLeftColor: "var(--fgColor-default)" },
  ".cm-selectionBackground, &.cm-focused .cm-selectionBackground": {
    backgroundColor: "var(--bgColor-accent-muted)",
  },
  ".cm-diagnostic-error": { borderLeft: "3px solid var(--borderColor-danger-emphasis)" },
  ".cm-diagnostic-warning": { borderLeft: "3px solid var(--borderColor-attention-emphasis)" },
  ".cm-lintRange-error": { textDecoration: "underline wavy var(--fgColor-danger)" },
  ".cm-lintRange-warning": { textDecoration: "underline wavy var(--fgColor-attention)" },
});

/** Registry used by tests, which cannot reach into the shadow root. */
export const editorViews: EditorView[] = [];

function toCm(state: EditorState, diagnostics: Diagnostic[]): CmDiagnostic[] {
  const lines = state.doc.lines;
  return diagnostics.flatMap((item) => {
    if (item.line < 1 || item.line > lines) return [];
    const line = state.doc.line(item.line);
    const from = Math.min(line.from + Math.max(0, item.column - 1), line.to);
    const word = /^[\w.$]+/.exec(state.sliceDoc(from, line.to));
    const to = Math.min(line.to, from + Math.max(1, word ? word[0].length : 1));
    return [
      {
        from,
        to: Math.max(to, from),
        severity: item.severity,
        message: item.message,
      },
    ];
  });
}

/**
 * CodeMirror 6 inside a shadow root: CodeMirror injects its styles as `<style>` tags, which the
 * API's CSP (`default-src 'self'`) blocks; in a shadow root it uses constructed stylesheets.
 * Theme colours are CSS variables, which inherit through the shadow boundary.
 */
export function CodeEditor({
  value,
  onChange,
  readOnly,
  diagnostics,
  label,
  handle,
  language = "groovy",
  onRun,
}: {
  value: string;
  onChange: (text: string) => void;
  readOnly: boolean;
  diagnostics: Diagnostic[];
  label: string;
  handle?: Ref<CodeEditorHandle>;
  /** Highlighting dialect; the SQL console uses the engine's. */
  language?: EditorLanguage;
  /** Ctrl/Cmd+Enter. */
  onRun?: () => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const runRef = useRef(onRun);
  useEffect(() => {
    runRef.current = onRun;
  });
  const editable = useRef(new Compartment());
  const changeRef = useRef(onChange);
  useEffect(() => {
    changeRef.current = onChange;
  });

  useEffect(() => {
    const element = host.current;
    if (!element) return;
    const shadow = element.shadowRoot ?? element.attachShadow({ mode: "open" });
    const parent = document.createElement("div");
    parent.className = "cm-host";
    shadow.append(parent);
    const editor = new EditorView({
      root: shadow,
      parent,
      state: EditorState.create({
        doc: value,
        extensions: [
          lineNumbers(),
          highlightActiveLineGutter(),
          highlightActiveLine(),
          history(),
          indentOnInput(),
          bracketMatching(),
          lintGutter(),
          StreamLanguage.define(LANGUAGES[language]),
          syntaxHighlighting(highlight),
          keymap.of([
            {
              key: "Mod-Enter",
              run: () => {
                runRef.current?.();
                return runRef.current !== undefined;
              },
            },
            ...defaultKeymap,
            ...historyKeymap,
            indentWithTab,
          ]),
          theme,
          EditorView.lineWrapping,
          EditorView.contentAttributes.of({
            "aria-label": label,
            "aria-multiline": "true",
          }),
          editable.current.of([
            EditorState.readOnly.of(readOnly),
            EditorView.editable.of(!readOnly),
          ]),
          EditorView.updateListener.of((update) => {
            if (update.docChanged) changeRef.current(update.state.doc.toString());
          }),
        ],
      }),
    });
    view.current = editor;
    editorViews.push(editor);
    return () => {
      editorViews.splice(editorViews.indexOf(editor), 1);
      editor.destroy();
      view.current = null;
      parent.remove();
    };
    // The editor is created once; value/readOnly/diagnostics are synced by the effects below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const editor = view.current;
    if (!editor) return;
    const current = editor.state.doc.toString();
    if (current !== value) {
      editor.dispatch({ changes: { from: 0, to: current.length, insert: value } });
    }
  }, [value]);

  useEffect(() => {
    view.current?.dispatch({
      effects: editable.current.reconfigure([
        EditorState.readOnly.of(readOnly),
        EditorView.editable.of(!readOnly),
      ]),
    });
  }, [readOnly]);

  useEffect(() => {
    const editor = view.current;
    if (!editor) return;
    editor.dispatch(setDiagnostics(editor.state, toCm(editor.state, diagnostics)));
  }, [diagnostics, value]);

  useImperativeHandle(handle, () => ({
    jumpTo: (line, column) => {
      const editor = view.current;
      if (!editor || line < 1 || line > editor.state.doc.lines) return;
      const target = editor.state.doc.line(line);
      const position = Math.min(target.from + Math.max(0, column - 1), target.to);
      editor.dispatch({
        selection: { anchor: position },
        effects: EditorView.scrollIntoView(position, { y: "center" }),
      });
      editor.focus();
    },
  }));

  return <div ref={host} className={styles.codeEditor} data-readonly={readOnly} />;
}

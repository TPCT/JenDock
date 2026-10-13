import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import {
  HighlightStyle,
  StreamLanguage,
  bracketMatching,
  indentOnInput,
  syntaxHighlighting,
} from "@codemirror/language";
import { groovy } from "@codemirror/legacy-modes/mode/groovy";
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

export interface CodeEditorHandle {
  /** Moves the cursor to a 1-based line/column, scrolls to it and focuses the editor. */
  jumpTo: (line: number, column: number) => void;
}

const highlight = HighlightStyle.define([
  { tag: [tags.keyword, tags.controlKeyword, tags.definitionKeyword], color: "var(--syn-keyword)" },
  { tag: [tags.string, tags.special(tags.string)], color: "var(--syn-string)" },
  { tag: [tags.number, tags.bool, tags.atom], color: "var(--syn-number)" },
  { tag: [tags.comment, tags.lineComment, tags.blockComment], color: "var(--syn-comment)" },
  { tag: [tags.variableName, tags.propertyName], color: "var(--syn-name)" },
  { tag: tags.operator, color: "var(--fg-muted)" },
]);

const theme = EditorView.theme({
  "&": {
    color: "var(--log-fg)",
    backgroundColor: "var(--log-bg)",
    border: "1px solid var(--border)",
    borderRadius: "6px",
    fontSize: "0.8125rem",
    height: "100%",
  },
  "&.cm-focused": { outline: "2px solid var(--focus)", outlineOffset: "2px" },
  ".cm-scroller": { fontFamily: "var(--font-mono)", overflow: "auto", lineHeight: "1.5" },
  ".cm-gutters": {
    backgroundColor: "var(--bg-subtle)",
    color: "var(--fg-muted)",
    borderRight: "1px solid var(--border)",
  },
  ".cm-activeLine": { backgroundColor: "var(--bg-hover)" },
  ".cm-activeLineGutter": { backgroundColor: "var(--bg-hover)" },
  ".cm-cursor": { borderLeftColor: "var(--log-fg)" },
  ".cm-selectionBackground, &.cm-focused .cm-selectionBackground": {
    backgroundColor: "var(--accent-subtle)",
  },
  ".cm-diagnostic-error": { borderLeft: "3px solid var(--danger)" },
  ".cm-diagnostic-warning": { borderLeft: "3px solid var(--attention)" },
  ".cm-lintRange-error": { textDecoration: "underline wavy var(--danger)" },
  ".cm-lintRange-warning": { textDecoration: "underline wavy var(--attention)" },
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
}: {
  value: string;
  onChange: (text: string) => void;
  readOnly: boolean;
  diagnostics: Diagnostic[];
  label: string;
  handle?: Ref<CodeEditorHandle>;
}) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
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
          StreamLanguage.define(groovy),
          syntaxHighlighting(highlight),
          keymap.of([...defaultKeymap, ...historyKeymap, indentWithTab]),
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

  return <div ref={host} className="code-editor" data-readonly={readOnly} />;
}

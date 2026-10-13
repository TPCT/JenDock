import { AlertIcon, StopIcon } from "@primer/octicons-react";
import type { Diagnostic } from "../api/types";

/** Problems with position; each entry is a button that jumps to that spot in the text. */
export function DiagnosticsList({
  diagnostics,
  onJump,
  jumpHint,
}: {
  diagnostics: Diagnostic[];
  onJump?: (line: number, column: number) => void;
  jumpHint?: string;
}) {
  if (diagnostics.length === 0) return null;
  const errors = diagnostics.filter((d) => d.severity === "error").length;
  return (
    <section className="diagnostics" aria-label="Problems">
      <h3 className="diagnostics-title">
        {errors > 0 ? `${errors} ${errors === 1 ? "error" : "errors"}` : "Warnings"}
        {diagnostics.length > errors ? `, ${diagnostics.length - errors} warning(s)` : ""}
      </h3>
      {jumpHint ? <p className="hint">{jumpHint}</p> : null}
      <ul>
        {diagnostics.map((item, index) => {
          const Icon = item.severity === "error" ? StopIcon : AlertIcon;
          const where = `${item.line}:${item.column}`;
          return (
            <li key={`${where}-${index}`} className={`diag diag-${item.severity}`}>
              <Icon size={16} aria-hidden="true" />
              {onJump ? (
                <button
                  type="button"
                  className="diag-jump"
                  onClick={() => {
                    onJump(item.line, item.column);
                  }}
                >
                  <span className="mono">{where}</span>
                  <span className="sr-only">{item.severity}: </span>
                  {item.message}
                </button>
              ) : (
                <span>
                  <span className="mono">{where}</span> {item.message}
                </span>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

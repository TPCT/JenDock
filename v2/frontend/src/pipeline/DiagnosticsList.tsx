import { AlertIcon, StopIcon } from "@primer/octicons-react";
import { Button, Heading, VisuallyHidden } from "@primer/react";
import { FlashMessage } from "../components/Feedback";
import type { Diagnostic } from "../api/types";
import styles from "./pipeline.module.css";

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
    <FlashMessage variant={errors > 0 ? "danger" : "warning"} as="section" aria-label="Problems">
      <Heading as="h3" variant="small">
        {errors > 0 ? `${errors} ${errors === 1 ? "error" : "errors"}` : "Warnings"}
        {diagnostics.length > errors ? `, ${diagnostics.length - errors} warning(s)` : ""}
      </Heading>
      {jumpHint ? <p>{jumpHint}</p> : null}
      <ul className={styles.diagnostics}>
        {diagnostics.map((item, index) => {
          const Icon = item.severity === "error" ? StopIcon : AlertIcon;
          const where = `${item.line}:${item.column}`;
          return (
            <li
              key={`${where}-${index}`}
              className={`${styles.diag} ${item.severity === "error" ? styles.diagError : styles.diagWarning}`}
            >
              <Icon size={16} aria-hidden="true" />
              {onJump ? (
                <Button
                  variant="link"
                  onClick={() => {
                    onJump(item.line, item.column);
                  }}
                >
                  <span className={styles.mono}>{where}</span>
                  <VisuallyHidden> {item.severity}: </VisuallyHidden> {item.message}
                </Button>
              ) : (
                <span>
                  <span className={styles.mono}>{where}</span> {item.message}
                </span>
              )}
            </li>
          );
        })}
      </ul>
    </FlashMessage>
  );
}

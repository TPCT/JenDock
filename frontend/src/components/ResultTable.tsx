import { Button, Dialog } from "@primer/react";
import { useState } from "react";
import type { Cell, ResultColumn } from "../api/dbTypes";
import { formatBytes } from "../lib/format";
import box from "../styles/box.module.css";
import styles from "./ResultTable.module.css";

const PREVIEW_CHARS = 80;

/** 89504e47 -> "89 50 4e 47", eight bytes per line. */
export function hexLines(hex: string): string {
  const pairs = hex.match(/.{1,2}/g) ?? [];
  const lines: string[] = [];
  for (let i = 0; i < pairs.length; i += 8) lines.push(pairs.slice(i, i + 8).join(" "));
  return lines.join("\n");
}

type Open =
  | { kind: "text"; column: string; text: string; limit: number }
  | { kind: "hex"; column: string; hex: string; bytes: number };

/**
 * Query and row results as a table. Long text and binary values arrive cut by the API (4,096
 * bytes of text, 64 bytes of binary); the dialog shows exactly what the API sent and says so.
 */
export function ResultTable({
  columns,
  rows,
  label,
}: {
  columns: ResultColumn[];
  rows: Cell[][];
  label: string;
}) {
  const [open, setOpen] = useState<Open | null>(null);
  return (
    <>
      <div className={styles.scroll}>
        <table className={styles.table} aria-label={label}>
          <thead>
            <tr>
              {columns.map((column) => (
                <th key={column.name} scope="col">
                  {column.name}
                  <span className={styles.type}>{column.type}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={rowIndex}>
                {row.map((cell, index) => {
                  const column = columns[index]?.name ?? String(index);
                  if (cell === null) {
                    return (
                      <td key={index}>
                        <span className={styles.null}>NULL</span>
                      </td>
                    );
                  }
                  if (typeof cell === "object" && "truncated" in cell) {
                    return (
                      <td key={index}>
                        <span className={styles.text}>{cell.text.slice(0, PREVIEW_CHARS)}…</span>{" "}
                        <Button
                          size="small"
                          variant="invisible"
                          aria-label={`Show full value of ${column}`}
                          onClick={() => {
                            setOpen({ kind: "text", column, text: cell.text, limit: cell.limit });
                          }}
                        >
                          Show full value
                        </Button>
                      </td>
                    );
                  }
                  if (typeof cell === "object") {
                    return (
                      <td key={index}>
                        <code className={styles.hex}>{cell.hex.slice(0, 16)}…</code>{" "}
                        <span className="muted">binary, {formatBytes(cell.bytes)}</span>{" "}
                        <Button
                          size="small"
                          variant="invisible"
                          aria-label={`Show hex of ${column}`}
                          onClick={() => {
                            setOpen({ kind: "hex", column, hex: cell.hex, bytes: cell.bytes });
                          }}
                        >
                          Show hex
                        </Button>
                      </td>
                    );
                  }
                  return (
                    <td key={index}>
                      <span className={styles.text}>{String(cell)}</span>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {open ? (
        <Dialog
          title={
            open.kind === "text" ? `Value of ${open.column}` : `Binary value of ${open.column}`
          }
          width="large"
          onClose={() => {
            setOpen(null);
          }}
          footerButtons={[
            {
              buttonType: "primary",
              content: "Close",
              onClick: () => {
                setOpen(null);
              },
            },
          ]}
        >
          <div className={box.stack}>
            {open.kind === "text" ? (
              <>
                <p className="muted">
                  The API returns at most the first {open.limit.toLocaleString()} bytes of a long
                  value, so this is the beginning, not necessarily all of it.
                </p>
                <pre className={styles.full} tabIndex={0} aria-label="Full value">
                  {open.text}
                </pre>
              </>
            ) : (
              <>
                <p className="muted">
                  The first {String(open.hex.length / 2)} of {open.bytes.toLocaleString()} bytes, in
                  hexadecimal.
                </p>
                <pre className={styles.full} tabIndex={0} aria-label="Hex dump">
                  {hexLines(open.hex)}
                </pre>
              </>
            )}
          </div>
        </Dialog>
      ) : null}
    </>
  );
}

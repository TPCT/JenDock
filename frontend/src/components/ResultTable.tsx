import { CheckIcon, CopyIcon, KeyIcon, TableIcon } from "@primer/octicons-react";
import { Dialog, IconButton } from "@primer/react";
import { Blankslate } from "@primer/react/experimental";
import { useEffect, useMemo, useState } from "react";
import type { Cell, ResultColumn } from "../api/dbTypes";
import { formatBytes } from "../lib/format";
import box from "../styles/box.module.css";
import styles from "./ResultTable.module.css";

const PREVIEW_CHARS = 80;
export function hexLines(hex: string): string {
  const pairs = hex.match(/.{1,2}/g) ?? [];
  const lines: string[] = [];
  for (let i = 0; i < pairs.length; i += 8) lines.push(pairs.slice(i, i + 8).join(" "));
  return lines.join("\n");
}

type Open = { column: ResultColumn; cell: Exclude<Cell, null> };

export function ResultTable({
  columns,
  rows,
  label,
  primaryKey = [],
  emptyTitle = "No rows",
  emptyDescription = "This table is empty.",
  bare = false,
}: {
  columns: ResultColumn[];
  rows: Cell[][];
  label: string;
  primaryKey?: string[];
  emptyTitle?: string;
  emptyDescription?: string;
  /** Drop the grid's own border when it sits inside a bordered box. */
  bare?: boolean;
}) {
  const [open, setOpen] = useState<Open | null>(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => {
      setCopied(false);
    }, 2000);
    return () => {
      window.clearTimeout(timer);
    };
  }, [copied]);

  const numericColumns = useMemo(
    () =>
      columns.map((column, index) => {
        const hasNumbers = rows.some((row) => typeof row[index] === "number");
        return (
          /^((tiny|small|medium|big)?int(eger)?\d*|(big|small)?serial\d*|numeric|decimal|real|double|float\d*|money)\b/i.test(
            column.type,
          ) ||
          (hasNumbers &&
            rows
              .filter((row) => row[index] !== null)
              .every((row) => typeof row[index] === "number"))
        );
      }),
    [columns, rows],
  );

  const display = (cell: Exclude<Cell, null>) => {
    if (typeof cell === "object" && "truncated" in cell)
      return `${cell.text.slice(0, PREVIEW_CHARS)}…`;
    if (typeof cell === "object") return `0x${cell.hex.slice(0, 8)}… (${formatBytes(cell.bytes)})`;
    return String(cell);
  };
  const fullValue = (cell: Exclude<Cell, null>) =>
    typeof cell === "object" && "truncated" in cell
      ? cell.text
      : typeof cell === "object"
        ? cell.hex
        : String(cell);

  return (
    <>
      <div className={`${styles.gridScroll} ${bare ? styles.bare : ""}`} data-testid="grid-scroll">
        <table className={styles.table} aria-label={label}>
          <thead>
            <tr>
              {columns.map((column, index) => (
                <th
                  key={column.name}
                  scope="col"
                  className={`${styles.stickyHeader} ${index === 0 ? styles.stickyColumn : ""} ${numericColumns[index] ? styles.numeric : ""}`}
                  title={column.type}
                >
                  <span className={styles.headingName}>
                    {primaryKey.includes(column.name) ? (
                      <KeyIcon size={12} aria-label="Primary key" />
                    ) : null}
                    {column.name}
                  </span>
                  {column.type ? <span className={styles.type}>{column.type}</span> : null}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={Math.max(columns.length, 1)} className={styles.emptyCell}>
                  <Blankslate narrow>
                    <Blankslate.Visual>
                      <TableIcon />
                    </Blankslate.Visual>
                    <Blankslate.Heading>{emptyTitle}</Blankslate.Heading>
                    {emptyDescription ? (
                      <Blankslate.Description>{emptyDescription}</Blankslate.Description>
                    ) : null}
                  </Blankslate>
                </td>
              </tr>
            ) : (
              rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {columns.map((column, index) => {
                    const cell = row[index] ?? null;
                    const first = index === 0;
                    const numericCell = numericColumns[index];
                    return (
                      <td
                        key={index}
                        className={`${first ? styles.stickyColumn : ""} ${numericCell ? styles.numeric : ""}`}
                      >
                        {cell === null ? (
                          <span className={styles.null}>NULL</span>
                        ) : (
                          <button
                            type="button"
                            className={styles.cellButton}
                            aria-haspopup="dialog"
                            onClick={() => {
                              setCopied(false);
                              setOpen({ column, cell });
                            }}
                          >
                            {display(cell)}
                          </button>
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      {open ? (
        <Dialog
          title={open.column.name}
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
            <p className="muted">{open.column.type}</p>
            {typeof open.cell === "object" && "truncated" in open.cell ? (
              <p className="muted">
                The API returns at most the first {open.cell.limit.toLocaleString()} bytes of a long
                value, so this is the beginning, not necessarily all of it.
              </p>
            ) : null}
            {typeof open.cell === "object" && "binary" in open.cell ? (
              <p className="muted">
                The first {String(open.cell.hex.length / 2)} of {open.cell.bytes.toLocaleString()}{" "}
                bytes, in hexadecimal.
              </p>
            ) : null}
            <pre
              className={styles.full}
              tabIndex={0}
              aria-label={
                typeof open.cell === "object" && "binary" in open.cell ? "Hex dump" : "Full value"
              }
            >
              {typeof open.cell === "object" && "binary" in open.cell
                ? hexLines(open.cell.hex)
                : fullValue(open.cell)}
            </pre>
            <IconButton
              size="small"
              variant="invisible"
              icon={copied ? CheckIcon : CopyIcon}
              aria-label={copied ? "Copied" : "Copy value"}
              onClick={() => {
                void navigator.clipboard
                  .writeText(fullValue(open.cell))
                  .then(() => {
                    setCopied(true);
                  })
                  .catch(() => {
                    setCopied(false);
                  });
              }}
            />
          </div>
        </Dialog>
      ) : null}
    </>
  );
}

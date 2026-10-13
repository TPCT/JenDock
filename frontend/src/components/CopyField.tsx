import { CheckIcon, CopyIcon } from "@primer/octicons-react";
import { Button, VisuallyHidden } from "@primer/react";
import { useEffect, useState } from "react";
import styles from "./CopyField.module.css";

/** A read-only value in a code box with a copy button that confirms visibly and audibly. */
export function CopyField({
  label,
  value,
  multiline = false,
}: {
  label: string;
  value: string;
  multiline?: boolean;
}) {
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

  const copy = () => {
    navigator.clipboard
      .writeText(value)
      .then(() => {
        setCopied(true);
      })
      .catch(() => {
        setCopied(false);
      });
  };

  return (
    <div className={styles.field}>
      <span className={styles.label}>{label}</span>
      <div className={styles.row}>
        <code
          className={multiline ? `${styles.value} ${styles.multi}` : styles.value}
          tabIndex={0}
          aria-label={label}
        >
          {value}
        </code>
        <Button
          size="small"
          leadingVisual={copied ? CheckIcon : CopyIcon}
          onClick={copy}
          aria-label={`Copy ${label}`}
        >
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
      <VisuallyHidden role="status">{copied ? `${label} copied` : ""}</VisuallyHidden>
    </div>
  );
}

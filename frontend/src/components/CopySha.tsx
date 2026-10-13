import { CheckIcon, CopyIcon } from "@primer/octicons-react";
import { IconButton, VisuallyHidden } from "@primer/react";
import { useEffect, useState } from "react";
import { shortSha } from "../lib/format";
import styles from "./CopySha.module.css";

/** Commit SHA, always left-to-right, with a copy button that confirms audibly and visibly. */
export function CopySha({ sha }: { sha: string }) {
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
      .writeText(sha)
      .then(() => {
        setCopied(true);
      })
      .catch(() => {
        setCopied(false);
      });
  };

  return (
    <span className={styles.sha}>
      <code className="ltr" title={sha}>
        {shortSha(sha)}
      </code>
      <IconButton
        size="small"
        variant="invisible"
        icon={copied ? CheckIcon : CopyIcon}
        onClick={copy}
        aria-label={`Copy full commit SHA ${sha}`}
        tooltipDirection="s"
      />
      <VisuallyHidden role="status">{copied ? "Commit SHA copied" : ""}</VisuallyHidden>
    </span>
  );
}

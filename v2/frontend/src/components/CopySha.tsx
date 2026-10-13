import { CheckIcon, CopyIcon } from "@primer/octicons-react";
import { useEffect, useState } from "react";
import { shortSha } from "../lib/format";
import "./CopySha.css";

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
    <span className="sha">
      <code className="ltr" title={sha}>
        {shortSha(sha)}
      </code>
      <button
        type="button"
        className="btn btn-invisible btn-icon sha-copy"
        onClick={copy}
        aria-label={`Copy full commit SHA ${sha}`}
      >
        {copied ? <CheckIcon size={14} /> : <CopyIcon size={14} />}
      </button>
      <span role="status" className="sr-only">
        {copied ? "Commit SHA copied" : ""}
      </span>
    </span>
  );
}

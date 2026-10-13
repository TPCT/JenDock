import { AlertIcon, GitCommitIcon, PlayIcon, SearchIcon } from "@primer/octicons-react";
import { useState, type SyntheticEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useBranchHead, useStartRun } from "../api/hooks";
import type { EnvironmentName } from "../api/types";
import { capitalize, shortSha } from "../lib/format";
import { Dialog } from "./Dialog";
import { errorMessage } from "./Feedback";
import { RelativeTime } from "./Time";

/** Two steps so nobody deploys a commit they have not seen: look up the branch head, then run it. */
export function StartRunDialog({
  slug,
  env,
  defaultBranch,
  onClose,
}: {
  slug: string;
  env: EnvironmentName;
  defaultBranch: string;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const [branch, setBranch] = useState(defaultBranch);
  const [lookup, setLookup] = useState<string | null>(null);
  const head = useBranchHead(slug, env, lookup ?? "");
  const start = useStartRun(slug, env);

  const preview = lookup !== null && lookup === branch.trim() ? head.data : undefined;

  const onLookup = (event: SyntheticEvent) => {
    event.preventDefault();
    if (branch.trim() !== "") setLookup(branch.trim());
  };

  const onRun = () => {
    if (!preview) return;
    start.mutate(
      { branch: preview.branch, commit_sha: preview.commit_sha },
      {
        onSuccess: (run) => {
          onClose();
          void navigate(`/runs/${run.id}`);
        },
      },
    );
  };

  return (
    <Dialog title={`Run pipeline on ${capitalize(env)}`} onClose={onClose}>
      <p className="muted">Pick a branch. You will see the exact commit before anything starts.</p>
      <form className="stack" onSubmit={onLookup} noValidate>
        <div className="field">
          <label htmlFor="run-branch">Branch</label>
          <input
            id="run-branch"
            className="input mono"
            value={branch}
            onChange={(event) => {
              setBranch(event.target.value);
            }}
            spellCheck={false}
            autoCapitalize="none"
          />
          <p className="hint">
            The default for {capitalize(env)} is {defaultBranch}.
          </p>
        </div>
        <div>
          <button type="submit" className="btn" disabled={branch.trim() === "" || head.isFetching}>
            <SearchIcon size={16} aria-hidden="true" />
            {head.isFetching ? "Looking up…" : "Look up latest commit"}
          </button>
        </div>
      </form>
      {lookup !== null && head.isError && lookup === branch.trim() ? (
        <div className="callout callout-danger" role="alert">
          <AlertIcon size={16} aria-hidden="true" />
          <div className="callout-body">{errorMessage(head.error)}</div>
        </div>
      ) : null}
      {preview ? (
        <div className="callout" role="status">
          <GitCommitIcon size={16} aria-hidden="true" />
          <div className="callout-body">
            <strong className="ltr">{shortSha(preview.commit_sha)}</strong>
            <span>{preview.message}</span>
            <span className="muted">
              {preview.author} · <RelativeTime iso={preview.committed_at} />
            </span>
          </div>
        </div>
      ) : null}
      {start.isError ? (
        <div className="callout callout-danger" role="alert">
          <AlertIcon size={16} aria-hidden="true" />
          <div className="callout-body">{errorMessage(start.error)}</div>
        </div>
      ) : null}
      <div className="dialog-actions">
        <button type="button" className="btn" onClick={onClose}>
          Cancel
        </button>
        <button
          type="button"
          className="btn btn-primary"
          disabled={!preview || start.isPending}
          onClick={onRun}
        >
          <PlayIcon size={16} aria-hidden="true" />
          {start.isPending ? "Starting…" : "Run pipeline"}
        </button>
      </div>
    </Dialog>
  );
}

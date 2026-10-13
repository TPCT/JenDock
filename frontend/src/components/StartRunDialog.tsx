import { GitCommitIcon, SearchIcon } from "@primer/octicons-react";
import { Button, Dialog, FormControl, TextInput } from "@primer/react";
import { useState, type SyntheticEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useBranchHead, useStartRun } from "../api/hooks";
import type { EnvironmentName } from "../api/types";
import { capitalize, shortSha } from "../lib/format";
import box from "../styles/box.module.css";
import { Callout, errorMessage } from "./Feedback";
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
    <Dialog
      title={`Run pipeline on ${capitalize(env)}`}
      subtitle="Pick a branch. You will see the exact commit before anything starts."
      width="large"
      onClose={onClose}
      footerButtons={[
        { buttonType: "default", content: "Cancel", onClick: onClose },
        {
          buttonType: "primary",
          content: start.isPending ? "Starting…" : "Run pipeline",
          disabled: !preview || start.isPending,
          onClick: onRun,
        },
      ]}
    >
      <div className={box.stack}>
        <form className={box.stack} onSubmit={onLookup} noValidate>
          <FormControl>
            <FormControl.Label>Branch</FormControl.Label>
            <TextInput
              block
              monospace
              value={branch}
              onChange={(event) => {
                setBranch(event.target.value);
              }}
              spellCheck={false}
              autoCapitalize="none"
            />
            <FormControl.Caption>
              The default for {capitalize(env)} is {defaultBranch}.
            </FormControl.Caption>
          </FormControl>
          <div>
            <Button
              type="submit"
              leadingVisual={SearchIcon}
              disabled={branch.trim() === "" || head.isFetching}
            >
              {head.isFetching ? "Looking up…" : "Look up latest commit"}
            </Button>
          </div>
        </form>
        {lookup !== null && head.isError && lookup === branch.trim() ? (
          <Callout variant="danger" role="alert">
            {errorMessage(head.error)}
          </Callout>
        ) : null}
        {preview ? (
          <div role="status" className={box.box}>
            <div className={box.row}>
              <div className={box.cluster}>
                <GitCommitIcon size={16} aria-hidden="true" />
                <strong className="ltr">{shortSha(preview.commit_sha)}</strong>
                <span>{preview.message}</span>
              </div>
              <span className="muted">
                {preview.author} · <RelativeTime iso={preview.committed_at} />
              </span>
            </div>
          </div>
        ) : null}
        {start.isError ? (
          <Callout variant="danger" role="alert">
            {errorMessage(start.error)}
          </Callout>
        ) : null}
      </div>
    </Dialog>
  );
}

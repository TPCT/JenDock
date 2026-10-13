import { Heading, Link as PrimerLink } from "@primer/react";
import type { GitTransport } from "../api/adminTypes";
import box from "../styles/box.module.css";
import { CopyField } from "./CopyField";
import styles from "./GithubSetup.module.css";

export const WEBHOOK_PATH = "/api/github/webhook";
const SECRET_COMMAND = "docker compose exec worker cat /run/shiftcodes-keys/github_webhook_secret";
const PUBLIC_KEY_COMMAND = "ssh-keygen -y -f deploy_key";

function ghLink(repository: string, path: string): string {
  return `https://github.com/${repository}/${path}`;
}

/**
 * Step-by-step instructions for connecting a repository: the deploy key (private repositories)
 * and the push webhook. The platform never shows the key or the webhook secret itself (the
 * worker alone holds them), so the instructions say where the operator finds them.
 */
export function GithubSetup({
  repository,
  transport,
}: {
  repository: string;
  transport: GitTransport;
}) {
  const repo = repository.trim() === "" ? "OWNER/NAME" : repository.trim();
  const payloadUrl = `${window.location.origin}${WEBHOOK_PATH}`;
  return (
    <div className={box.stack}>
      <section className={styles.part} aria-labelledby="gh-key">
        <Heading as="h3" id="gh-key" className={styles.title}>
          1. Let Shiftcodes read the code
        </Heading>
        {transport === "ssh" ? (
          <>
            <p>
              Private repositories are cloned with a <strong>deploy key</strong>: a read-only SSH
              key that only works for this repository. Shiftcodes already holds the private half;
              you add the public half to GitHub.
            </p>
            <ol className={styles.steps}>
              <li>
                Get the public key. On the server that runs Shiftcodes, in the folder where the key{" "}
                <code>deploy_key</code> was created, run:
                <CopyField label="Command that prints the public key" value={PUBLIC_KEY_COMMAND} />
              </li>
              <li>
                Open{" "}
                <PrimerLink
                  href={ghLink(repo, "settings/keys/new")}
                  target="_blank"
                  rel="noreferrer"
                >
                  {repo} → Settings → Deploy keys → Add deploy key
                </PrimerLink>
                .
              </li>
              <li>
                Give it any title (for example <em>Shiftcodes</em>), paste the key, and leave{" "}
                <strong>Allow write access</strong> unchecked. Read-only is enough.
              </li>
            </ol>
            <p className="muted">
              One deploy key can only be used on one repository. If GitHub says the key is already
              in use, the administrator has to create a separate key for this repository.
            </p>
          </>
        ) : (
          <p>
            Public repositories are cloned over https and need no key. Choose the SSH transport
            instead if the repository is private.
          </p>
        )}
      </section>

      <section className={styles.part} aria-labelledby="gh-hook">
        <Heading as="h3" id="gh-hook" className={styles.title}>
          2. Tell Shiftcodes about every push (webhook)
        </Heading>
        <p>
          The webhook makes GitHub call Shiftcodes after each push, so the matching environment
          deploys automatically. Without it you can still start runs by hand.
        </p>
        <ol className={styles.steps}>
          <li>
            Open{" "}
            <PrimerLink href={ghLink(repo, "settings/hooks/new")} target="_blank" rel="noreferrer">
              {repo} → Settings → Webhooks → Add webhook
            </PrimerLink>
            .
          </li>
          <li>
            Fill the form exactly like this:
            <div className={styles.fields}>
              <CopyField label="Payload URL" value={payloadUrl} />
              <CopyField label="Content type" value="application/json" />
              <CopyField label="Secret command" value={SECRET_COMMAND} />
            </div>
            <p className="muted">
              The <strong>Secret</strong> is the text stored in <code>github_webhook_secret</code>{" "}
              on the server. Shiftcodes never displays it; the command above prints it. Paste its
              output into the Secret box.
            </p>
          </li>
          <li>
            Under <strong>Which events would you like to trigger this webhook?</strong> choose{" "}
            <em>Just the push event</em>, keep <strong>Active</strong> ticked and press{" "}
            <strong>Add webhook</strong>.
          </li>
          <li>
            GitHub sends a test (<code>ping</code>). On the webhook&apos;s{" "}
            <em>Recent Deliveries</em> tab a green tick means Shiftcodes answered.
          </li>
        </ol>
      </section>
    </div>
  );
}

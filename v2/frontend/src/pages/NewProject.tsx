import { Button, FormControl, Heading, Radio, RadioGroup, TextInput } from "@primer/react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useCreateProject, useTemplates } from "../api/adminHooks";
import type { GitTransport } from "../api/adminTypes";
import { isApiError } from "../api/client";
import { Callout, ErrorCallout, Loading, errorMessage } from "../components/Feedback";
import { GithubSetup } from "../components/GithubSetup";
import { PageHead } from "../components/PageHead";
import box from "../styles/box.module.css";
import styles from "./NewProject.module.css";

const STEPS = ["Repository", "Connect GitHub", "Branches", "Pipeline", "Review"] as const;
const REPOSITORY = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\/[A-Za-z0-9._-]{1,100}$/;
const SLUG = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/;
const NO_TEMPLATE = "__none__";

export function slugFrom(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
}

interface Draft {
  repository: string;
  name: string;
  slug: string;
  transport: GitTransport;
  development: string;
  production: string;
  template: string;
}

type Errors = Partial<Record<keyof Draft, string>>;

function validate(step: number, draft: Draft): Errors {
  const errors: Errors = {};
  if (step === 0) {
    if (!REPOSITORY.test(draft.repository.trim())) {
      errors.repository = "Write it as owner/name, for example acme/shop-api.";
    }
    if (draft.name.trim() === "") errors.name = "Give the project a name.";
    if (!SLUG.test(draft.slug)) {
      errors.slug =
        "Use lowercase letters, digits and dashes, starting and ending with a letter or digit.";
    }
  }
  if (step === 2) {
    if (draft.development.trim() === "") errors.development = "Enter a branch name.";
    if (draft.production.trim() === "") errors.production = "Enter a branch name.";
    if (
      !errors.development &&
      !errors.production &&
      draft.development.trim() === draft.production.trim()
    ) {
      errors.production = "Development and Production must track different branches.";
    }
  }
  return errors;
}

/** Project creation wizard for people who have never connected a repository to a deploy tool. */
export function NewProjectPage() {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [slugTouched, setSlugTouched] = useState(false);
  const [errors, setErrors] = useState<Errors>({});
  const [draft, setDraft] = useState<Draft>({
    repository: "",
    name: "",
    slug: "",
    transport: "ssh",
    development: "develop",
    production: "main",
    template: NO_TEMPLATE,
  });
  const templates = useTemplates();
  const create = useCreateProject();
  const set = (patch: Partial<Draft>) => {
    setDraft((current) => ({ ...current, ...patch }));
  };
  const chosen = templates.data?.find((item) => item.name === draft.template);

  const next = () => {
    const found = validate(step, draft);
    setErrors(found);
    if (Object.keys(found).length === 0) setStep((current) => current + 1);
  };

  const submit = () => {
    create.mutate(
      {
        body: {
          slug: draft.slug,
          name: draft.name.trim(),
          repository: draft.repository.trim(),
          development_branch: draft.development.trim(),
          production_branch: draft.production.trim(),
          git_transport: draft.transport,
        },
        jenkinsfile: chosen?.jenkinsfile ?? null,
      },
      {
        onSuccess: (project) => {
          void navigate(`/projects/${encodeURIComponent(project.slug)}/settings/webhook`);
        },
      },
    );
  };

  const slugConflict = create.isError && isApiError(create.error) && create.error.status === 409;

  return (
    <>
      <PageHead
        crumbs={[{ label: "Projects", to: "/projects" }, { label: "New project" }]}
        title="Create a project"
        description="A project connects one GitHub repository to a Development and a Production environment. It takes five short steps."
      />
      <div className={`${box.section} ${styles.wizard}`}>
        <ol className={styles.steps} aria-label="Steps">
          {STEPS.map((label, index) => (
            <li
              key={label}
              className={index === step ? `${styles.step} ${styles.current}` : styles.step}
              aria-current={index === step ? "step" : undefined}
              data-done={index < step ? "true" : undefined}
            >
              <span className={styles.number}>{index + 1}</span>
              {label}
            </li>
          ))}
        </ol>

        <div className={`${box.box} ${box.row} ${box.stack}`}>
          {step === 0 ? (
            <>
              <Heading as="h2" variant="medium">
                Which repository?
              </Heading>
              <FormControl required>
                <FormControl.Label>GitHub repository</FormControl.Label>
                <TextInput
                  block
                  monospace
                  value={draft.repository}
                  placeholder="acme/shop-api"
                  autoCapitalize="none"
                  spellCheck={false}
                  validationStatus={errors.repository ? "error" : undefined}
                  onChange={(event) => {
                    const repository = event.target.value;
                    const guess = repository.split("/")[1] ?? "";
                    set({
                      repository,
                      ...(draft.name === "" ||
                      draft.name === prettify(draft.repository.split("/")[1] ?? "")
                        ? { name: prettify(guess) }
                        : {}),
                      ...(slugTouched ? {} : { slug: slugFrom(guess) }),
                    });
                  }}
                />
                <FormControl.Caption>
                  The owner and the repository name from its GitHub address: github.com/
                  <strong>owner/name</strong>.
                </FormControl.Caption>
                {errors.repository ? (
                  <FormControl.Validation variant="error">
                    {errors.repository}
                  </FormControl.Validation>
                ) : null}
              </FormControl>
              <FormControl required>
                <FormControl.Label>Project name</FormControl.Label>
                <TextInput
                  block
                  value={draft.name}
                  validationStatus={errors.name ? "error" : undefined}
                  onChange={(event) => {
                    set({
                      name: event.target.value,
                      ...(slugTouched ? {} : { slug: slugFrom(event.target.value) }),
                    });
                  }}
                />
                <FormControl.Caption>
                  How the project is called in lists and headings.
                </FormControl.Caption>
                {errors.name ? (
                  <FormControl.Validation variant="error">{errors.name}</FormControl.Validation>
                ) : null}
              </FormControl>
              <FormControl required>
                <FormControl.Label>Short name (slug)</FormControl.Label>
                <TextInput
                  block
                  monospace
                  value={draft.slug}
                  validationStatus={errors.slug ? "error" : undefined}
                  onChange={(event) => {
                    setSlugTouched(true);
                    set({ slug: event.target.value });
                  }}
                />
                <FormControl.Caption>
                  Used in web addresses and container names. It cannot be changed later.
                </FormControl.Caption>
                {errors.slug ? (
                  <FormControl.Validation variant="error">{errors.slug}</FormControl.Validation>
                ) : null}
              </FormControl>
            </>
          ) : null}

          {step === 1 ? (
            <>
              <Heading as="h2" variant="medium">
                How should Jendock reach {draft.repository.trim() || "the repository"}?
              </Heading>
              <RadioGroup
                name="transport"
                onChange={(selected) => {
                  if (selected === "ssh" || selected === "https") set({ transport: selected });
                }}
              >
                <RadioGroup.Label>Access method</RadioGroup.Label>
                <FormControl>
                  <Radio value="ssh" checked={draft.transport === "ssh"} />
                  <FormControl.Label>SSH deploy key (recommended)</FormControl.Label>
                  <FormControl.Caption>
                    Works for private and public repositories. The key is read-only and valid for
                    this one repository.
                  </FormControl.Caption>
                </FormControl>
                <FormControl>
                  <Radio value="https" checked={draft.transport === "https"} />
                  <FormControl.Label>HTTPS (public repositories only)</FormControl.Label>
                  <FormControl.Caption>
                    Nothing to set up, but it cannot read private repositories.
                  </FormControl.Caption>
                </FormControl>
              </RadioGroup>
              <GithubSetup repository={draft.repository} transport={draft.transport} />
              <Callout>
                You can create the project first and finish these GitHub steps afterwards; the same
                instructions stay in the project&apos;s Settings.
              </Callout>
            </>
          ) : null}

          {step === 2 ? (
            <>
              <Heading as="h2" variant="medium">
                Which branches deploy where?
              </Heading>
              <p className="muted">
                A push to the Development branch deploys the Development environment, a push to the
                Production branch deploys Production. Each branch belongs to one environment.
              </p>
              <FormControl required>
                <FormControl.Label>Development branch</FormControl.Label>
                <TextInput
                  block
                  monospace
                  value={draft.development}
                  validationStatus={errors.development ? "error" : undefined}
                  onChange={(event) => {
                    set({ development: event.target.value });
                  }}
                />
                <FormControl.Caption>
                  For testing changes before they go live. Often <code>develop</code> or{" "}
                  <code>staging</code>.
                </FormControl.Caption>
                {errors.development ? (
                  <FormControl.Validation variant="error">
                    {errors.development}
                  </FormControl.Validation>
                ) : null}
              </FormControl>
              <FormControl required>
                <FormControl.Label>Production branch</FormControl.Label>
                <TextInput
                  block
                  monospace
                  value={draft.production}
                  validationStatus={errors.production ? "error" : undefined}
                  onChange={(event) => {
                    set({ production: event.target.value });
                  }}
                />
                <FormControl.Caption>
                  The live version, usually <code>main</code>. New projects deploy Development
                  automatically and Production only when you start it.
                </FormControl.Caption>
                {errors.production ? (
                  <FormControl.Validation variant="error">
                    {errors.production}
                  </FormControl.Validation>
                ) : null}
              </FormControl>
            </>
          ) : null}

          {step === 3 ? (
            <>
              <Heading as="h2" variant="medium">
                Pick a starter pipeline
              </Heading>
              <p className="muted">
                A pipeline lists the steps that build, test and deploy your code. The starter is
                saved for both environments and you can change it later in the Pipeline tab. If the
                repository has its own <code>Jenkinsfile</code>, that one is used instead.
              </p>
              {templates.isPending ? <Loading label="Loading templates" /> : null}
              {templates.isError ? (
                <ErrorCallout title="Could not load the templates" error={templates.error} />
              ) : null}
              {templates.data ? (
                <div className={styles.templates}>
                  <RadioGroup
                    name="template"
                    onChange={(selected) => {
                      if (selected !== null) set({ template: selected });
                    }}
                  >
                    <RadioGroup.Label>Template</RadioGroup.Label>
                    {templates.data.map((item) => (
                      <FormControl key={item.name}>
                        <Radio value={item.name} checked={draft.template === item.name} />
                        <FormControl.Label>{item.title}</FormControl.Label>
                        <FormControl.Caption>{item.description}</FormControl.Caption>
                      </FormControl>
                    ))}
                    <FormControl>
                      <Radio value={NO_TEMPLATE} checked={draft.template === NO_TEMPLATE} />
                      <FormControl.Label>No template</FormControl.Label>
                      <FormControl.Caption>
                        Start from the editor&apos;s built-in starter later.
                      </FormControl.Caption>
                    </FormControl>
                  </RadioGroup>
                  {chosen ? (
                    <div>
                      <strong>Preview: {chosen.title}</strong>
                      <pre
                        className={styles.preview}
                        tabIndex={0}
                        aria-label={`Jenkinsfile of ${chosen.title}`}
                      >
                        {chosen.jenkinsfile}
                      </pre>
                    </div>
                  ) : null}
                </div>
              ) : null}
            </>
          ) : null}

          {step === 4 ? (
            <>
              <Heading as="h2" variant="medium">
                Review and create
              </Heading>
              <dl className={box.defs}>
                <dt>Repository</dt>
                <dd className="ltr">{draft.repository.trim()}</dd>
                <dt>Name</dt>
                <dd>{draft.name.trim()}</dd>
                <dt>Short name</dt>
                <dd className="ltr">{draft.slug}</dd>
                <dt>Access</dt>
                <dd>
                  {draft.transport === "ssh" ? "SSH deploy key" : "HTTPS (public repository)"}
                </dd>
                <dt>Development</dt>
                <dd className="ltr">{draft.development.trim()}</dd>
                <dt>Production</dt>
                <dd className="ltr">{draft.production.trim()}</dd>
                <dt>Starter pipeline</dt>
                <dd>{chosen ? chosen.title : "None"}</dd>
              </dl>
              {create.isError ? (
                <Callout
                  variant="danger"
                  role="alert"
                  title={slugConflict ? "That short name is taken" : "Could not create the project"}
                >
                  {errorMessage(create.error)}
                </Callout>
              ) : null}
            </>
          ) : null}
        </div>

        <div className={box.spread}>
          <Button
            disabled={step === 0 || create.isPending}
            onClick={() => {
              setErrors({});
              setStep((current) => Math.max(0, current - 1));
            }}
          >
            Back
          </Button>
          {step < STEPS.length - 1 ? (
            <Button variant="primary" onClick={next}>
              Continue
            </Button>
          ) : (
            <Button variant="primary" disabled={create.isPending} onClick={submit}>
              {create.isPending ? "Creating…" : "Create project"}
            </Button>
          )}
        </div>
      </div>
    </>
  );
}

function prettify(repoName: string): string {
  return repoName
    .split(/[-_.]+/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

# Jenkins compatibility

The platform accepts a **subset of Jenkins Declarative Pipeline**. This page records how the
subset was checked against a real Jenkins: the Declarative linter of
`jenkins/jenkins:2.541.3-lts-jdk17` with `workflow-aggregator`, `pipeline-model-definition` and
`docker-workflow` (`POST /pipeline-model-converter/validate`). Rerun everything with
`make lint-jenkins` (needs Docker; it starts and removes a throwaway container and rewrites this
file and `tests/fixtures/jenkins_verdicts.json`).

## Rules

- **Jenkins rejects, we accept** is a bug. There are none; the test-suite
  (`tests/test_jenkins_compat.py`) replays the recorded verdicts offline and fails if one appears.
- **Jenkins accepts, we reject** must be a deliberate platform decision and is listed in the
  `Difference` column with its reason. Anything else would be parser strictness and is fixed.
- The linter only checks syntax and the structure of the Declarative model. It does **not**
  reject unknown step names (`dockerBuild`, `deploy`, `healthCheck` and even `archiveArtifacts`
  all pass), unknown variables, or Groovy that is only wrong at run time. Our extra checks
  (step catalogue, variables, credentials, images, paths, docker args) therefore show up as
  "platform rule" differences. Platform steps are fine for the linter.

## Findings that changed the engine

The first comparison found these cases where we were wrong; all are fixed:

- Jenkins rejects empty `environment`, `options`, `parameters`, `post` and `triggers` sections,
  and an empty post condition block: now rejected. Triggers (empty or not) are rejected with the
  platform message.
- Jenkins rejects the same option twice (`options { retry(2); retry(3) }`): now an error.
- Jenkins accepts any depth of sequential `stages`, and `parallel` inside nested sequential
  stages; only `parallel` inside `parallel` (also through nested `stages`) is rejected. We
  allowed one level only and now follow Jenkins.
- Jenkins accepts a newline before a section brace (`stages` NEWLINE `{`, `agent` NEWLINE `{`)
  and newlines inside `credentials(...)` / `stage(...)` parentheses: now accepted.
- `timeout(5) { ... }` (positional time) is valid Jenkins: now accepted.

Things that look like parser strictness but are the same in Jenkins (so we keep them): several
statements on one line without `;` (`agent any stages { ... }`, `stage('a') {...} stage('b')
{...}`, `echo 'a' echo 'b'`), a stage with `steps` and `stages`, empty `steps`/`stages`/`parallel`,
and `parallel` nested in `parallel`.

Recorded runs are only as good as the probes: the table lists every pipeline that was compared.

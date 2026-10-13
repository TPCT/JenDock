# Pipelines (Jenkinsfile)

Pipelines are a **subset of Jenkins Declarative Pipeline**. Everything the engine accepts is
meant to be parseable by real Jenkins too (platform steps look like shared-library steps); what
it does not support is rejected with a positioned error, never ignored. The engine is pure
code (`backend.pipeline`): `parse(text) -> Pipeline`, `validate(pipeline, context) ->
list[Diagnostic]`, `serialize(pipeline) -> str`, `PipelineSyntaxError`. Nothing evaluates Groovy
or runs a process. `PipelineService.save` validates with `JenkinsfileValidator` (secret names
come from the environment's secrets) and raises `ValidationError` whose `diagnostics` are
returned by the API as `error.diagnostics` (`severity`, `message`, `line`, `column`).

## Supported

- Exactly one `pipeline { ... }`. Sections in any order, each at most once: `agent` (required),
  `environment`, `options`, `parameters`, `stages` (at least one stage), `post`; none of them
  may be empty. A newline is allowed before a section's `{`.
- `agent any`, `agent none`, `agent { docker 'img' }`, `agent { docker { image 'img'; args '...' } }`
  at pipeline and stage level. `image` is either an image built by an earlier `dockerBuild`
  (logical name) or an external reference that pins a tag or digest (`node:22`,
  `registry.example.com:5000/x:1`). `args` allowlist: `--network none`, `-e NAME=value`,
  `--memory N`, `--cpus N`; anything else is an error naming the flag.
- `environment { NAME = 'value'; NAME = "value ${OTHER}"; NAME = credentials('SECRET') }`,
  names `[A-Z_][A-Z0-9_]*`; a variable can use earlier ones and outer scopes.
- `options { timeout(time: N, unit: 'SECONDS'|'MINUTES'|'HOURS'); retry(N);
  disableConcurrentBuilds(); skipDefaultCheckout() }` (pipeline and stage level; `retry`
  1..10; the same option twice is an error).
- `parameters { string(name:, defaultValue:, description:); booleanParam(...);
  choice(name:, choices: [...], description:) }`.
- `stage('Name') { agent / environment / options / when / one of steps | parallel | stages / post }`.
  `parallel` contains only stages (a branch may hold sequential `stages`); sequential `stages`
  nest to any depth (limit 32 levels of braces) but `parallel` cannot appear inside `parallel`. Stage names are unique per level.
- `when { branch 'glob'; environment name: 'X', value: 'y'; not { ... }; allOf { ... };
  anyOf { ... } }` (several top-level conditions mean all of them).
- `post { always | changed | aborted | failure | success | unstable | cleanup { steps } }`
  (pipeline and stage level).
- Steps (command or parenthesis syntax, `;` or newline separated, trailing commas allowed):
  - `sh 'cmd'` / `sh(script:, label:)` - needs a docker agent; `returnStatus` and
    `returnStdout` are rejected because there is no Groovy to consume them.
  - `echo 'msg'`, `error 'msg'`.
  - `timeout(time: N, unit: 'MINUTES') { }` (also `timeout(N) { }`), `retry(N) { }`, `dir('relative/path') { }`,
    `withEnv(['NAME=value']) { }` (names defined by `withEnv` are visible inside its block).
  - `dockerBuild(image: 'logical-name', dockerfile: 'Dockerfile', context: '.',
    target: 'stage', args: [KEY: 'value'])` - image `[a-z0-9][a-z0-9_.-]*`, paths relative
    without `..`.
  - `deploy(service: 'web', image: 'logical-name')` or `deploy(services: [web: 'image', ...])`
    - the image must be built by a `dockerBuild` in an earlier step or stage (parallel branches
    cannot depend on each other; later stages see everything the group built).
  - `healthCheck(url: 'https://...', expectStatus: 200, timeout: 60)`.
- Values: strings, integers (up to 9 digits), `true`/`false`, lists `[...]`, maps `[k: v]`
  (`[:]` is the empty map).
- Strings: `'...'` and `'''...'''` (no interpolation), `"..."` and `"""..."""` with `${NAME}`,
  `$NAME`, `${env.NAME}`, `${params.NAME}`; escapes `\n \t \r \\ \' \" \$`. Known names: the
  pipeline/stage `environment`, `withEnv` names and the built-ins `BRANCH_NAME`, `GIT_COMMIT`,
  `BUILD_NUMBER`, `ENVIRONMENT`, `PROJECT`; `params.X` only for declared parameters. A secret
  interpolated into a double-quoted string is a warning (use single quotes). Interpolations are
  kept as variable parts in the AST and never evaluated.
- Comments `//` and `/* */`. Limits: 256 KiB input, nesting depth 32.

## Rejected (with a message)

`script { }`, `def`, `if`/`for`/`try` and other Groovy statements, assignments, method
definitions, `@Library`, imports and any code outside `pipeline { }`; `when { expression { } }`
and `when` conditions other than the ones above; `triggers` in any form (triggers are configured
in the platform); `tools`, `input`, `libraries`, `agent { label/dockerfile/node }`, other
post conditions; `null`, negative or non-integer numbers; unknown steps (the error lists the
supported ones); any `${...}` that is not a plain name (method calls, arithmetic, `a.b.c`);
unknown variables, parameters and credentials; docker args outside the allowlist; external
images without a tag or digest; `sh` without a docker agent; `deploy` of an image no earlier
`dockerBuild` produced; empty `steps`, `stages` or `parallel`; duplicate sections, stage names,
environment names or parameters.

## Compatibility with real Jenkins

Every valid fixture, the example below and the serializer output of each are accepted by the
Declarative linter of a real Jenkins, and nothing Jenkins rejects is accepted here. The
comparison (with the reason for every deliberate difference) is in
`docs/jenkins-compatibility.md`; rerun it with `make lint-jenkins` (needs Docker).

## Round trip

`serialize` writes the canonical form (4-space indent, single quotes unless a string has
variables, sections in Jenkins' conventional order, post blocks sorted, positional arguments
written in the canonical call style). For every accepted text,
`parse(serialize(parse(x))) == parse(x)` (spans are excluded from equality). Comments are not
part of the AST and are not preserved: the visual editor rewrites the canonical form, while
text-mode edits keep the user's own text.

## Editors, templates and dry run

The browser never parses Jenkinsfile text. `backend/pipeline/model*.py` define the JSON
editing model (pydantic) and its two converters (`to_model` / `to_ast`, round trip tested on every
valid fixture); `catalog.py` generates the step catalogue (titles, help texts, enums, bounds and
image roles come from `steps.py`, so forms and validator cannot disagree);
`pipeline/templates/` holds the starter Jenkinsfiles (also compared with real Jenkins);
`execution/dry_run.py` plans a pipeline for a branch without executing it. The endpoints are in
`docs/api-contract.md`, "Pipelines". The repository's `Jenkinsfile` is detected by the worker
(`pipeline.repository_file` RPC, a depth-1 filtered fetch of the environment branch).

The API image builds `frontend/` (node stage) and serves it, with a fallback to `index.html`
for client routes (`backend/api/frontend.py`).

## Example

```groovy
pipeline {
    agent none
    environment {
        DB_PASSWORD = credentials('DB_PASSWORD')
    }
    options {
        timeout(time: 30, unit: 'MINUTES')
    }
    stages {
        stage('Build image') {
            steps {
                dockerBuild(image: 'laravel-app', dockerfile: 'docker/Dockerfile')
            }
        }
        stage('Test') {
            agent {
                docker {
                    image 'laravel-app'
                    args '--network none --memory 1g'
                }
            }
            steps {
                sh 'php artisan test'
            }
        }
        stage('Migrate') {
            when { branch 'production' }
            agent { docker 'laravel-app' }
            steps {
                sh 'php artisan migrate --force'
            }
        }
        stage('Deploy') {
            steps {
                deploy service: 'app', image: 'laravel-app'
                healthCheck url: 'https://example.test/up', timeout: 90
            }
        }
    }
    post {
        failure {
            echo "Build ${BUILD_NUMBER} failed on ${BRANCH_NAME}"
        }
    }
}
```

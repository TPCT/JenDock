# Repository build profile onboarding

This slice adds an owner-authenticated console configuration for producing a tested,
immutable application artifact from an existing project's verified repository. It
records build configuration only: it does not clone, build, provision, start, deploy,
change runtime ownership or manufacture Compose configuration.

## Contract

`build-profile-get` accepts `project` and `environment`. `build-profile-save` also
requires integer `project_revision`, `environment_revision`, `profile_revision`
(zero for the initial save), and `values` with exactly these fields:

- `dockerfile`: normalized repository-relative file path.
- `context`: normalized repository-relative directory, including `.`.
- `target`: null or a bounded Docker stage identifier.
- `test_command`: nonempty bounded argv array, executed inside the built image.
- `command`: nonempty bounded application argv array.
- `healthcheck`: nonempty bounded container health argv array.
- `user`: numeric non-root UID:GID.

Paths cannot be absolute, escape using `..`, use backslashes or `.git`, or contain
control characters. No host mounts, build arguments, literal environment settings,
credential fields, raw image names, shell script fields or Compose paths are accepted.
Arguments are passed as argv; they are never interpolated into host shell commands.
Credentials must use a future explicit secret-reference flow, not these fields.

Both actions return `{profile: {...}}`. The profile contains `project`, `environment`,
all three reviewed revisions, `source_revision` (immutable recipe ID or null),
`values` (null when unconfigured), and `capabilities: {validate: boolean, deploy: false}`.
A missing profile is a valid empty state. Invalid values and stale revisions return
409 with an actionable error. Session identity supplies the audit actor.

## Persistence and worker handoff

`environment_build_profiles(project,environment,revision,recipe_revision)` is independent
of `registry_environments.recipe_revision`. A save uses BEGIN IMMEDIATE, checks all
three revisions, creates/reuses an immutable registry recipe, advances the separate
profile revision and bumps project/environment revisions. Runtime state, preferences,
and runtime recipe binding remain untouched. Two concurrent saves cannot both win.

Generated private recipe:

```json
{
  "schema_kind": "artifact-build",
  "schema_version": 1,
  "images": [{"name": "project-development", "dockerfile": "Dockerfile", "context": "."}],
  "tests": [{"image": "project-development", "command": ["app", "test"]}],
  "service_profiles": {"application-v1": {
    "revision": 1, "image_name": "project-development", "command": ["app", "serve"],
    "healthcheck": ["app", "health"], "user": "65534:65534"
  }}
}
```

Optional target is present only when configured. Image identity is generated from
project and environment; the worker derives the immutable per-job image tag.
`build_profiles.get_private(db,project,environment)` returns
`{source_revision,profile_revision,recipe}` with digest verification, or null.
Delivery captures `source_revision` as the target recipe revision for validate-only
jobs; deploying this schema is rejected. Worker path containment must also reject
symlink escape from the pinned checkout. Existing runtime delivery recipes remain
eligible through their separate existing path.

## Acceptance

Verify new empty profile; strict invalid-input rejection; immutable save and reopen;
concurrent same-revision single winner; preservation of adopted runtime ownership and
preferences; independent development/production profiles; authenticated API read/save;
then actual repository build/test/artifact publication through worker integration.
Passing metadata tests alone is not evidence of a successful image build or service
provisioning. Profiles may be saved before a repository's Dockerfile is validated;
build failure must report the actual missing/invalid repository input.

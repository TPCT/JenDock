# Environment source/build acceptance — 2026-10-11

Source/build profiles are independently persisted per Development and Production. They generate immutable artifact-only recipes without replacing runtime ownership. Revision checks reject stale review; source validation cannot deploy or provision containers. Existing complete runtime deployment recipes retain their normal routing and stopped-environment restrictions.

## Verified

- 352 backend tests and 43 frontend tests passed; Vite and both Docker image builds passed.
- Actual local Git clone, Docker build, isolated test and immutable image publication passed through `tests/live_artifact_build.py`; source labels, SQLite reconnect, unchanged peer containers and fixture cleanup verified. Only Git remote/work directory/network were substituted for local acceptance.
- Nine delivery integration regressions cover pinned source identities, stale/mixed/deploy rejection, automatic disabled state, independent environments, existing runtime preservation, webhook replay after configuration changes and source routing after managed provisioning.
- Browser on localhost: synthetic `deploy-monitor-ui` project profile saved through authenticated UI, page reloaded and saved values reopened; Production remained unconfigured. No real project profile was modified. Desktop and 390x844 mobile form inspected; mobile modal header/footer remain visible and body scrolls. Screenshot `/tmp/monitor-build-profile-mobile-20261011.png`.
- Receiver and worker isolated image imports passed.

## Limits and follow-up

The browser preview has no running worker: successful browser-clicked build and provisioning are not claimed. The synthetic local registry project is visibly named Local build profile test; it does not verify a GitHub repository. Provider GitHub access and the production deploy-build network were not exercised by the local Git fixture. Full mixed-environment rollout, routing/domains, persistent services and MCP remain broader platform work. Source profiles currently build artifacts; they do not themselves replace a running managed container.

Two pre-existing SQLite ResourceWarnings and frontend bundle-size warning remain. No Git push or production deployment was performed in this pass.

# Resource limits implementation plan

Goal: persistent project/environment budgets and verified RAM/CPU enforcement on owned services.
Architecture: store budgets separately from Git policy; the existing authenticated receiver delegates live inspection/application to the Docker worker. Reuse service identity, Compose ownership and recovery guards. Disk quotas are out of scope by explicit owner instruction; disk usage remains monitoring only.
Spec: docs/specs/dynamic-environment-platform.md and the owner's resource-limit instructions.

- [ ] Add validated, revisioned project and environment resource policies with atomic aggregate checks.
- [ ] Add authenticated policy read/save API and worker-backed observed service limits.
- [ ] Add persistent Compose service RAM/CPU resource mutation, stale revision guard, runtime verification and recovery.
- [ ] Add Resources workspace with environment selection, budgets and service allocation/application.
- [ ] Exercise persistence, oversubscription, invalid input, stale identity and rollback tests; inspect mobile and desktop UI.
- [ ] Build and deploy only when existing operations are idle. Verify actual image and API. Verify that disk quotas are not exposed as a setting.

Constraints: Development and Production coexist; saving a policy does not start or stop containers. No database/volume deletion. No secrets in resource responses. Existing unrelated changes are preserved. An unapplied budget is not reported as an enforced runtime limit. No synthetic observations on production.

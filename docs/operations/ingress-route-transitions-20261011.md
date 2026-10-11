# Gateway backend transitions

The accepted gateway definition describes fixed hostname routing, endpoint/alias and infrastructure. Current backend identity is stored separately per routed service with a generation and immutable gateway digest. Several hostnames targeting one service share that service's backend generation; unrelated services do not advance together.

Managed delivery captures route evidence before runtime mutation. An explicit absent route is part of that selection. A subsequently added gateway or changed route invalidates the operation. Endpoint/alias changes require a distinct gateway reconfiguration and are rejected before application replacement.

Successful replacement, executor-verified rollback and read-only recovery update application binding and route generation in one transaction. The persisted delivery plan retains its initial snapshot for immutable execution journal comparison and tracks its current snapshot for repeated recovery. Strong gateway verification reads current pins while checking the original immutable configuration and exact infrastructure identities.

## Acceptance status

Implementation and independent review are complete for the current source. All 542 backend tests pass, and receiver/worker/MCP images build and import the new module. The built worker binding module matches source SHA256 `a4895c66a18e9a61a7288c5f8119026a1d55ecf366d297d9b109e0a3ac9aff5d`.

Real Docker acceptance completed successfully using the product 35-second health timeout. The three actual managed transitions were normal completion (generation 1 to 2), interruption after Docker replacement before metadata commit followed by read-only recovery (2 to 3), and failed-health replacement followed by verified restoration of the old definition in a new container (3 to 4). Each passed strong gateway verification and idempotent replay without gateway recreation. Stale generations were rejected; sibling route and Production evidence remained unchanged.

Final inventory showed zero fixture application/gateway containers and networks, and preexisting deploy-monitor peers were unchanged. Machine evidence, exact hashes and completion timestamp are in `.runtime/ingress-runtime-acceptance.json`. The earlier short-timeout fixture failure exposed a diagnostic-cleanup weakness; diagnostic collection is now inside guaranteed teardown and cannot bypass cleanup.

No production activation occurred. Public DNS, Apache/TLS, domain operations/API/UI and whole-platform acceptance remain open.

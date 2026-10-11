# MCP worker and console acceptance — 2026-10-11

This checkpoint extends `mcp-access-acceptance-20261011.md`. It closes the missing local synthetic SDK-to-worker-to-console proof, not full product or production acceptance.

## Verified creation path

`tests/live_mcp_creation.py` ran the official SDK over a real loopback HTTP listener against a temporary SQLite database. A real fixture Docker image was built and checked, then published as explicitly synthetic environment artifacts. Scoped credentials discovered the template, created a service and read its operation. `worker.process_service_once` executed the real provisioning implementation. No provisioning result was mocked.

Both Development and Production fixture services reached healthy/running state, concurrently. Real cgroup reads confirmed 64 MiB memory, zero swap and 0.25 CPU. Neither service had host mounts or published ports. A Development-to-Production socket connection failed as required. Repeating each creation returned the original operation and did not create or queue another service.

Authenticated console HTTP login, `service-list` and environment workspace returned the same services. Reopening SQLite retained the running records. Existing peer container identities were unchanged. Cleanup removed only the tracked fixture containers, networks and image. Report: `.runtime/mcp-creation-last.json`. This uses synthetic application/build/provider evidence and is not an external-repository or production business-flow test. Browser-rendered cross-surface creation remains unverified.

## Progress contract and review

MCP operation status now authorizes the current owner/project/repository/environment and provides bounded cursor-based events, allowlisted stage/status codes, fixed public descriptions and explicit console recovery guidance. Raw errors, actor identities, commands, environment values and journal paths are excluded. Unknown evidence remains unknown. The original console response contract is preserved. An independent review found no blocking defect; 459 backend tests passed, including status pagination, secret sanitization and authorization.

## Packaging and proxy evidence

Added opt-in `compose.mcp.yml` sharing the exact registry, owner configuration and external integration key, with no Docker socket and a loopback-only listener. It waits for worker health at startup and has its own bounded unauthenticated 401 health probe. Compose configuration validation passed without starting production services.

`deployment/apache-mcp.conf` targets the existing monitoring TLS vhost, before its catch-all proxy. `tests/live_mcp_proxy.py` passed Apache syntax validation and exercised an actual local Apache container against MCP: unauthenticated 401; forwarded-header stripping 401 instead of boundary rejection; wrong Host 421; wrong Origin 403. The test mirrors the production global HTTPS forwarding-header setter. TLS certificate verification and authenticated traffic through the actual production vhost remain open.

Read-only SSH reverified production identity: hostname root, UID 0, eth0 176.118.198.49. Live receiver/worker images remain `0ac2162`, healthy at inspection. No production files, containers or services were modified. Production MCP packaging is prepared but not activated. Existing integration-key provisioning must be checked before activation; no key material was read or displayed.

## Remaining scope

Actual intended MCP client and production TLS/worker acceptance, OAuth interoperability, browser one-time credential lifecycle, real repository build/provisioning, complete domain/TLS management, production migration safeguards, database/file/scheduler tools, legacy environment adoption and full product acceptance remain open. Do not infer their completion from these fixture results.

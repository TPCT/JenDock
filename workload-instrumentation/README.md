# Deploy Monitor workload instrumentation 1.0.0

Experimental implementation: the initial real Laravel/PostgreSQL fixture passes;
this is not full workload-role or public API acceptance. Supported dependency pin
is **Laravel 12.69.3**, PHP >=8.2 and PostgreSQL. Composer auditing remains enabled.
Laravel 12.35.0 was rejected by Composer security advisories and is not supported.

## Trusted runtime inputs

The orchestrator injects these values into the exact accepted container:

- `DEPLOY_MONITOR_PROJECT`
- `DEPLOY_MONITOR_ENVIRONMENT`: development or production
- `DEPLOY_MONITOR_SERVICE_ID`: canonical UUID
- `DEPLOY_MONITOR_DEFINITION_DIGEST`: 64 lowercase hex characters
- `DEPLOY_MONITOR_WORKLOAD`: accepted canonical workload JSON with exactly role,
  adapter, adapter_revision, framework_version, instrumentation, configuration.

The provider reads process environment, overriding stale cached Laravel scope.
Queue configuration.connection is the Laravel database queue connection; its
configured retry_after must equal the accepted retry_after_seconds. Only exact
builtin DatabaseQueue and DatabaseUuidFailedJobProvider are supported. Their
jobs, failed jobs and instrumentation use the same PostgreSQL connection.
Scheduler configuration.connection names its explicit Laravel database connection.
The scheduler config also supplies timezone and scope=default.

The normal application `APP_KEY` supplies a purpose-derived private HMAC key.
No listener, additional credentials, Docker socket or external URL is introduced.
Application-owned `config/deploy-monitor.php` contains `retry_classes` and
`schedule_names` allowlists. These cannot come from protocol requests. Standard
framework jobs using custom retryUntil, encryption, unsupported magic methods or
unregistered classes are not retryable through this adapter. Queue payloads are
never deserialized or returned by the protocol.

Publish the additive migration explicitly using `deploy-monitor-migrations`, and
run it through the application's normal reviewed migration path. No read installs
or repairs schema. Operational events currently require explicit administrator
retention management; automatic retention is not implemented. Never delete retry
receipts while outstanding operation IDs may still be replayed.

## CLI protocol

Fixed argv: `php artisan deploy-monitor:protocol --no-ansi --no-interaction`.
Send one JSON document on stdin, at most 16 KiB, and close stdin. The trusted caller
must bound process duration and stdout/stderr independently; stdout is one JSON
response, maximum 256 KiB. Never expose raw stderr on the public API.

Every request has `protocol:1`, `operation` and exact `scope` matching the four
accepted scope inputs. No request-selected paths, commands, SQL, credentials or
connection URLs are accepted.

Operations and additional fields:

| Operation | Fields |
| --- | --- |
| capabilities | none |
| queue.observe | none |
| queue.failures | queue, optional limit (1–100), optional cursor |
| queue.retry | request_id (16–80 safe characters), queue, failed_job_id, failure_revision |
| queue.receipt | request_id |
| scheduler.observe | none |
| scheduler.history | optional limit, optional cursor |

Success is `{protocol:1,status:"ok",observed_at,scope,result}`. Errors contain
`error:{code}` and no exception, payload, environment or query text. `requeued`
means an atomic queue insert, failed-row deletion and durable receipt committed;
it does not mean business execution completed. Exact retry replay returns that
receipt, including after an application-level lost response. An unknown outcome
must be reconciled by receipt, never retried with a fresh request ID.

Queue observations contain scoped queue ready/delayed/reserved counts and worker
heartbeats. Failures contain ID, queue, failure timestamp, `job_failed`
classification, opaque keyed revision and retry_supported, with signed cursors.
Scheduler reads use persisted registry/events only and never execute tasks.

Package digest: SHA256 of canonical sorted JSON mapping relative filenames to
SHA256(file bytes), covering composer.json and all files under src, config and
database; excludes vendor, tests and this documentation. Obtain it with:

```sh
php -r "require 'vendor/autoload.php'; echo Shiftcodes\\Workloads\\PackageIdentity::digest();"
```

## Verification and current boundaries

`php tests/unit.php` runs eight protocol/config/payload regressions.
`php tests/framework.php` checks real Laravel schedule registration with an
isolated strict SQL boolean-type fixture, immutable empty registries, overlap skip
execution markers, accepted-timeout heartbeat freshness and ISO timestamp shaping.
These five checks are not PostgreSQL compatibility proof.
`tests/live_laravel.py` uses disposable exact-owned `deploy-monitor` Docker PG
and real host PHP/Laravel: actual queue success/failure events, transactional retry,
receipt/replay/conflict, business outcome, scheduler events and read-only behavior,
wrong-scope rejection, exact cleanup and peer/source-hash preservation.

The initial passing fixture does not cover public HTTP/MCP/browser integration,
Docker-exec transport identity, simultaneous retry clients, interrupted scheduler
work, actual elapsed long-running job health, or overlap/filter skip attribution
through the full PostgreSQL scheduler loop. The execution marker and timeout
boundaries have isolated regressions; broader runtime acceptance remains open.

Latest real regression: Laravel 12.69.3 / PostgreSQL passed with package digest
`b0a6dcf746f0e2a22393bd9ed44e6e020e1e94965b46bf7c3b08f854006aafa5`.
Exact fixture cleanup, unchanged peer inventory and unchanged package source hash
were checked after the run. This does not establish public workload UI readiness.

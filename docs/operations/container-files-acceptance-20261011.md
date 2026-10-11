# Application file browser acceptance — 2026-10-11

Implemented in the actual deploy-monitor checkout, current main branch. Production remains unchanged.

## Behavior

The authenticated console exposes Files on container details. The receiver forwards to the existing worker socket; the worker verifies project ownership and immutable Docker ID before and after each read. The application root is the actual container WorkingDir, never a supplied host path. Root-relative directory navigation, bounded cursor pages and text previews retain existing session/CSRF protection. React discards stale responses when container identity or navigation changes.

Listings scan at most 4096 entries and return at most 100 per page. Text previews are at most 64 KiB, UTF-8 and supported text formats. Protected names, known environment secrets, credential assignments and private-key markers are denied. Symlinks, hardlinks, devices and traversal are denied; directory descriptors and process start identity are pinned. Content filtering is conservative and does not guarantee that arbitrary source files contain no unknown secret.

Storage origin reports image, container writable layer, mount, or unknown. Docker change metadata is fetched through a bounded Unix-socket request; unavailable metadata does not manufacture provenance. Host mount sources are never returned by this API.

## Runtime boundary

The worker reads the target process root through a read-only /host/proc bind and SYS_PTRACE, with all other capabilities dropped and no-new-privileges. This does not require an interpreter or shell in the target. The proc mount alone does not make the reached target filesystem read-only: application code uses read-only descriptor operations and exposes no writes. DAC permissions still apply; owner-1001 mode-0600 content was denied without adding DAC capabilities. Stopped containers, unsupported roots and permission failures remain explicit.

## Actual acceptance

`tests/live_files_http.py` ran authenticated HTTP → Unix socket → Docker worker → actual adopted Development container under the product Compose project deploy-monitor. It checked CSRF, unauthenticated rejection, immutable identity, image/writable/mount classification and denied .env, credential-bearing config.ini, symlink, hardlink, oversized content, private permissions and traversal. Target UID was 65534. Exact fixture resources were removed; existing peer containers were unchanged. Report: `.runtime/files-http-last.json`.

The local Alpine worker fixture mounted the workstation glibc loader/library read-only to run the local Docker/Compose CLI. Production uses its existing pinned toolchain. This local fixture does not prove production kernel/proc policy compatibility.

Actual browser acceptance on the held fixture: login, Files listing, image.txt preview, Close, root-relative directory input and navigation into mounted data. Mobile 390x844 had page client/scroll widths 383/383 and preview widths 358/358. Screenshots: `/tmp/monitor-files-desktop-20261011.png`, `/tmp/monitor-files-mobile-20261011.png`, `/tmp/monitor-files-preview-mobile-20261011.png`. Long mobile container identity and breadcrumb spacing were corrected separately in 9f00a97.

## Validation and limits

476 backend tests and 62 frontend tests passed. Receiver, worker and MCP Docker image builds passed. Existing SQLite ResourceWarnings and frontend large-bundle warning remain. This is a read-only browser, not a file editor; durable source changes remain Git/build operations. Full database browsing, scheduler instrumentation, domains and final production acceptance remain separate open platform work.

Independent review found that Reload retained a stale directory cursor. Fixed Reload to return to the first page of the selected directory. Real browser regression: paged directory 105 files, first page100, second page5; mutate-paged marker created one safe fixture file against pinned target ID; Reload returned the first page without error, Next page showed six entries including file-added-by-marker.txt. Evidence: `/tmp/monitor-files-pagination-recovery-20261011.png`.
